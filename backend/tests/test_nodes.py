from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text
from sqlalchemy.orm import Session

from app.api import agent
from app.container_lifecycle import remove_container_record
from app.database import _migrate_compute_nodes
from app.database_models import ComputeNodeModel
from app.node_service import build_service_url, node_response, select_node
from app.remote_agent import normalize_agent_base_url


def test_node_response_never_exposes_agent_token():
    node = SimpleNamespace(
        id="worker-1",
        name="Worker 1",
        base_url="http://worker:8000",
        public_host="worker.example",
        agent_token="secret-token",
        enabled=True,
        schedulable=True,
        last_seen_at=None,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )
    payload = node_response(node)
    assert payload["has_agent_token"] is True
    assert "agent_token" not in payload
    assert "secret-token" not in str(payload)


def test_agent_auth_rejects_when_token_is_not_configured(monkeypatch):
    monkeypatch.setattr(agent, "NODE_ROLE", "worker")
    monkeypatch.setattr(agent, "AGENT_API_TOKEN", "")
    with pytest.raises(HTTPException) as exc:
        agent.require_agent_token(HTTPAuthorizationCredentials(scheme="Bearer", credentials="anything"))
    assert exc.value.status_code == 401


def test_agent_auth_accepts_exact_bearer_token(monkeypatch):
    monkeypatch.setattr(agent, "NODE_ROLE", "worker")
    monkeypatch.setattr(agent, "AGENT_API_TOKEN", "configured-token")
    assert agent.require_agent_token(HTTPAuthorizationCredentials(scheme="Bearer", credentials="configured-token")) is None


