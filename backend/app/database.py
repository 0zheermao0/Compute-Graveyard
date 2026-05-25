"""数据库模型与初始化"""
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import StaticPool

from app.config import DATABASE_URL, DATA_DIR

# 解析 sqlite 路径，确保使用绝对路径且目录存在
_db_url = DATABASE_URL
if "sqlite" in _db_url:
    # sqlite:///./data/xxx 或 sqlite:///xxx -> 统一为 DATA_DIR/lab_gpu.db
    _db_path = Path(DATA_DIR) / "lab_gpu.db"
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    _db_url = f"sqlite:///{_db_path}"

engine = create_engine(
    _db_url,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    poolclass=StaticPool if "sqlite" in DATABASE_URL else None,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_db():
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    import app.database_models  # 注册模型
    Base.metadata.create_all(bind=engine)
    _migrate_add_ssh_password()
    _migrate_add_extra_ports()
    _migrate_gpu_ids_nullable()
    _migrate_user_approval()
    _migrate_container_timestamps()
    _migrate_system_settings()
    _migrate_pending_share_json()


def _migrate_pending_share_json():
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE containers ADD COLUMN pending_share_json TEXT"))
            conn.commit()
    except Exception:
        pass


def _migrate_add_ssh_password():
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE containers ADD COLUMN ssh_password VARCHAR(64)"))
            conn.commit()
    except Exception:
        pass


def _migrate_add_extra_ports():
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE containers ADD COLUMN extra_ports VARCHAR(256)"))
            conn.commit()
    except Exception:
        pass


def _migrate_gpu_ids_nullable():
    pass


def _migrate_user_approval():
    from sqlalchemy import text
    for col, ctype in [("real_name", "VARCHAR(64)"), ("contact_type", "VARCHAR(16)"),
                       ("contact_value", "VARCHAR(64)"), ("approved", "INTEGER DEFAULT 1")]:
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {ctype}"))
                conn.commit()
        except Exception:
            pass


def _migrate_container_timestamps():
    from sqlalchemy import text
    for col, ctype in [("created_at", "DATETIME"), ("stopped_at", "DATETIME")]:
        try:
            with engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE containers ADD COLUMN {col} {ctype}"))
                conn.commit()
        except Exception:
            pass


def _migrate_system_settings():
    """确保 system_settings 表存在（Base.metadata.create_all 应已创建，这里作保险）"""
    pass


def get_setting(key: str, default: str = "") -> str:
    """读取系统配置，不存在则返回 default"""
    db = SessionLocal()
    try:
        from app.database_models import SystemSettings
        row = db.query(SystemSettings).filter(SystemSettings.key == key).first()
        return row.value if row else default
    finally:
        db.close()


def set_setting(key: str, value: str) -> None:
    """写入系统配置"""
    db = SessionLocal()
    try:
        from app.database_models import SystemSettings
        row = db.query(SystemSettings).filter(SystemSettings.key == key).first()
        if row:
            row.value = value
        else:
            db.add(SystemSettings(key=key, value=value))
        db.commit()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_default_admin():
    from app.database_models import UserModel
    from app.auth import get_password_hash, verify_password
    db = SessionLocal()
    try:
        admin = db.query(UserModel).filter(UserModel.username == "admin").first()
        if not admin:
            correct_hash = get_password_hash("admin123")
            admin = UserModel(
                username="admin",
                hashed_password=correct_hash,
                role="admin",
                display_name="管理员",
                approved=1,
            )
            db.add(admin)
            db.commit()
    except Exception as e:
        db.rollback()
        import logging
        logging.getLogger(__name__).error("create_default_admin failed: %s", e)
        raise
    finally:
        db.close()
