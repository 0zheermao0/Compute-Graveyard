"""数据库模型与初始化"""
import logging
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import StaticPool

from app.config import (
    DATABASE_URL,
    DATA_DIR,
    DEFAULT_DISK_QUOTA_BYTES,
    INITIAL_ADMIN_PASSWORD,
    INITIAL_ADMIN_USERNAME,
    NODE_ID,
    NODE_NAME,
    NODE_PUBLIC_HOST,
)

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
    _migrate_container_idle_reclaim()
    _migrate_system_settings()
    _migrate_pending_share_json()
    _migrate_disk_quota()
    _migrate_container_stop_reason()
    _migrate_compute_nodes()


def _migrate_pending_share_json():
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE containers ADD COLUMN pending_share_json TEXT"))
            conn.commit()
    except Exception:
        pass


def _disk_quota_column_types(dialect_name: str) -> dict[str, str]:
    timestamp_type = "TIMESTAMP" if dialect_name == "postgresql" else "DATETIME"
    boolean_type = "BOOLEAN" if dialect_name == "postgresql" else "INTEGER"
    return {
        "disk_quota_bytes": "BIGINT",
        "disk_usage_bytes": "BIGINT",
        "disk_usage_checked_at": timestamp_type,
        "disk_usage_scan_complete": boolean_type,
        "disk_quota_exceeded_since": timestamp_type,
        "disk_quota_blocked": boolean_type,
    }


def _add_missing_columns(bind, table_name: str, columns: dict[str, str]) -> None:
    if not inspect(bind).has_table(table_name):
        return
    existing = {column["name"] for column in inspect(bind).get_columns(table_name)}
    logger = logging.getLogger(__name__)
    for column, column_definition in columns.items():
        if column in existing:
            continue
        try:
            with bind.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column} {column_definition}"))
            existing.add(column)
        except DBAPIError as exc:
            if _is_duplicate_column_error(exc):
                logger.info("迁移列 %s 已存在", column)
                existing.add(column)
                continue
            logger.exception("新增表 %s 迁移列 %s 失败", table_name, column)
            raise


def _migrate_disk_quota(bind=None):
    if bind is None:
        bind = engine
    dialect_name = bind.dialect.name
    types = _disk_quota_column_types(dialect_name)
    defaults = {
        "disk_quota_bytes": f"{types['disk_quota_bytes']} NOT NULL DEFAULT {DEFAULT_DISK_QUOTA_BYTES}",
        "disk_usage_bytes": f"{types['disk_usage_bytes']} NOT NULL DEFAULT 0",
        "disk_usage_checked_at": types["disk_usage_checked_at"],
        "disk_usage_scan_complete": f"{types['disk_usage_scan_complete']} NOT NULL DEFAULT {'FALSE' if dialect_name == 'postgresql' else '0'}",
        "disk_quota_exceeded_since": types["disk_quota_exceeded_since"],
        "disk_quota_blocked": f"{types['disk_quota_blocked']} NOT NULL DEFAULT {'FALSE' if dialect_name == 'postgresql' else '0'}",
    }
    _add_missing_columns(bind, "users", defaults)
    if not inspect(bind).has_table("users"):
        return
    with bind.begin() as conn:
        conn.execute(text("UPDATE users SET disk_quota_bytes = :quota WHERE disk_quota_bytes IS NULL"), {"quota": DEFAULT_DISK_QUOTA_BYTES})
        conn.execute(text("UPDATE users SET disk_usage_bytes = 0 WHERE disk_usage_bytes IS NULL"))
        conn.execute(text("UPDATE users SET disk_usage_scan_complete = :complete WHERE disk_usage_scan_complete IS NULL"), {"complete": False})
        conn.execute(text("UPDATE users SET disk_quota_blocked = :blocked WHERE disk_quota_blocked IS NULL"), {"blocked": False})


def _migrate_container_stop_reason(bind=None):
    if bind is None:
        bind = engine
    _add_missing_columns(bind, "containers", {"stop_reason": "VARCHAR(64)"})