def test_compute_node_migration_backfills_existing_containers(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        "containers",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(128)),
    )
    Table(
        "compute_nodes",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("name", String(128)),
        Column("base_url", String(512)),
        Column("public_host", String(255)),
        Column("agent_token", String(512)),
        Column("enabled", Integer),
        Column("schedulable", Integer),
        Column("created_at", String),
        Column("updated_at", String),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO containers (id, name) VALUES (1, 'legacy')"))

    _migrate_compute_nodes(engine)
    _migrate_compute_nodes(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("containers")}
    assert {"node_id", "node_name", "access_host", "service_scheme"}.issubset(columns)
    node_columns = {column["name"] for column in inspect(engine).get_columns("compute_nodes")}
    assert "last_seen_at" in node_columns
    with engine.connect() as conn:
        row = conn.execute(text("SELECT node_id, node_name, access_host FROM containers WHERE id = 1")).one()
    assert row.node_id
    assert row.node_name
    assert row.access_host


def test_local_placement_preserves_standalone_behavior(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    ComputeNodeModel.__table__.create(engine)
    with Session(engine) as db:
        from app.node_service import NODE_ID
        db.add(ComputeNodeModel(id=NODE_ID, name="Local", public_host="localhost"))
        db.commit()
        monkeypatch.setattr(
            "app.node_service.inventory_for_node",
            lambda _db, _node: {"gpus": [{"index": 0}], "containers": [{"status": "running", "gpu_ids": "0"}]},
        )
        node, _ = select_node(db, "local", None, [0], False)
        assert node.id == NODE_ID


def test_auto_placement_skips_offline_and_selects_available_gpu(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    ComputeNodeModel.__table__.create(engine)
    with Session(engine) as db:
        db.add_all([
            ComputeNodeModel(id="offline", name="Offline", base_url="http://offline", public_host="offline", agent_token="x"),
            ComputeNodeModel(id="busy", name="Busy", base_url="http://busy", public_host="busy", agent_token="x"),
            ComputeNodeModel(id="free", name="Free", base_url="http://free", public_host="free", agent_token="x"),
        ])
        db.commit()

        def fake_inventory(_db, node):
            if node.id == "offline":
                from app.remote_agent import RemoteAgentError
                raise RemoteAgentError("offline")
            containers = [{"status": "running", "gpu_ids": "0"}] if node.id == "busy" else []
            return {
                "gpus": [{"index": 0, "memory_percent": 0}],
                "containers": containers,
                "system_load": {"memory_percent": 10, "cpu_percent": 10},
            }

        monkeypatch.setattr("app.node_service.inventory_for_node", fake_inventory)
        node, _ = select_node(db, "auto", None, [0], False)
        assert node.id == "free"


def test_auto_placement_skips_remote_gpu_occupied_by_worker_local_container(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    ComputeNodeModel.__table__.create(engine)
    with Session(engine) as db:
        db.add(ComputeNodeModel(id="worker-1", name="Worker 1", base_url="http://worker", public_host="worker", agent_token="x"))
        db.commit()
        monkeypatch.setattr(
            "app.node_service.inventory_for_node",
            lambda _db, _node: {
                "gpus": [{"index": 0, "memory_percent": 10}],
                "containers": [{"status": "running", "gpu_ids": "0"}],
                "system_load": {"memory_percent": 10, "cpu_percent": 10},
            },
        )
        with pytest.raises(ValueError, match="没有在线且资源满足要求"):
            select_node(db, "auto", None, [0], False, applicant_id=8, max_share=4)


def test_remote_removal_routes_through_node_runtime(monkeypatch):
    container = SimpleNamespace(
        id=1,
        name="remote-container",
        status="running",
        container_id="docker-id",
        node_id="worker-1",
        stopped_at=None,
        removed_at=None,
        removal_reason=None,
        gpu_ids="0",
        gpu_idle_low_since=None,
        gpu_idle_last_sample_at=None,
        pending_share_json=None,
    )
    db = SimpleNamespace(commit=lambda: None)
    calls = []
    monkeypatch.setattr("app.node_service.delete_on_node", lambda passed_db, passed_container: calls.append((passed_db, passed_container)) or True)
    result = remove_container_record(db, container, "test")
    assert result.success
    assert calls == [(db, container)]
    assert container.status == "removed"


def test_local_placement_rejects_unknown_gpu(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    ComputeNodeModel.__table__.create(engine)
    with Session(engine) as db:
        from app.node_service import NODE_ID
        db.add(ComputeNodeModel(id=NODE_ID, name="Local", public_host="localhost"))
        db.commit()
        monkeypatch.setattr(
            "app.node_service.inventory_for_node",
            lambda _db, _node: {"gpus": [], "containers": [], "system_load": {}},
        )
        with pytest.raises(ValueError, match="不包含所选 GPU"):
            select_node(db, "local", None, [0], False)


def test_auto_placement_allows_local_gpu_sharing_when_capacity_remains(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    ComputeNodeModel.__table__.create(engine)
    with Session(engine) as db:
        from app.node_service import NODE_ID
        db.add(ComputeNodeModel(id=NODE_ID, name="Local", public_host="localhost"))
        db.commit()
        monkeypatch.setattr(
            "app.node_service.inventory_for_node",
            lambda _db, _node: {
                "gpus": [{"index": 0, "memory_percent": 20}],
                "containers": [{"status": "running", "gpu_ids": "0"}],
                "system_load": {"memory_percent": 10, "cpu_percent": 10},
            },
        )
        monkeypatch.setattr("app.node_service._users_per_gpu", lambda _db, _node_id: {0: {7}})
        node, _ = select_node(db, "auto", None, [0], False, applicant_id=8, max_share=2)
        assert node.id == NODE_ID


def test_agent_base_url_rejects_ssrf_prone_and_ambiguous_urls():
    assert normalize_agent_base_url("https://worker.example:9000/") == "https://worker.example:9000"
    for value in ("http://127.0.0.1:9000", "http://169.254.169.254", "http://user:pass@worker", "http://worker/path"):
        with pytest.raises(ValueError):
            normalize_agent_base_url(value)


def test_service_url_formats_ipv6_host():
    assert build_service_url("https", "2001:db8::1", 8080) == "https://[2001:db8::1]:8080"


def test_agent_refuses_non_worker_role(monkeypatch):
    monkeypatch.setattr(agent, "NODE_ROLE", "master")
    monkeypatch.setattr(agent, "AGENT_API_TOKEN", "configured-token")
    with pytest.raises(HTTPException) as exc:
        agent.require_agent_token(HTTPAuthorizationCredentials(scheme="Bearer", credentials="configured-token"))
    assert exc.value.status_code == 404


def test_agent_refuses_unmanaged_container(monkeypatch):
    monkeypatch.setattr(agent, "is_managed_container", lambda _container_id: False)
    with pytest.raises(HTTPException) as exc:
        agent._require_managed("foreign-container")
    assert exc.value.status_code == 403
