import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import admin, agent, containers, dashboard
from app.auth import create_access_token
from app.container_lifecycle import remove_container_record
from app.database import Base, _migrate_compute_nodes, get_db
from app.database_models import ComputeNodeModel, ContainerModel, ShareRequestModel, UserModel
from app.node_service import aggregate_inventories, build_service_url, node_response, select_node, verified_owners
from app.remote_agent import normalize_agent_base_url


def test_admin_delete_node_requires_verified_empty_master_workspace(monkeypatch):
    from app.remote_agent import RemoteAgentError

    monkeypatch.setattr(admin, "NODE_ROLE", "master")
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(UserModel(username="boss", hashed_password="x", role="admin"))
        db.add(UserModel(username="alice", hashed_password="x", role="user"))
        db.add(ComputeNodeModel(id="remote", name="remote", base_url="http://remote.example", agent_token="token"))
        db.commit()
        alice = db.query(UserModel).filter_by(username="alice").one()
        db.add(ContainerModel(name="old-box", user_id=alice.id, node_id="remote", status="removed",
                              ssh_port=22001, expires_at=datetime.now() + timedelta(days=1)))
        db.commit()
        app = FastAPI()
        app.include_router(admin.router, prefix="/api/admin")
        app.dependency_overrides[get_db] = lambda: db
        path = "/api/admin/nodes/remote"
        headers = {"Authorization": f"Bearer {create_access_token({'sub': 'boss'})}"}
        response = {"node_id": "remote", "has_workspace_data": True, "complete": True}
        monkeypatch.setattr(admin.RemoteAgentClient, "workspace_data", lambda _: response.copy())
        with TestClient(app) as client:
            assert client.delete(path).status_code == 401
            assert client.delete(path, headers=headers).status_code == 409
            for change in ({"node_id": "wrong"}, {"complete": False}, {"complete": 1},
                           {"has_workspace_data": 0}, {"has_workspace_data": None}):
                monkeypatch.setattr(admin.RemoteAgentClient, "workspace_data", lambda _, change=change: {
                    **response, **change,
                })
                assert client.delete(path, headers=headers).status_code == 502
            monkeypatch.setattr(admin.RemoteAgentClient, "workspace_data", lambda _: None)
            assert client.delete(path, headers=headers).status_code == 502
            def offline(_):
                raise RemoteAgentError("offline")
            monkeypatch.setattr(admin.RemoteAgentClient, "workspace_data", offline)
            assert client.delete(path, headers=headers).status_code == 502
            assert db.get(ComputeNodeModel, "remote") is not None
            monkeypatch.setattr(admin.RemoteAgentClient, "workspace_data", lambda _: {
                **response, "has_workspace_data": False,
            })
            assert client.delete(path, headers=headers).status_code == 200
            assert db.get(ComputeNodeModel, "remote") is None
    engine.dispose()


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


