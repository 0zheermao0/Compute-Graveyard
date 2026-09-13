"""SQLAlchemy 数据库模型"""
from datetime import datetime
from sqlalchemy import BigInteger, Column, Integer, String, DateTime, Boolean, Text, ForeignKey, JSON, text
from sqlalchemy.orm import relationship, synonym

from app.config import DEFAULT_DISK_QUOTA_BYTES, NODE_ID
from app.database import Base  # noqa: F401


class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    hashed_password = Column(String(128), nullable=False)
    display_name = Column(String(64), default="")
    real_name = Column(String(64), default="")  # 实名
    contact_type = Column(String(16), default="")  # phone | wechat
    contact_value = Column(String(64), default="")  # 手机号或微信号
    approved = Column(Integer, default=0)  # 0 待审批 1 已通过，admin 默认 1
    role = Column(String(16), default="user")  # user | admin
    created_at = Column(DateTime, default=datetime.now)
    disk_quota_bytes = Column(BigInteger, nullable=False, default=DEFAULT_DISK_QUOTA_BYTES, server_default=text(str(DEFAULT_DISK_QUOTA_BYTES)))
    disk_usage_bytes = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    disk_usage_checked_at = Column(DateTime, nullable=True)
    disk_usage_scan_complete = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    disk_quota_exceeded_since = Column(DateTime, nullable=True)
    disk_quota_blocked = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    quota_bytes = synonym("disk_quota_bytes")
    usage_bytes = synonym("disk_usage_bytes")
    quota_exceeded_since = synonym("disk_quota_exceeded_since")
    disk_quota_over_since = synonym("disk_quota_exceeded_since")
    over_quota_since = synonym("disk_quota_exceeded_since")
    quota_blocked = synonym("disk_quota_blocked")
    containers = relationship("ContainerModel", back_populates="owner")


class ComputeNodeModel(Base):
    __tablename__ = "compute_nodes"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    base_url = Column(String(512), nullable=False, default="")
    public_host = Column(String(255), nullable=False, default="")
    agent_token = Column(String(512), nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    schedulable = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    last_seen_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ContainerModel(Base):
    __tablename__ = "containers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    container_id = Column(String(64), unique=True, index=True)  # Docker 容器 ID
    name = Column(String(128), nullable=False, unique=True)  # 容器名
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    node_id = Column(String(64), nullable=False, default=NODE_ID, index=True)
    node_name = Column(String(128), nullable=True)
    access_host = Column(String(255), nullable=True)
    service_scheme = Column(String(8), nullable=False, default="http", server_default=text("'http'"))
    gpu_ids = Column(String(32), default="")  # 如 "0,1"，空表示纯 CPU
    ssh_port = Column(Integer, nullable=False)
    extra_ports = Column(String(256), nullable=True)  # JSON: {"8888":30123,"6006":30124,"8080":30125}
    ssh_password = Column(String(64), nullable=True)  # 随机生成，仅容器拥有者可见
    status = Column(String(16), default="running")  # running | stopped | removed | pending_share_approval | share_rejected
    stop_reason = Column(String(64), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    stopped_at = Column(DateTime)  # 停止时间，用于 24h 后清理
    created_at = Column(DateTime, default=datetime.now)
    gpu_idle_low_since = Column(DateTime, nullable=True)
    gpu_idle_last_sample_at = Column(DateTime, nullable=True)
    removal_reason = Column(String(256), nullable=True)
    removed_at = Column(DateTime, nullable=True)
    # GPU 共用审批：pending_share_json 存 JSON（lease_days、approvers 等），仅在 status=pending_share_approval 时有值
    pending_share_json = Column(Text, nullable=True)
    owner = relationship("UserModel", back_populates="containers")
    lease_records = relationship("LeaseRecordModel", back_populates="container")


class LeaseRecordModel(Base):
    __tablename__ = "lease_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    container_id = Column(Integer, ForeignKey("containers.id"), nullable=False)
    action = Column(String(16), nullable=False)  # create | renew
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    container = relationship("ContainerModel", back_populates="lease_records")


class SystemSettings(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(64), unique=True, nullable=False, index=True)
    value = Column(String(256), nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
