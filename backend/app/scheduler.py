"""定时任务：到期通知、自动回收"""
from datetime import datetime, timedelta
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.database import SessionLocal
from app.database_models import ContainerModel
from app.docker_service import stop_container, remove_container
from app.config import NOTIFY_WEBHOOK

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def _send_notify(msg: str):
    """发送通知（Webhook 如钉钉）"""
    if not NOTIFY_WEBHOOK:
        logger.info("[通知] %s", msg)
        return
    try:
        import httpx
        httpx.post(
            NOTIFY_WEBHOOK,
            json={"msgtype": "text", "text": {"content": msg}},
            timeout=5,
        )
    except Exception as e:
        logger.warning("Webhook 发送失败: %s", e)


def _check_expiry_and_notify():
    """检查即将到期的容器，发送 12h/2h 提醒"""
    db = SessionLocal()
    try:
        now = datetime.now()
        for c in db.query(ContainerModel).filter(ContainerModel.status == "running").all():
            if not c.expires_at:
                continue
            delta = (c.expires_at - now).total_seconds()
            from app.database_models import UserModel
            u = db.query(UserModel).filter(UserModel.id == c.user_id).first()
            owner_name = u.username if u else "未知"

            if 11.5 * 3600 <= delta <= 12.5 * 3600:
                _send_notify(f"【Lab-GPU】容器 {c.name} 将在约 12 小时后到期，请及时续租。用户: {owner_name}")
            elif 1.5 * 3600 <= delta <= 2.5 * 3600:
                _send_notify(f"【Lab-GPU】容器 {c.name} 将在约 2 小时后到期！用户: {owner_name}")
    except Exception as e:
        logger.exception("到期检查失败: %s", e)
    finally:
        db.close()


def _stop_expired_containers():
    """到期停用：docker stop"""
    db = SessionLocal()
    try:
        now = datetime.now()
        for c in db.query(ContainerModel).filter(ContainerModel.status == "running").all():
            if c.expires_at and c.expires_at <= now and c.container_id:
                if stop_container(c.container_id):
                    c.status = "stopped"
                    c.stopped_at = now
                    db.commit()
                    _send_notify(f"【Lab-GPU】容器 {c.name} 已到期，已执行停止。")
    except Exception as e:
        logger.exception("停用过期容器失败: %s", e)
    finally:
        db.close()


def _remove_stopped_containers():
    """停止 24 小时后清理：docker rm（不删用户目录）"""
    db = SessionLocal()
    try:
        now = datetime.now()
        threshold = now - timedelta(hours=24)
        for c in db.query(ContainerModel).filter(ContainerModel.status == "stopped").all():
            if c.stopped_at and c.stopped_at <= threshold and c.container_id:
                remove_container(c.container_id)
                c.status = "removed"
                c.container_id = None
                db.commit()
                logger.info("已清理容器 %s", c.name)
    except Exception as e:
        logger.exception("清理已停止容器失败: %s", e)
    finally:
        db.close()


def start_scheduler():
    scheduler.add_job(_check_expiry_and_notify, IntervalTrigger(minutes=30), id="notify")
    scheduler.add_job(_stop_expired_containers, IntervalTrigger(minutes=5), id="stop")
    scheduler.add_job(_remove_stopped_containers, IntervalTrigger(hours=1), id="remove")
    scheduler.start()
    logger.info("定时任务已启动")
