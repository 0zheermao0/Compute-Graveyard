"""租期与续租 API"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from app.database import get_db
from app.database_models import ContainerModel, LeaseRecordModel
from app.models import LeaseRenewRequest
from app.config import MAX_LEASE_DAYS

router = APIRouter()

# 可续租的提前窗口：到期前 24 小时内
RENEW_WINDOW_HOURS = 24


@router.post("/renew/{container_id}")
def renew_lease(container_id: int, req: LeaseRenewRequest, user=Depends(get_current_user), db=Depends(get_db)):
    c = db.query(ContainerModel).filter(ContainerModel.id == container_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="容器不存在")
    if c.user_id != user.id:
        raise HTTPException(status_code=403, detail="只能续租自己的容器")
    if c.status != "running":
        raise HTTPException(status_code=400, detail="仅运行中的容器可续租")

    now = datetime.now()
    if now >= c.expires_at:
        raise HTTPException(status_code=400, detail="已过期，无法续租")
    # 到期前 24 小时内可续租
    if (c.expires_at - now).total_seconds() > RENEW_WINDOW_HOURS * 3600:
        raise HTTPException(
            status_code=400,
            detail=f"仅在到期前 {RENEW_WINDOW_HOURS} 小时内可申请续租",
        )

    days = min(req.lease_days, MAX_LEASE_DAYS)
    if days < 1:
        days = 1

    new_expires = c.expires_at + timedelta(days=days)
    c.expires_at = new_expires
    rec = LeaseRecordModel(container_id=c.id, action="renew", expires_at=new_expires)
    db.add(rec)
    db.commit()
    return {"message": "续租成功", "expires_at": new_expires}
