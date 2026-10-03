from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, inspect, text

from app.database import _idle_reclaim_column_types, _migrate_container_idle_reclaim
from app.database_models import ContainerModel


def test_idle_reclaim_migration_adds_columns_to_sqlite():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        "containers",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(128)),
    )
    metadata.create_all(engine)

    _migrate_container_idle_reclaim(engine)
    _migrate_container_idle_reclaim(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("containers")}
    assert {
        "gpu_idle_low_since",
        "gpu_idle_last_sample_at",
        "gpu_idle_stage_mask",
        "gpu_idle_warned_at",
        "gpu_idle_memory_snapshot",
        "removal_reason",
        "removed_at",
    }.issubset(columns)


def test_idle_window_migration_preserves_existing_rows_and_defaults():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        "containers",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(128)),
        Column("gpu_idle_low_since", DateTime),
        Column("gpu_idle_last_sample_at", DateTime),
        Column("removal_reason", String(256)),
        Column("removed_at", DateTime),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO containers (id, name, gpu_idle_low_since) VALUES (1, 'existing', '2026-01-01 00:00:00')"))

    _migrate_container_idle_reclaim(engine)
    _migrate_container_idle_reclaim(engine)

    columns = {column["name"]: column for column in inspect(engine).get_columns("containers")}
    assert columns["gpu_idle_stage_mask"]["nullable"] is False
    assert columns["gpu_idle_warned_at"]["nullable"] is True
    assert columns["gpu_idle_memory_snapshot"]["nullable"] is True
    assert str(columns["gpu_idle_memory_snapshot"]["type"]) == "TEXT"
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO containers (id, name) VALUES (2, 'new')"))
        rows = conn.execute(text(
            "SELECT gpu_idle_stage_mask, gpu_idle_warned_at, gpu_idle_memory_snapshot FROM containers ORDER BY id"
        )).all()
        assert rows == [(0, None, None), (0, None, None)]
        assert conn.execute(text("SELECT gpu_idle_low_since FROM containers WHERE id = 1")).scalar() == "2026-01-01 00:00:00"


def test_memory_snapshot_migration_from_existing_window_schema():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        "containers",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("gpu_idle_low_since", DateTime),
        Column("gpu_idle_last_sample_at", DateTime),
        Column("gpu_idle_stage_mask", Integer, nullable=False, server_default=text("0")),
        Column("gpu_idle_warned_at", DateTime),
        Column("removal_reason", String(256)),
        Column("removed_at", DateTime),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO containers (id, gpu_idle_stage_mask) VALUES (1, 7)"))

    _migrate_container_idle_reclaim(engine)
    with engine.begin() as conn:
        row = conn.execute(text("SELECT gpu_idle_stage_mask, gpu_idle_memory_snapshot FROM containers")).one()
        assert row == (7, None)
        conn.execute(text("UPDATE containers SET gpu_idle_memory_snapshot = :snapshot"),
                     {"snapshot": '{"0": 1024, "1": 2048}'})

    _migrate_container_idle_reclaim(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT gpu_idle_memory_snapshot FROM containers")).scalar() == '{"0": 1024, "1": 2048}'


def test_idle_window_model_defaults():
    columns = ContainerModel.__table__.columns
    assert columns.gpu_idle_stage_mask.default.arg == 0
    assert str(columns.gpu_idle_stage_mask.server_default.arg) == "0"
    assert columns.gpu_idle_stage_mask.nullable is False
    assert columns.gpu_idle_warned_at.nullable is True
    assert columns.gpu_idle_memory_snapshot.nullable is True
    assert str(columns.gpu_idle_memory_snapshot.type) == "TEXT"


def test_postgresql_migration_uses_timestamp_types():
    types = _idle_reclaim_column_types("postgresql")
    assert types["gpu_idle_low_since"] == "TIMESTAMP"
    assert types["gpu_idle_last_sample_at"] == "TIMESTAMP"
    assert types["gpu_idle_warned_at"] == "TIMESTAMP"
    assert types["gpu_idle_memory_snapshot"] == "TEXT"
    assert types["gpu_idle_stage_mask"] == "INTEGER NOT NULL DEFAULT 0"
    assert types["removed_at"] == "TIMESTAMP"
    assert types["removal_reason"] == "VARCHAR(256)"