def test_local_placement_rejects_unknown_runtime(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        from app.node_service import NODE_ID
        db.add(ComputeNodeModel(id=NODE_ID, name="Local", public_host="localhost"))
        db.commit()
        monkeypatch.setattr(
            "app.node_service.inventory_for_node",
            lambda _db, _node: {"gpus": [{"index": 0}], "containers": [{"status": "running", "gpu_ids": "0"}]},
        )
        with pytest.raises(ValueError, match="未知占用"):
            select_node(db, "local", None, [0], False)


def test_auto_placement_skips_offline_and_selects_available_gpu(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
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
    Base.metadata.create_all(engine)
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


@pytest.mark.parametrize("status,name", [
    ("running", "worker-private"),
    ("merging", "worker-private"),
    ("stopped", "worker-private-merge-old"),
])
def test_remote_specific_and_merge_placement_respect_worker_private_gpus(monkeypatch, status, name):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(ComputeNodeModel(id="worker-1", name="Worker 1", base_url="http://worker", public_host="worker", agent_token="x"))
        db.commit()
        inventory = {
            "gpus": [{"index": index, "memory_percent": 0} for index in range(6)],
            "containers": [
                {"container_id": "worker-only-a", "name": name, "status": status, "gpu_ids": "0,1"},
                {"container_id": "worker-only-b", "name": name, "status": status, "gpu_ids": "4,5"},
            ],
            "system_load": {"memory_percent": 10, "cpu_percent": 10},
        }
        monkeypatch.setattr("app.node_service.inventory_for_node", lambda _db, _node: inventory)
        for placement_mode in ("auto", "specific"):
            node, selected = select_node(db, placement_mode, "worker-1", [2, 3], False, applicant_id=8, max_share=4)
            assert node.id == "worker-1"
            assert selected is inventory
        for gpu_ids in ([0, 1], [4, 5]):
            with pytest.raises(ValueError, match="未知占用"):
                select_node(db, "specific", "worker-1", gpu_ids, False, applicant_id=8, max_share=4)
    engine.dispose()


def test_remote_specific_placement_allows_master_known_sharing(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(ComputeNodeModel(id="worker-1", name="Worker 1", base_url="http://worker", public_host="worker", agent_token="x"))
        db.add(UserModel(id=7, username="owner", hashed_password="x", role="user"))
        db.commit()
        db.add(ContainerModel(
            container_id="master-known", name="master-known", user_id=7, node_id="worker-1",
            gpu_ids="0", ssh_port=22001, status="running", expires_at=datetime.now() + timedelta(days=1),
        ))
        db.commit()
        monkeypatch.setattr("app.node_service.inventory_for_node", lambda _db, _node: {
            "gpus": [{"index": 0}, {"index": 1}],
            "containers": [
                {"container_id": "master-known", "name": "master-known", "username": "owner", "status": "running", "gpu_ids": "0"},
                {"container_id": "worker-only", "name": "worker-private", "status": "running", "gpu_ids": "1"},
            ],
            "system_load": {"memory_percent": 10, "cpu_percent": 10},
        })
        node, _ = select_node(db, "specific", "worker-1", [0], False, applicant_id=8, max_share=2)
        assert node.id == "worker-1"
        with pytest.raises(ValueError, match="未知占用"):
            select_node(db, "specific", "worker-1", [1], False, applicant_id=8, max_share=2)
    engine.dispose()


@pytest.mark.parametrize("status,name,expected", [
    ("running", "worker-private", True),
    ("merging", "worker-private", True),
    ("stopped", "worker-private-merge-old", True),
    ("stopped", "worker-private", False),
    ("removed", "worker-private", False),
])
def test_dashboard_external_inventory_allocation_states(status, name, expected):
    inventory = {
        "gpus": [{"index": index} for index in range(4)],
        "containers": [{"container_id": "worker-only", "name": name, "status": status, "gpu_ids": "0,1"}],
    }
    rows = dashboard._node_gpu_sharing(inventory, [], "worker-1", 4)
    assert [(row.external_occupied, row.occupant_count, row.selectable) for row in rows] == [
        (expected, int(expected), not expected),
        (expected, int(expected), not expected),
        (False, 0, True),
        (False, 0, True),
    ]


def test_dashboard_known_remote_occupants_are_not_double_counted():
    known = [
        {"container_id": "master-id", "username": "owner"},
        {"container_id": "master-id-2", "username": "owner"},
    ]
    inventory = {
        "gpus": [{"index": index} for index in range(4)],
        "containers": [
            {"container_id": "master-id", "status": "running", "gpu_ids": "0"},
            {"container_id": "master-id-2", "status": "running", "gpu_ids": "0"},
            {"container_id": "stopped-id", "status": "stopped", "gpu_ids": "1"},
        ],
    }
    rows = dashboard._node_gpu_sharing(inventory, known[:2], "worker-1", 2, {"master-id", "master-id-2"})
    assert [(row.external_occupied, row.occupant_count, row.selectable) for row in rows] == [
        (False, 1, True), (False, 0, True), (False, 0, True), (False, 0, True),
    ]
    inventory["containers"].append({"container_id": "private-id", "status": "running", "gpu_ids": "0"})
    row = dashboard._node_gpu_sharing(inventory, known[:2], "worker-1", 4, {"master-id", "master-id-2"})[0]
    assert (row.external_occupied, row.occupant_count, row.selectable) == (True, 2, False)


def test_dashboard_local_only_does_not_request_remote_agents(owner_db, monkeypatch):
    from app import node_service

    inventory = {
        "node_id": "worker-1",
        "gpus": [{"index": 0, "name": "GPU"}],
        "containers": [],
        "system_load": {"cpu_percent": 1, "memory_used_gb": 1, "memory_total_gb": 10,
                        "memory_percent": 10, "disk_free_gb": 5, "disk_total_gb": 10},
    }
    monkeypatch.setattr(node_service, "NODE_ID", "worker-1")
    monkeypatch.setattr(dashboard, "NODE_ID", "worker-1")
    monkeypatch.setattr(dashboard, "NODE_ROLE", "master")
    monkeypatch.setattr(node_service, "local_inventory", lambda _db: inventory)
    monkeypatch.setattr(dashboard, "get_gpu_info", lambda: inventory["gpus"])
    monkeypatch.setattr(dashboard, "get_system_load", lambda: inventory["system_load"])
    monkeypatch.setattr(dashboard, "get_setting", lambda *_: "4")
    monkeypatch.setattr(node_service.RemoteAgentClient, "inventory", lambda _client: pytest.fail("remote inventory requested"))

    rows = aggregate_inventories(owner_db, local_only=True)
    assert [(row["node"]["id"], row["online"]) for row in rows] == [("worker-1", True), ("worker-2", False)]
    response = dashboard.get_dashboard(db=owner_db, _=None, local_only=True)
    assert [(node.node_id, node.online) for node in response.nodes] == [("worker-1", True), ("worker-2", False)]
    assert response.nodes[0].gpu_sharing[0].selectable is True


def test_dashboard_disk_ranking_only_includes_completed_scans(owner_db):
    alice = owner_db.get(UserModel, 1)
    bob = owner_db.get(UserModel, 2)
    alice.disk_usage_bytes = 0
    alice.disk_usage_scan_complete = True
    bob.disk_usage_bytes = 100000
    bob.disk_usage_scan_complete = False
    owner_db.commit()
    assert [(row.username, row.usage_bytes) for row in dashboard._disk_ranking(owner_db)] == [("alice", 0)]
    bob.disk_usage_scan_complete = True
    owner_db.commit()
    assert [(row.username, row.rank) for row in dashboard._disk_ranking(owner_db)] == [("bob", 1), ("alice", 2)]


def test_dashboard_gpu_estimate_multi_gpu_sharing_and_dedup():
    from app.models import ContainerOccupancy, DashboardNode, GPUInfo
    now = datetime(2026, 9, 29, 12)

    def occupancy(user, gpu, days):
        return ContainerOccupancy(gpu_index=gpu, container_name=user, username=user, display_name=user,
                                  created_at=now - timedelta(days=days), expires_at=now + timedelta(days=1))

    node = DashboardNode(node_id="one", node_name="one", online=True, schedulable=True,
        gpus=[GPUInfo(index=0, name="0", utilization=100, memory_used_mb=50, memory_total_mb=100),
              GPUInfo(index=1, name="1", utilization=50), GPUInfo(index=2, name="2")],
        occupancies=[occupancy("alice", 0, 10), occupancy("alice", 0, 2), occupancy("bob", 0, 1),
                     occupancy("alice", 1, 1), occupancy("bob", 2, 1)])
    week = dashboard._estimated_gpu_ranking([node], now, 7)
    assert [(row.username, row.estimated_percent) for row in week] == [("bob", 42.5), ("alice", 41.6)]
    month = dashboard._estimated_gpu_ranking([node], now, 30)
    assert [(row.username, row.estimated_percent) for row in month] == [("bob", 42.5), ("alice", 41.8)]
    node.online = False
    assert dashboard._estimated_gpu_ranking([node], now, 7) == []


def test_dashboard_gpu_estimate_separates_master_and_worker_same_username(owner_db, monkeypatch):
    now = datetime.now()
    master_owner = verified_owners(owner_db, "worker-1", [
        {"container_id": "one", "name": "one", "username": "alice", "status": "running", "gpu_ids": "0"},
    ])[0]
    worker_owner = {**master_owner, "container_id": "worker-only", "container_name": "worker-only", "created_at": (now - timedelta(hours=24)).isoformat()}
    master_owner["created_at"] = worker_owner["created_at"]
    owner_db.delete(owner_db.query(ContainerModel).filter_by(container_id="one").one())
    owner_db.commit()
    inventory = {
        "gpus": [{"index": 0, "name": "GPU", "utilization": 100}, {"index": 1, "name": "GPU", "utilization": 20}],
        "containers": [
            {"container_id": "one", "name": "one", "username": "alice", "status": "running", "gpu_ids": "0"},
            {"container_id": "worker-only", "name": "worker-only", "username": "alice", "status": "running", "gpu_ids": "1"},
        ],
        "owners": [worker_owner],
        "system_load": {"cpu_percent": 1, "memory_used_gb": 1, "memory_total_gb": 10, "memory_percent": 10, "disk_free_gb": 5, "disk_total_gb": 10},
    }
    monkeypatch.setattr(dashboard, "NODE_ROLE", "worker")
    monkeypatch.setattr(dashboard, "NODE_ID", "worker-1")
    monkeypatch.setattr(dashboard, "MASTER_API_URL", "http://master")
    monkeypatch.setattr(dashboard, "AGENT_API_TOKEN", "token-1")
    monkeypatch.setattr(dashboard, "local_inventory", lambda _db: inventory)
    monkeypatch.setattr(dashboard, "get_gpu_info", lambda: inventory["gpus"])
    monkeypatch.setattr(dashboard, "get_system_load", lambda: inventory["system_load"])
    monkeypatch.setattr(dashboard, "get_setting", lambda *_: "4")
    monkeypatch.setattr(dashboard.RemoteAgentClient, "owners_for_worker", lambda *_: {"node_id": "worker-1", "owners": [master_owner]})
    result = dashboard.get_dashboard(db=owner_db, _=None)
    assert {(row.origin, row.container_name) for row in result.nodes[0].occupancies} == {("master", "one"), ("worker-1", "worker-only")}
    assert [(row.username, row.real_name, row.estimated_percent) for row in result.weekly_gpu_ranking] == [
        ("alice", "Alice Real (master: alice)", 70.0),
        ("alice", "Alice Real (worker-1: alice)", 14.0),
    ]
    assert len(result.monthly_gpu_ranking) == 2


def test_dashboard_gpu_estimate_excludes_outside_window_and_future():
    from app.models import ContainerOccupancy, DashboardNode, GPUInfo
    now = datetime(2026, 9, 29, 12)
    node = DashboardNode(node_id="one", node_name="one", online=True, schedulable=True,
        gpus=[GPUInfo(index=0, name="0", utilization=80)],
        occupancies=[ContainerOccupancy(gpu_index=0, container_name=user, username=user, display_name=user,
                     created_at=created, expires_at=now + timedelta(days=1)) for user, created in
                     [("old", now - timedelta(days=31)), ("future", now + timedelta(hours=1))]])
    assert [(row.username, row.estimated_percent) for row in dashboard._estimated_gpu_ranking([node], now, 7)] == [("old", 56.0)]
    node.occupancies = node.occupancies[1:]
    assert dashboard._estimated_gpu_ranking([node], now, 7) == []


def test_dashboard_authenticated_response_hides_worker_local_identity(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserModel(username="viewer", hashed_password="x", role="user")
        db.add(user)
        db.commit()
        db.add_all([
            ContainerModel(
                container_id="master-id", name="master-container", user_id=user.id,
                node_id="worker-1", gpu_ids="0", ssh_port=22001, status="running",
                expires_at=datetime.now() + timedelta(days=1),
            ),
            ContainerModel(
                container_id="stopped-id", name="stopped-container", user_id=user.id,
                node_id="worker-1", gpu_ids="2", ssh_port=22002, status="stopped",
                expires_at=datetime.now() + timedelta(days=1),
            ),
        ])
        db.commit()
        inventory = {
            "gpus": [{"index": index, "name": "GPU"} for index in range(4)],
            "containers": [
                {"container_id": "master-id", "name": "master-container", "username": "viewer", "status": "running", "gpu_ids": "0"},
                {"container_id": "worker-only", "name": "private-worker-name", "username": "private-worker-user", "status": "running", "gpu_ids": "1"},
                {"container_id": "stopped-id", "name": "stopped-container", "status": "stopped", "gpu_ids": "2"},
            ],
            "system_load": {"cpu_percent": 1, "memory_used_gb": 1, "memory_total_gb": 10, "memory_percent": 10, "disk_free_gb": 5, "disk_total_gb": 10},
        }
        monkeypatch.setattr(dashboard, "NODE_ROLE", "master")
        monkeypatch.setattr(dashboard, "get_gpu_info", lambda: [])
        monkeypatch.setattr(dashboard, "get_system_load", lambda: inventory["system_load"])
        monkeypatch.setattr(dashboard, "get_setting", lambda *_args: "4")
        monkeypatch.setattr(dashboard, "aggregate_inventories", lambda _db: [{
            "node": {"id": "worker-1", "name": "Worker 1", "public_host": "worker", "schedulable": True, "is_local": False},
            "online": True, "inventory": inventory,
        }])
        app = FastAPI()
        app.include_router(dashboard.router, prefix="/api/dashboard")
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as client:
            assert client.get("/api/dashboard").status_code == 401
            response = client.get("/api/dashboard", headers={"Authorization": f"Bearer {create_access_token({'sub': 'viewer'})}"})
        assert response.status_code == 200
        payload = response.json()
        rows = payload["nodes"][0]["gpu_sharing"]
        assert [(row["external_occupied"], row["occupant_count"], row["selectable"]) for row in rows] == [
            (False, 1, True), (True, 1, False), (False, 0, True), (False, 0, True),
        ]
        assert [row["container_name"] for row in payload["nodes"][0]["occupancies"]] == ["master-container"]
        assert "private-worker-name" not in response.text
        assert "private-worker-user" not in response.text
        assert "worker-only" not in response.text
    engine.dispose()


@pytest.mark.parametrize("case,expected", [
    ("eligible", [True, True, False]),
    ("full", [False, False, False]),
    ("unknown", [False, False, False]),
    ("merge-old", [False, False, False]),
    ("mixed", [False, False, False]),
    ("master-only", [False, False, False]),
    ("invalid-owners", [False, False, False]),
    ("invalid-occupancy", [False, False, False]),
    ("offline", [False, False, False]),
    ("unschedulable", [False, False, False]),
    ("local", [False, False, False]),
])
def test_dashboard_worker_shareable_requires_worker_only_verified_capacity(owner_db, monkeypatch, case, expected):
    expires = (datetime.now() + timedelta(days=1)).isoformat()
    runtime = [{"container_id": "private", "name": "private", "username": "worker-user",
                "status": "running", "gpu_ids": "4,5"}]
    owners = [{"container_id": "private", "container_name": "private", "username": "worker-user",
               "gpu_ids": "4,5", "expires_at": expires}]
    if case == "full":
        runtime.append({"container_id": "second", "name": "second", "username": "another",
                        "status": "running", "gpu_ids": "4,5"})
        owners.append({"container_id": "second", "container_name": "second", "username": "another",
                       "gpu_ids": "4,5", "expires_at": expires})
    if case == "unknown":
        owners.clear()
    if case == "merge-old":
        runtime.append({"container_id": "old", "name": "private-merge-old", "status": "stopped", "gpu_ids": "4,5"})
    if case in {"mixed", "master-only"}:
        owner_db.add(ContainerModel(container_id="master-extra", name="master-extra", node_id="worker-1",
            user_id=1, status="running", gpu_ids="4,5", ssh_port=22007,
            expires_at=datetime.now() + timedelta(days=1)))
        owner_db.commit()
        if case == "master-only":
            runtime.clear()
            owners.clear()
        runtime.append({"container_id": "master-extra", "name": "master-extra", "username": "alice", "status": "running", "gpu_ids": "4,5"})
    if case == "invalid-owners":
        owners.clear()
    if case == "invalid-occupancy":
        owners[0]["expires_at"] = "invalid"
    inventory = {"gpus": [{"index": i, "name": "GPU"} for i in (4, 5, 6)],
                 "containers": runtime, "owners": owners,
                 "system_load": {"cpu_percent": 1, "memory_used_gb": 1, "memory_total_gb": 10,
                                 "memory_percent": 10, "disk_free_gb": 5, "disk_total_gb": 10}}
    node = {"id": "worker-1", "name": "Worker", "public_host": "worker", "schedulable": case != "unschedulable",
            "is_local": case == "local"}
    monkeypatch.setattr(dashboard, "NODE_ROLE", "master")
    monkeypatch.setattr(dashboard, "NODE_ID", "master")
    monkeypatch.setattr(dashboard, "get_gpu_info", lambda: [])
    monkeypatch.setattr(dashboard, "get_system_load", lambda: inventory["system_load"])
    monkeypatch.setattr(dashboard, "get_setting", lambda *_: "2")
    monkeypatch.setattr(dashboard, "aggregate_inventories", lambda _db: [{"node": node,
        "online": case != "offline", "inventory": inventory}])
    result = dashboard.get_dashboard(db=owner_db, _=None)
    rows = result.nodes[0].gpu_sharing
    assert [row.worker_shareable for row in rows] == expected
    assert rows[0].external_occupied is (case != "master-only")
    assert rows[0].selectable is (case == "master-only")
    assert result.gpu_sharing == ([] if case != "local" else rows)
    if case == "eligible":
        assert rows[0].worker_shareable and rows[1].worker_shareable
        assert not rows[2].worker_shareable
        assert "worker-user" not in str([row.model_dump() for row in rows])


def test_worker_shareable_single_gpu_does_not_approve_mixed_multi_gpu_selection(owner_db):
    from app.node_service import worker_share_snapshot
    owner_db.add(ContainerModel(container_id="master-on-one", name="master-on-one", node_id="worker-1",
        user_id=1, status="running", gpu_ids="1", ssh_port=22007,
        expires_at=datetime.now() + timedelta(days=1)))
    owner_db.commit()
    inventory = {"gpus": [{"index": 4}, {"index": 1}], "containers": [
        {"container_id": "private", "name": "private", "username": "worker-user", "gpu_ids": "4", "status": "running"},
        {"container_id": "master-on-one", "name": "master-on-one", "username": "alice", "gpu_ids": "1", "status": "running"},
    ], "owners": [{"container_id": "private", "container_name": "private", "username": "worker-user",
                  "gpu_ids": "4", "expires_at": (datetime.now() + timedelta(days=1)).isoformat()}]}
    assert worker_share_snapshot(owner_db, "worker-1", inventory, [4])
    with pytest.raises(ValueError, match="Master 占用"):
        worker_share_snapshot(owner_db, "worker-1", inventory, [4, 1])


def test_cpu_auto_placement_ignores_external_gpu_occupancy(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(ComputeNodeModel(id="worker-1", name="Worker 1", base_url="http://worker", public_host="worker", agent_token="x"))
        db.commit()
        monkeypatch.setattr("app.node_service.inventory_for_node", lambda _db, _node: {
            "gpus": [{"index": 0}],
            "containers": [{"status": "running", "gpu_ids": "0", "container_id": "worker-only"}],
            "system_load": {"memory_percent": 10, "cpu_percent": 10},
        })
        node, _ = select_node(db, "auto", None, [], True, applicant_id=8, max_share=4)
        assert node.id == "worker-1"
    engine.dispose()


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
    Base.metadata.create_all(engine)
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


def test_auto_placement_rejects_unknown_local_gpu_even_when_capacity_remains(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
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
        with pytest.raises(ValueError, match="没有在线且资源满足要求"):
            select_node(db, "auto", None, [0], False, applicant_id=8, max_share=2)


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


@pytest.fixture
def owner_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([
            UserModel(id=1, username="alice", display_name="Alice", real_name="Alice Real", contact_type="wechat", contact_value="alice-id", hashed_password="x"),
            UserModel(id=2, username="bob", display_name="Bob", real_name="Bob Real", contact_type="phone", contact_value="123456", hashed_password="x"),
            ComputeNodeModel(id="worker-1", name="Worker", base_url="http://worker", agent_token="token-1"),
            ComputeNodeModel(id="worker-2", name="Other", base_url="http://other", agent_token="token-2"),
        ])
        db.commit()
        for cid, node, user, status in [("one", "worker-1", 1, "running"), ("two", "worker-1", 2, "running"), ("other", "worker-2", 2, "running"), ("stale", "worker-1", 2, "stopped")]:
            db.add(ContainerModel(container_id=cid, name=cid, node_id=node, user_id=user, status=status, gpu_ids="0", ssh_port=22000, expires_at=datetime.now() + timedelta(days=1)))
        db.commit()
        yield db
    engine.dispose()


def test_verified_owner_requires_running_matching_runtime_and_node(owner_db):
    runtime = [{"container_id": cid, "name": name, "username": username, "status": status, "gpu_ids": "0"} for cid, name, username, status in [
        ("one", "one", "alice", "running"), ("two", "two", "alice", "running"),
        ("stale", "stale", "bob", "running"), ("other", "other", "bob", "running"),
        ("unknown", "unknown", "bob", "running"),
    ]]
    owners = verified_owners(owner_db, "worker-1", runtime)
    assert [(row["container_id"], row["real_name"], row["contact_value"]) for row in owners] == [("one", "Alice Real", "alice-id")]
    runtime[0]["status"] = "stopped"
    assert verified_owners(owner_db, "worker-1", runtime) == []
    for username in ("", None, "bob"):
        runtime[0]["status"] = "running"
        runtime[0]["username"] = username
        assert verified_owners(owner_db, "worker-1", runtime) == []
    runtime[0]["username"] = "alice"
    runtime[0]["name"] = "one-merge-old"
    assert verified_owners(owner_db, "worker-1", runtime) == []


def test_master_feed_scoped_to_enabled_node_token(owner_db, monkeypatch):
    monkeypatch.setattr(agent, "NODE_ROLE", "master")
    def unexpected_inventory(*_args):
        raise AssertionError("owner feed must not request worker inventory")
    monkeypatch.setattr("app.node_service.inventory_for_node", unexpected_inventory)
    app = FastAPI()
    app.include_router(agent.router, prefix="/api/agent/v1")
    app.dependency_overrides[get_db] = lambda: owner_db
    with TestClient(app) as client:
        path = "/api/agent/v1/owners/worker-1"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer token-2"}).status_code == 401
        response = client.get(path, headers={"Authorization": "Bearer token-1"})
        assert [row["container_id"] for row in response.json()["owners"]] == ["one", "two"]
        owner_db.get(ComputeNodeModel, "worker-1").enabled = False
        owner_db.commit()
        assert client.get(path, headers={"Authorization": "Bearer token-1"}).status_code == 401


@pytest.mark.parametrize("role", ["master", "worker"])
def test_dashboard_reconciles_cross_database_owners(owner_db, monkeypatch, role):
    owner_db.get(UserModel, 1).disk_usage_bytes = 1024
    owner_db.get(UserModel, 1).disk_usage_scan_complete = True
    owner_db.commit()
    runtime = [
        {"container_id": "one", "name": "one", "username": "alice", "status": "running", "gpu_ids": "0"},
        {"container_id": "two", "name": "two", "username": "bob", "status": "running", "gpu_ids": "1"},
        {"container_id": "stale", "name": "stale", "username": "bob", "status": "stopped", "gpu_ids": "2"},
        {"container_id": "unknown", "name": "unknown", "status": "running", "gpu_ids": "3"},
    ]
    inventory = {"gpus": [{"index": i, "name": "GPU"} for i in range(4)], "containers": runtime,
                 "system_load": {"cpu_percent": 1, "memory_used_gb": 1, "memory_total_gb": 10, "memory_percent": 10, "disk_free_gb": 5, "disk_total_gb": 10}}
    monkeypatch.setattr(dashboard, "NODE_ROLE", role)
    monkeypatch.setattr(dashboard, "NODE_ID", "worker-1")
    monkeypatch.setattr(dashboard, "get_gpu_info", lambda: inventory["gpus"])
    monkeypatch.setattr(dashboard, "get_system_load", lambda: inventory["system_load"])
    monkeypatch.setattr(dashboard, "get_setting", lambda *_: "4")
    if role == "master":
        inventory["owners"] = verified_owners(owner_db, "worker-1", runtime)
        monkeypatch.setattr(dashboard, "aggregate_inventories", lambda _db: [{"node": {"id": "worker-1", "name": "Worker", "public_host": "worker", "schedulable": True, "is_local": False}, "online": True, "inventory": inventory}])
    else:
        monkeypatch.setattr(dashboard, "local_inventory", lambda _db: inventory)
        monkeypatch.setattr(dashboard, "MASTER_API_URL", "http://master")
        monkeypatch.setattr(dashboard, "AGENT_API_TOKEN", "token-1")
        monkeypatch.setattr(dashboard.RemoteAgentClient, "owners_for_worker", lambda _client, _node: {"node_id": "worker-1", "owners": verified_owners(owner_db, "worker-1", runtime)})
    app = FastAPI()
    app.include_router(dashboard.router, prefix="/api/dashboard")
    app.dependency_overrides[get_db] = lambda: owner_db
    with TestClient(app) as client:
        response = client.get("/api/dashboard", headers={"Authorization": f"Bearer {create_access_token({'sub': 'alice'})}"})
    assert response.status_code == 200
    payload = response.json()
    assert {(row["container_name"], row["real_name"], row["contact_value"]) for row in payload["all_containers"]} == {("one", "Alice Real", "alice-id"), ("two", "Bob Real", "123456")}
    assert len(payload["nodes"][0]["occupancies"]) == 2
    assert [(row["username"], row["usage_bytes"]) for row in payload["disk_ranking"]] == [("alice", 1024)]
    assert payload["weekly_gpu_ranking"] == []
    assert payload["monthly_gpu_ranking"] == []
    assert payload["nodes"][0]["gpu_sharing"][3]["external_occupied"] is True
    assert "unknown" not in str(payload["all_containers"])


def test_worker_dashboard_failed_feed_preserves_unknown_occupancy(owner_db, monkeypatch):
    from app.remote_agent import RemoteAgentError
    runtime = [{"container_id": "master-only", "name": "master-only", "status": "running", "gpu_ids": "2"}]
    inventory = {"gpus": [{"index": 2, "name": "GPU"}], "containers": runtime,
                 "system_load": {"cpu_percent": 1, "memory_used_gb": 1, "memory_total_gb": 10, "memory_percent": 10, "disk_free_gb": 5, "disk_total_gb": 10}}
    monkeypatch.setattr(dashboard, "NODE_ROLE", "worker")
    monkeypatch.setattr(dashboard, "NODE_ID", "worker-1")
    monkeypatch.setattr(dashboard, "MASTER_API_URL", "http://master")
    monkeypatch.setattr(dashboard, "AGENT_API_TOKEN", "token-1")
    monkeypatch.setattr(dashboard, "local_inventory", lambda _db: inventory)
    monkeypatch.setattr(dashboard, "get_gpu_info", lambda: inventory["gpus"])
    monkeypatch.setattr(dashboard, "get_system_load", lambda: inventory["system_load"])
    monkeypatch.setattr(dashboard, "get_setting", lambda *_: "4")
    def unavailable(_client, _node):
        raise RemoteAgentError("offline")
    monkeypatch.setattr(dashboard.RemoteAgentClient, "owners_for_worker", unavailable)
    app = FastAPI()
    app.include_router(dashboard.router, prefix="/api/dashboard")
    app.dependency_overrides[get_db] = lambda: owner_db
    with TestClient(app) as client:
        response = client.get("/api/dashboard", headers={"Authorization": f"Bearer {create_access_token({'sub': 'alice'})}"})
    assert response.status_code == 200
    assert response.json()["all_containers"] == []
    assert response.json()["gpu_sharing"][0]["external_occupied"] is True


@pytest.mark.parametrize("role", ["master", "worker"])
def test_dashboard_rejects_conflicting_inventory_identity(owner_db, monkeypatch, role):
    inventory = {
        "gpus": [{"index": 0, "name": "GPU"}],
        "containers": [{"container_id": "one", "name": "one", "username": "bob", "status": "running", "gpu_ids": "0"}],
        "owners": [{"container_id": "one", "container_name": "one", "username": "bob", "expires_at": (datetime.now() + timedelta(days=1)).isoformat()}],
        "system_load": {"cpu_percent": 1, "memory_used_gb": 1, "memory_total_gb": 10, "memory_percent": 10, "disk_free_gb": 5, "disk_total_gb": 10},
    }
    monkeypatch.setattr(dashboard, "NODE_ROLE", role)
    monkeypatch.setattr(dashboard, "NODE_ID", "worker-1")
    monkeypatch.setattr(dashboard, "get_gpu_info", lambda: inventory["gpus"])
    monkeypatch.setattr(dashboard, "get_system_load", lambda: inventory["system_load"])
    monkeypatch.setattr(dashboard, "get_setting", lambda *_: "4")
    if role == "master":
        monkeypatch.setattr(dashboard, "aggregate_inventories", lambda _db: [{"node": {"id": "worker-1", "name": "Worker", "public_host": "worker", "schedulable": True, "is_local": False}, "online": True, "inventory": inventory}])
    else:
        monkeypatch.setattr(dashboard, "local_inventory", lambda _db: inventory)
        monkeypatch.setattr(dashboard, "MASTER_API_URL", "http://master")
        monkeypatch.setattr(dashboard, "AGENT_API_TOKEN", "token-1")
        monkeypatch.setattr(dashboard.RemoteAgentClient, "owners_for_worker", lambda *_: {"node_id": "worker-1", "owners": inventory["owners"]})
    result = dashboard.get_dashboard(db=owner_db, _=None)
    assert result.all_containers == []
    assert result.nodes[0].gpu_sharing[0].unknown_occupant_count == 1


@pytest.mark.parametrize("role,expected", [("master", "Alice Real"), ("worker", "Alice Real")])
def test_dashboard_owner_source_precedence(owner_db, monkeypatch, role, expected):
    runtime = [{"container_id": "one", "name": "one", "username": "alice", "status": "running", "gpu_ids": "0"}]
    master_owner = verified_owners(owner_db, "worker-1", runtime)[0]
    worker_owner = {**master_owner, "real_name": "Worker Real", "contact_value": "worker-contact"}
    inventory = {
        "gpus": [{"index": 0, "name": "GPU"}], "containers": runtime, "owners": [worker_owner],
        "system_load": {"cpu_percent": 1, "memory_used_gb": 1, "memory_total_gb": 10, "memory_percent": 10, "disk_free_gb": 5, "disk_total_gb": 10},
    }
    monkeypatch.setattr(dashboard, "NODE_ROLE", role)
    monkeypatch.setattr(dashboard, "NODE_ID", "worker-1")
    monkeypatch.setattr(dashboard, "get_gpu_info", lambda: inventory["gpus"])
    monkeypatch.setattr(dashboard, "get_system_load", lambda: inventory["system_load"])
    monkeypatch.setattr(dashboard, "get_setting", lambda *_: "4")
    if role == "master":
        monkeypatch.setattr(dashboard, "aggregate_inventories", lambda _db: [{"node": {"id": "worker-1", "name": "Worker", "public_host": "worker", "schedulable": True, "is_local": False}, "online": True, "inventory": inventory}])
    else:
        monkeypatch.setattr(dashboard, "local_inventory", lambda _db: inventory)
        monkeypatch.setattr(dashboard, "MASTER_API_URL", "http://master")
        monkeypatch.setattr(dashboard, "AGENT_API_TOKEN", "token-1")
        monkeypatch.setattr(dashboard.RemoteAgentClient, "owners_for_worker", lambda *_: {"node_id": "worker-1", "owners": [master_owner]})
    result = dashboard.get_dashboard(db=owner_db, _=None)
    assert [row.real_name for row in result.all_containers] == [expected]
    assert result.nodes[0].gpu_sharing[0].unknown_occupant_count == 0


def test_dashboard_mixed_verified_and_unknown_occupancy(owner_db):
    inventory = {
        "gpus": [{"index": 0}],
        "containers": [
            {"container_id": "one", "name": "one", "username": "alice", "status": "running", "gpu_ids": "0"},
            {"container_id": "external", "name": "private", "username": "bob", "status": "running", "gpu_ids": "0"},
            {"container_id": "old", "name": "old-merge-old", "status": "stopped", "gpu_ids": "0"},
        ],
    }
    owners = verified_owners(owner_db, "worker-1", inventory["containers"])
    row = dashboard._node_gpu_sharing(inventory, owners, "worker-1", 4, {"one"})[0]
    assert (row.occupant_count, row.unknown_occupant_count, row.external_occupied, row.selectable) == (3, 2, True, False)


def test_scheduler_and_dashboard_require_matching_username(owner_db, monkeypatch):
    from app.node_service import _known_runtime_ids
    monkeypatch.setattr("app.node_service.NODE_ID", "worker-1")
    for username in ("", "bob"):
        inventory = {"gpus": [{"index": 0}], "containers": [{"container_id": "one", "name": "one", "username": username, "status": "running", "gpu_ids": "0"}]}
        assert _known_runtime_ids(owner_db, "worker-1", inventory) == set()
        assert dashboard._node_gpu_sharing(inventory, [], "worker-1", 4)[0].selectable is False
        monkeypatch.setattr("app.node_service.inventory_for_node", lambda _db, _node: inventory)
        with pytest.raises(ValueError, match="未知占用"):
            select_node(owner_db, "specific", "worker-1", [0], False, applicant_id=2, max_share=4)


def test_local_scheduler_blocks_external_runtime_even_with_sharing(owner_db, monkeypatch):
    monkeypatch.setattr("app.node_service.NODE_ID", "worker-1")
    monkeypatch.setattr("app.node_service.inventory_for_node", lambda _db, _node: {"gpus": [{"index": 0}, {"index": 1}], "containers": [{"container_id": "external-master", "status": "running", "gpu_ids": "0"}], "system_load": {"memory_percent": 1, "cpu_percent": 1}})
    with pytest.raises(ValueError, match="未知占用"):
        select_node(owner_db, "local", None, [0], False, applicant_id=1, max_share=4)
    node, _ = select_node(owner_db, "local", None, [1], False, applicant_id=1, max_share=4)
    assert node.id == "worker-1"


@pytest.mark.parametrize("role", ["master", "worker"])
def test_dashboard_remote_display_owners_remain_external(owner_db, monkeypatch, role):
    owner_db.add(ContainerModel(
        container_id="one-extra", name="one-extra", node_id="worker-1", user_id=1,
        status="running", gpu_ids="0", ssh_port=22005, expires_at=datetime.now() + timedelta(days=1),
    ))
    owner_db.commit()
    runtime = [
        {"container_id": "one", "name": "one", "username": "alice", "status": "running", "gpu_ids": "0"},
        {"container_id": "one-extra", "name": "one-extra", "username": "alice", "status": "running", "gpu_ids": "0"},
        {"container_id": "remote", "name": "remote", "username": "alice", "status": "running", "gpu_ids": "0,1,2"},
        {"container_id": "unverified", "name": "unverified", "status": "running", "gpu_ids": "2"},
    ]
    remote_owner = {
        "container_id": "remote", "container_name": "remote", "username": "alice",
        "real_name": "Remote Alice", "contact_value": "remote-contact",
        "expires_at": (datetime.now() + timedelta(days=1)).isoformat(),
    }
    inventory = {
        "gpus": [{"index": i, "name": "GPU"} for i in range(4)],
        "containers": runtime,
        "owners": [remote_owner, remote_owner] if role == "master" else [],
        "system_load": {"cpu_percent": 1, "memory_used_gb": 1, "memory_total_gb": 10, "memory_percent": 10, "disk_free_gb": 5, "disk_total_gb": 10},
    }
    monkeypatch.setattr(dashboard, "NODE_ROLE", role)
    monkeypatch.setattr(dashboard, "NODE_ID", "master" if role == "master" else "worker-1")
    monkeypatch.setattr(dashboard, "get_gpu_info", lambda: inventory["gpus"])
    monkeypatch.setattr(dashboard, "get_system_load", lambda: inventory["system_load"])
    monkeypatch.setattr(dashboard, "get_setting", lambda *_: "4")
    if role == "master":
        monkeypatch.setattr(dashboard, "aggregate_inventories", lambda _db: [{
            "node": {"id": "worker-1", "name": "Worker", "public_host": "worker", "schedulable": True, "is_local": False},
            "online": True, "inventory": inventory,
        }])
    else:
        monkeypatch.setattr(dashboard, "local_inventory", lambda _db: inventory)
        monkeypatch.setattr(dashboard, "MASTER_API_URL", "http://master")
        monkeypatch.setattr(dashboard, "AGENT_API_TOKEN", "token-1")
        monkeypatch.setattr(dashboard.RemoteAgentClient, "owners_for_worker", lambda *_: {"node_id": "worker-1", "owners": [remote_owner, remote_owner]})
    result = dashboard.get_dashboard(db=owner_db, _=None)
    assert [(row.occupant_count, row.unknown_occupant_count, row.external_occupied, row.selectable) for row in result.nodes[0].gpu_sharing] == [
        (2, 0, True, False),
        (1, 0, True, False),
        (2, 1, True, False),
        (0, 0, False, True),
    ]
    assert [(row.container_name, row.real_name, row.contact_value) for row in result.all_containers if row.container_name == "remote"] == [
        ("remote", "Remote Alice", "remote-contact"),
    ]


def test_reminder_ranking_empty_and_authenticated(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(UserModel(username="alice", real_name="Alice", hashed_password="x", approved=1))
    db.commit()
    monkeypatch.setattr(dashboard, "NODE_ROLE", "master")
    app = FastAPI()
    app.include_router(dashboard.router, prefix="/api/dashboard")
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(dashboard, "get_gpu_info", lambda: [])
    monkeypatch.setattr(dashboard, "get_system_load", lambda: {"cpu_percent": 0, "memory_used_gb": 0, "memory_total_gb": 1, "memory_percent": 0, "disk_free_gb": 1, "disk_total_gb": 1})
    monkeypatch.setattr(dashboard, "aggregate_inventories", lambda _: [])
    monkeypatch.setattr(dashboard, "get_setting", lambda *_: "4")
    with TestClient(app) as client:
        assert client.get("/api/dashboard").status_code == 401
        response = client.get("/api/dashboard", headers={"Authorization": f"Bearer {create_access_token({'sub': 'alice'})}"})
        assert response.status_code == 200
        assert response.json()["reminder_ranking"] == []
    db.close()
    engine.dispose()


def test_reminder_ranking_counts_and_order(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([UserModel(id=i, username=f"user{i:02}", real_name=f"Name {i}", hashed_password="x") for i in range(1, 24)])
        now = datetime.now()
        def container(name, user_id, status, expires, approvers=None):
            return ContainerModel(name=name, user_id=user_id, status=status, ssh_port=22000, expires_at=expires,
                                  pending_share_json=json.dumps({"approvers": approvers}) if approvers is not None else None)
        db.add_all([
            container("pending", 1, "pending_share_approval", now + timedelta(days=3), [{"user_id": 2, "approved": False}, {"user_id": 3, "approved": True}]),
            container("expired", 2, "running", now - timedelta(hours=1)),
            container("soon", 2, "running", now + timedelta(hours=2)),
            container("later", 3, "running", now + timedelta(days=2)),
            *[container(f"other{i}", i, "running", now + timedelta(hours=1)) for i in range(4, 24)],
        ])
        db.commit()
        monkeypatch.setattr(dashboard, "NODE_ROLE", "master")
        ranked = dashboard._reminder_ranking(db, now)
        assert len(ranked) == 20
        assert (ranked[0].username, ranked[0].unread_count) == ("user02", 2)
        assert ranked[1].username == "user01"
        assert all(row.username != "user03" for row in ranked)
        assert set(ranked[0].model_dump()) == {"username", "real_name", "unread_count"}
    engine.dispose()


@pytest.mark.parametrize("terminal_state", ["rejected", "cancelled", "expired"])
def test_master_reminder_skips_stale_remote_pending_without_writing(monkeypatch, terminal_state):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(UserModel(id=1, username="applicant", hashed_password="x"))
        db.add(ComputeNodeModel(id="worker-1", name="Worker", base_url="http://worker.example", agent_token="secret"))
        db.add(ContainerModel(id=1, name="remote-pending", user_id=1, node_id="worker-1", status="pending_share_approval",
                              ssh_port=0, expires_at=datetime.now() + timedelta(days=3650),
                              pending_share_json=json.dumps({"request_id": "request-1", "occupancy": [{"container_id": "owner-1"}],
                                                             "approvers": []})))
        db.commit()
        monkeypatch.setattr(dashboard, "NODE_ROLE", "master")
        monkeypatch.setattr(dashboard, "_remote_reminder_cache", {})
        clock = [100.0]
        monkeypatch.setattr(dashboard, "monotonic", lambda: clock[0])
        remote = {"state": "pending", "occupancy": [{"container_id": "owner-1"}], "calls": 0}

        def status(_, request_id):
            remote["calls"] += 1
            return {"request_id": request_id, "state": remote["state"], "occupancy": remote["occupancy"]}

        monkeypatch.setattr(containers.RemoteAgentClient, "share_status", status)
        assert [(r.username, r.unread_count) for r in dashboard._reminder_ranking(db, datetime.now())] == [("applicant", 1)]
        assert remote["calls"] == 1
        remote["state"] = terminal_state
        assert dashboard._reminder_ranking(db, datetime.now())[0].unread_count == 1
        assert remote["calls"] == 1
        clock[0] += 31
        assert dashboard._reminder_ranking(db, datetime.now()) == []
        assert db.get(ContainerModel, 1).status == "pending_share_approval"
        assert remote["calls"] == 2
        remote["state"] = "pending"
        remote["occupancy"] = [{"container_id": "other"}]
        clock[0] += 31
        assert dashboard._reminder_ranking(db, datetime.now()) == []
        remote["occupancy"] = [{"container_id": "owner-1"}]
        monkeypatch.setattr(containers.RemoteAgentClient, "share_status", lambda *_: {"request_id": "different", "state": "pending", "occupancy": remote["occupancy"]})
        clock[0] += 31
        assert dashboard._reminder_ranking(db, datetime.now()) == []
    engine.dispose()


def test_worker_reminder_only_counts_current_remote_share(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(UserModel(id=1, username="owner", hashed_password="x"))
        for name, expires in [("valid", datetime.now() + timedelta(hours=1)), ("stale", datetime.now() - timedelta(hours=1))]:
            db.add(ShareRequestModel(id=name, state="pending", expires_at=expires,
                                     approvers=json.dumps([{"user_id": 1, "approved": False}]), payload="{}"))
        db.commit()
        monkeypatch.setattr(dashboard, "NODE_ROLE", "worker")
        calls = []
        def current(_, row):
            calls.append(row.id)
            if row.id == "stale":
                raise HTTPException(status_code=409)
            return {}
        monkeypatch.setattr(containers, "require_remote_share_current", current)
        assert [(r.username, r.unread_count) for r in dashboard._reminder_ranking(db, datetime.now())] == [("owner", 1)]
        assert sorted(calls) == ["stale", "valid"]
    engine.dispose()