def _migrate_compute_nodes(bind=None):
    if bind is None:
        bind = engine
    _add_missing_columns(
        bind,
        "containers",
        {
            "node_id": "VARCHAR(64)",
            "node_name": "VARCHAR(128)",
            "access_host": "VARCHAR(255)",
            "service_scheme": "VARCHAR(8)",
        },
    )
    if inspect(bind).has_table("containers"):
        with bind.begin() as conn:
            conn.execute(text("UPDATE containers SET node_id = :node_id WHERE node_id IS NULL OR node_id = ''"), {"node_id": NODE_ID})
            conn.execute(text("UPDATE containers SET node_name = :node_name WHERE node_name IS NULL OR node_name = ''"), {"node_name": NODE_NAME})
            conn.execute(text("UPDATE containers SET access_host = :host WHERE access_host IS NULL OR access_host = ''"), {"host": NODE_PUBLIC_HOST})
            conn.execute(text("UPDATE containers SET service_scheme = 'http' WHERE service_scheme IS NULL OR service_scheme = ''"))
    timestamp_type = "TIMESTAMP" if bind.dialect.name == "postgresql" else "DATETIME"
    _add_missing_columns(
        bind,
        "compute_nodes",
        {
            "base_url": "VARCHAR(512) NOT NULL DEFAULT ''",
            "public_host": "VARCHAR(255) NOT NULL DEFAULT ''",
            "agent_token": "VARCHAR(512) NOT NULL DEFAULT ''",
            "enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
            "schedulable": "BOOLEAN NOT NULL DEFAULT TRUE",
            "last_seen_at": timestamp_type,
            "created_at": timestamp_type,
            "updated_at": timestamp_type,
        },
    )
    if inspect(bind).has_table("compute_nodes"):
        with bind.begin() as conn:
            existing = conn.execute(text("SELECT id FROM compute_nodes WHERE id = :node_id"), {"node_id": NODE_ID}).first()
            if existing is None:
                conn.execute(
                    text(
                        "INSERT INTO compute_nodes (id, name, base_url, public_host, agent_token, enabled, schedulable, created_at, updated_at) "
                        "VALUES (:id, :name, '', :host, '', :enabled, :schedulable, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"id": NODE_ID, "name": NODE_NAME, "host": NODE_PUBLIC_HOST, "enabled": True, "schedulable": True},
                )


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


def _idle_reclaim_column_types(dialect_name: str) -> dict[str, str]:
    timestamp_type = "TIMESTAMP" if dialect_name == "postgresql" else "DATETIME"
    return {
        "gpu_idle_low_since": timestamp_type,
        "gpu_idle_last_sample_at": timestamp_type,
        "removal_reason": "VARCHAR(256)",
        "removed_at": timestamp_type,
    }


def _is_duplicate_column_error(exc: DBAPIError) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    return "duplicate column" in message or "already exists" in message


def _migrate_container_idle_reclaim(bind=engine):
    logger = logging.getLogger(__name__)
    existing = {column["name"] for column in inspect(bind).get_columns("containers")}
    for column, column_type in _idle_reclaim_column_types(bind.dialect.name).items():
        if column in existing:
            continue
        try:
            with bind.begin() as conn:
                conn.execute(text(f"ALTER TABLE containers ADD COLUMN {column} {column_type}"))
        except DBAPIError as exc:
            if _is_duplicate_column_error(exc):
                logger.info("容器表迁移列 %s 已存在", column)
                continue
            logger.exception("新增容器表迁移列 %s 失败", column)
            raise


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
    from app.auth import get_password_hash
    db = SessionLocal()
    try:
        admin = db.query(UserModel).filter(UserModel.role == "admin").first()
        if not admin and db.query(UserModel).count() == 0 and INITIAL_ADMIN_PASSWORD:
            admin = UserModel(
                username=INITIAL_ADMIN_USERNAME,
                hashed_password=get_password_hash(INITIAL_ADMIN_PASSWORD),
                role="admin",
                display_name="管理员",
                approved=1,
                disk_quota_bytes=DEFAULT_DISK_QUOTA_BYTES,
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
