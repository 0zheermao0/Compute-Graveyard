from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scheduler
from app.config import NODE_ID
from app.database import Base
from app.database_models import ComputeNodeModel, ContainerModel, GPUHistorySampleModel, SystemSettings, UserModel
from app.gpu_history import clear_container_history, collect_history, history_response, resize_container_history
from app.container_lifecycle import RemovalResult, remove_container_record
from app.remote_agent import RemoteAgentError


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(ComputeNodeModel(id=NODE_ID, name="Local"))
        db.commit()
    yield factory
    engine.dispose()


def inventory():
    return {"gpus": [{"index": 0, "name": "A100", "utilization": 60,
                      "memory_used_mb": 1024, "memory_total_mb": 4096}],
            "containers": [{"container_id": name, "name": name, "username": name,
                            "status": "running", "gpu_ids": "0"} for name in ("alice", "bob")],
            "owners": [{"container_id": name, "container_name": name, "username": name,
                        "display_name": name, "node_id": NODE_ID} for name in ("alice", "bob")]}


def test_shared_gpu_single_sample_and_deduplication(sessions):
    now = datetime.now().replace(second=0, microsecond=0)
    loader = Mock(return_value=inventory())
    with sessions() as db:
        snapshots = collect_history(db, loader, now)
        collect_history(db, loader, now)
        assert snapshots[NODE_ID]["gpus"][0]["utilization"] == 60
        assert db.query(GPUHistorySampleModel).count() == 1
        response = history_response(db, now)
        assert len(response["series"]) == 2
        a, b = response["series"]
        assert a["points"] == b["points"]
        assert a["points"][0]["memory_percent"] == 25
        assert datetime.fromisoformat(a["points"][0]["timestamp"]).tzinfo is not None


def test_missing_metrics_and_invalid_owners(sessions):
    now = datetime.now()
    data = inventory()
    data["owners"][0]["container_name"] = "spoofed"
    data["gpus"][0].update(utilization=None, memory_used_mb=None)
    with sessions() as db:
        collect_history(db, Mock(return_value=data), now)
        response = history_response(db, now)
        assert [item["username"] for item in response["series"]] == ["bob"]
        point = response["series"][0]["points"][0]
        assert point["utilization"] is None
        assert point["memory_percent"] is None


def test_history_retention_and_offline_gap(sessions):
    now = datetime.now()
    with sessions() as db:
        collect_history(db, Mock(return_value=inventory()), now - timedelta(hours=26))
        collect_history(db, Mock(return_value=inventory()), now - timedelta(minutes=10))
        result = collect_history(db, Mock(side_effect=RemoteAgentError("offline")), now)
        assert result[NODE_ID] is None
        assert db.query(GPUHistorySampleModel).count() == 1
        assert len(history_response(db, now)["series"][0]["points"]) == 1


def test_sampling_continues_when_reclaim_disabled(sessions, monkeypatch):
    with sessions() as db:
        db.add(SystemSettings(key="idle_gpu_reclaim_enabled", value="false"))
        db.commit()
    loader = Mock(return_value=inventory())
    monkeypatch.setattr(scheduler, "SessionLocal", sessions)
    monkeypatch.setattr(scheduler, "inventory_for_node", loader)
    scheduler._reclaim_idle_gpu_containers()
    loader.assert_called_once()
    with sessions() as db:
        assert db.query(GPUHistorySampleModel).count() == 1


def test_reclaim_receives_same_inventory(sessions, monkeypatch):
    loader = Mock(return_value=inventory())
    reclaim = Mock()
    monkeypatch.setattr(scheduler, "SessionLocal", sessions)
    monkeypatch.setattr(scheduler, "inventory_for_node", loader)
    monkeypatch.setattr(scheduler, "_reclaim_idle_gpu_containers_locked", reclaim)
    scheduler._reclaim_idle_gpu_containers()
    loader.assert_called_once()
    assert reclaim.call_args.args[0][NODE_ID] == inventory()


def add_container(db, name="alice"):
    user = UserModel(username=name, hashed_password="unused")
    db.add(user)
    db.flush()
    container = ContainerModel(name=name, container_id=name, user_id=user.id,
                               node_id=NODE_ID, gpu_ids="0", ssh_port=22001,
                               status="running", expires_at=datetime.now() + timedelta(days=1))
    db.add(container)
    db.commit()
    return container


def test_release_immediately_clears_only_exiting_user(sessions):
    now = datetime.now()
    with sessions() as db:
        container = add_container(db)
        collect_history(db, Mock(return_value=inventory()), now)
        result = remove_container_record(db, container, "test release",
                                         docker_remover=lambda _: RemovalResult(success=True))
        assert result.success
        response = history_response(db, now)
        assert [item["username"] for item in response["series"]] == ["bob"]
        assert "alice" not in db.query(GPUHistorySampleModel).one().owners_json
        clear_container_history(db, docker_id="bob")
        db.commit()
        assert db.query(GPUHistorySampleModel).count() == 0
        assert history_response(db, now)["series"] == []


def test_failed_release_keeps_history(sessions):
    now = datetime.now()
    with sessions() as db:
        container = add_container(db)
        collect_history(db, Mock(return_value=inventory()), now)
        result = remove_container_record(db, container, "failed release",
                                         docker_remover=lambda _: RemovalResult(success=False))
        assert not result.success
        assert len(history_response(db, now)["series"]) == 2


def test_release_same_user_shared_card_preserves_other_allocation(sessions):
    now = datetime.now()
    data = inventory()
    data["containers"].append({"container_id": "alice-second", "name": "second",
                               "username": "alice", "status": "running", "gpu_ids": "0"})
    data["owners"].append({"container_id": "alice-second", "container_name": "second",
                           "username": "alice", "node_id": NODE_ID})
    with sessions() as db:
        container = add_container(db)
        db.add(ContainerModel(name="second", container_id="alice-second", user_id=container.user_id,
                              node_id=NODE_ID, gpu_ids="0", ssh_port=22002,
                              status="running", expires_at=container.expires_at))
        db.commit()
        collect_history(db, Mock(return_value=data), now)
        clear_container_history(db, container)
        db.commit()
        response = history_response(db, now)
        alice = next(item for item in response["series"] if item["username"] == "alice")
        assert alice["container_ids"] == ["alice-second"]
        assert len(response["series"]) == 2


def test_resize_clears_released_card_and_tracks_replacement(sessions):
    now = datetime.now()
    data = inventory()
    data["gpus"].append({**data["gpus"][0], "index": 1})
    data["containers"][0]["gpu_ids"] = "0,1"
    with sessions() as db:
        collect_history(db, Mock(return_value=data), now)
        resize_container_history(db, NODE_ID, "alice", "replacement", [0])
        db.commit()
        response = history_response(db, now)
        assert not any(item["username"] == "alice" and item["gpu_index"] == 1 for item in response["series"])
        clear_container_history(db, docker_id="replacement")
        db.commit()
        assert [item["username"] for item in history_response(db, now)["series"]] == ["bob"]


def test_external_release_reconciled_on_next_sample(sessions):
    now = datetime.now()
    with sessions() as db:
        collect_history(db, Mock(return_value=inventory()), now - timedelta(minutes=5))
        data = inventory()
        data["containers"] = data["containers"][1:]
        data["owners"] = data["owners"][1:]
        collect_history(db, Mock(return_value=data), now)
        assert [item["username"] for item in history_response(db, now)["series"]] == ["bob"]
