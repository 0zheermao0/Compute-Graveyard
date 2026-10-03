"""SQLAlchemy 数据库模型"""
from datetime import datetime
import secrets

from sqlalchemy import BigInteger, Column, Integer, String, DateTime, Boolean, Text, ForeignKey, UniqueConstraint, text
from sqlalchemy.orm import relationship, synonym

from app.config import DEFAULT_DISK_QUOTA_BYTES, MAX_GPUS_PER_USER, NODE_ID
from app.database import Base  # noqa: F401


class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    hashed_password = Column(String(128), nullable=False)
    webauthn_user_handle = Column(String(64), nullable=True, unique=True, index=True, default=lambda: secrets.token_urlsafe(32))
    display_name = Column(String(64), default="")
    real_name = Column(String(64), default="")  # 实名
    contact_type = Column(String(16), default="")  # phone | wechat
    contact_value = Column(String(64), default="")  # 手机号或微信号
    approved = Column(Integer, default=0)  # 0 待审批 1 已通过，admin 默认 1
    role = Column(String(16), default="user")  # user | admin
    created_at = Column(DateTime, default=datetime.now)
    reputation_score = Column(Integer, nullable=False, default=0, server_default=text("0"))
    max_gpus_per_user = Column(Integer, nullable=False, default=MAX_GPUS_PER_USER, server_default=text(str(MAX_GPUS_PER_USER)))
    disk_quota_bytes = Column(BigInteger, nullable=False, default=DEFAULT_DISK_QUOTA_BYTES, server_default=text(str(DEFAULT_DISK_QUOTA_BYTES)))
    disk_usage_bytes = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    disk_usage_checked_at = Column(DateTime, nullable=True)
    disk_usage_scan_complete = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    disk_quota_exceeded_since = Column(DateTime, nullable=True)
    disk_quota_blocked = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    disk_notification_band = Column(Integer, nullable=False, default=0, server_default=text("0"))
    quota_bytes = synonym("disk_quota_bytes")
    usage_bytes = synonym("disk_usage_bytes")
    quota_exceeded_since = synonym("disk_quota_exceeded_since")
    disk_quota_over_since = synonym("disk_quota_exceeded_since")
    over_quota_since = synonym("disk_quota_exceeded_since")
    quota_blocked = synonym("disk_quota_blocked")
    containers = relationship("ContainerModel", back_populates="owner")
    personal_tokens = relationship("PersonalTokenModel", back_populates="owner", cascade="all, delete-orphan")
    passkeys = relationship("PasskeyModel", back_populates="owner", cascade="all, delete-orphan")
    passkey_challenges = relationship("PasskeyChallengeModel", back_populates="owner", cascade="all, delete-orphan")


class PasskeyModel(Base):
    __tablename__ = "passkeys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    credential_id = Column(Text, unique=True, nullable=False)
    public_key = Column(Text, nullable=False)
    sign_count = Column(BigInteger, nullable=False, default=0)
    rp_id = Column(String(253), nullable=False)
    name = Column(String(100), nullable=False)
    transports = Column(Text, nullable=False, default="[]")
    device_type = Column(String(32), nullable=False)
    backed_up = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    last_used_at = Column(DateTime, nullable=True)
    owner = relationship("UserModel", back_populates="passkeys")


class PasskeyChallengeModel(Base):
    __tablename__ = "passkey_challenges"

    id = Column(String(64), primary_key=True)
    challenge = Column(String(128), nullable=False)
    kind = Column(String(16), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    rp_id = Column(String(253), nullable=False)
    origin = Column(String(512), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    owner = relationship("UserModel", back_populates="passkey_challenges")


class PersonalTokenModel(Base):
    __tablename__ = "personal_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    owner = relationship("UserModel", back_populates="personal_tokens")


class UserNotificationModel(Base):
    __tablename__ = "user_notifications"
    __table_args__ = (UniqueConstraint("user_id", "event_key"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    event_key = Column(String(128), nullable=False)
    type = Column(String(64), nullable=False)
    title = Column(String(256), nullable=False)
    message = Column(Text, nullable=False)
    container_id = Column(Integer, nullable=True)
    container_name = Column(String(128), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    read_at = Column(DateTime, nullable=True)


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
    gpu_idle_stage_mask = Column(Integer, nullable=False, default=0, server_default=text("0"))
    gpu_idle_warned_at = Column(DateTime, nullable=True)
    gpu_idle_cards_json = Column(Text, nullable=True)  # Per-card windows and runtime identity
    gpu_idle_memory_snapshot = Column(Text, nullable=True)  # JSON: 每张 GPU 上一采样 memory_used_mb
    removal_reason = Column(String(256), nullable=True)
    removed_at = Column(DateTime, nullable=True)
    # GPU 共用审批：pending_share_json 存 JSON（lease_days、approvers 等），仅在 status=pending_share_approval 时有值
    pending_share_json = Column(Text, nullable=True)
    target_container_id = Column(Integer, ForeignKey("containers.id"), nullable=True)
    owner = relationship("UserModel", back_populates="containers")
    lease_records = relationship("LeaseRecordModel", back_populates="container")


class ShareRequestModel(Base):
    __tablename__ = "share_requests"

    id = Column(String(64), primary_key=True)
    payload = Column(Text, nullable=False)
    approvers = Column(Text, nullable=False)
    state = Column(String(24), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    provision_result = Column(Text, nullable=True)


class LeaseRecordModel(Base):
    __tablename__ = "lease_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    container_id = Column(Integer, ForeignKey("containers.id"), nullable=False)
    action = Column(String(16), nullable=False)  # create | renew
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    container = relationship("ContainerModel", back_populates="lease_records")


class GPUHistorySampleModel(Base):
    __tablename__ = "gpu_history_samples"
    __table_args__ = (UniqueConstraint("node_id", "gpu_index", "sampled_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_id = Column(String(64), nullable=False, index=True)
    node_name = Column(String(128), nullable=False)
    gpu_index = Column(Integer, nullable=False)
    gpu_name = Column(String(128), nullable=False)
    sampled_at = Column(DateTime, nullable=False, index=True)
    utilization = Column(Integer, nullable=True)
    memory_used_mb = Column(BigInteger, nullable=True)
    memory_total_mb = Column(BigInteger, nullable=True)
    owners_json = Column(Text, nullable=False, default="[]")


class ReputationEventModel(Base):
    __tablename__ = "reputation_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event_key = Column(String(256), unique=True, nullable=False)
    event_type = Column(String(64), nullable=False)
    reason = Column(Text, nullable=False, default="", server_default=text("''"))
    source = Column(String(64), nullable=False, default="custom", server_default=text("'custom'"))
    delta = Column(Integer, nullable=False)
    score_before = Column(Integer, nullable=False)
    score_after = Column(Integer, nullable=False)
    actor_id = Column(Integer, nullable=True)
    container_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class SystemSettings(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(64), unique=True, nullable=False, index=True)
    value = Column(String(256), nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
