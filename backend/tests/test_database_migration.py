from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect

from app.database import _idle_reclaim_column_types, _migrate_container_idle_reclaim


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
        "removal_reason",
        "removed_at",
    }.issubset(columns)


def test_postgresql_migration_uses_timestamp_types():
    types = _idle_reclaim_column_types("postgresql")
    assert types["gpu_idle_low_since"] == "TIMESTAMP"
    assert types["gpu_idle_last_sample_at"] == "TIMESTAMP"
    assert types["removed_at"] == "TIMESTAMP"
    assert types["removal_reason"] == "VARCHAR(256)"
