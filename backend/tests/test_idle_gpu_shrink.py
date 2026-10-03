"""Partial idle reclaim: independent evidence and durable replacement lifecycle."""
import json
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import scheduler
from app.api import containers
from app.database_models import UserNotificationModel
from app.settings_service import load_settings, idle_policy_signature, save_settings
from test_gpu_merge import state


def test_shrink_defaults_on_and_changes_policy_signature(state):
    db, user, other, target = state
    settings = load_settings(db)
    assert settings.idle_gpu_shrink_enabled is True
    disabled = replace(settings, idle_gpu_shrink_enabled=False)
    assert idle_policy_signature(disabled) != idle_policy_signature(settings)
    save_settings(db, disabled)
    assert load_settings(db).idle_gpu_shrink_enabled is False


def test_per_card_json_migrates_existing_database():
    from sqlalchemy import create_engine, text, inspect
    from app.database import _migrate_container_idle_reclaim
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE containers (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO containers (id) VALUES (1)"))
    _migrate_container_idle_reclaim(engine)
    _migrate_container_idle_reclaim(engine)
    columns = {row["name"]: row for row in inspect(engine).get_columns("containers")}
    assert str(columns["gpu_idle_cards_json"]["type"]) == "TEXT"
    assert columns["gpu_idle_cards_json"]["nullable"] is True
    with engine.connect() as conn:
        assert conn.execute(text("SELECT gpu_idle_cards_json FROM containers")).scalar() is None
    engine.dispose()


def metrics(idle=0):
    return {i: {"index": i, "utilization": 1 if i == idle else 80,
                "memory_percent": 1 if i == idle else 80, "memory_used_mb": 100}
            for i in (0, 1)}


def prepare(state):
    db, user, other, target = state
    target.gpu_ids = "0,1"
    db.commit()
    settings = replace(load_settings(db), idle_gpu_reclaim_enabled=True, idle_gpu_duration_hours=1)
    return db, user, target, settings


def observe(db, target, settings, start, end=65, metric_fn=metrics):
    snapshot = None
    for minute in range(0, end + 1, 5):
        snapshot = scheduler._observe_idle_cards(db, target, [0, 1], metric_fn(minute), settings,
            idle_policy_signature(settings), start + timedelta(minutes=minute), timedelta(hours=1))
        db.commit()
    return snapshot


def test_independent_windows_warn_and_mature(state):
    db, user, target, settings = prepare(state)
    snapshot = observe(db, target, settings, datetime.now(), metric_fn=lambda _: metrics())
    assert snapshot["due"] == [0]
    cards = json.loads(target.gpu_idle_cards_json)["cards"]
    assert cards["0"]["mask"] == 31
    assert cards["1"]["start"] is None
    notices = db.query(UserNotificationModel).all()
    assert len(notices) == 1
    assert notices[0].type == "gpu_idle_shrink_warning"
    assert "中断所有进程" in notices[0].message


def test_rotating_activity_never_accumulates_idle_time(state):
    db, user, target, settings = prepare(state)
    snapshot = observe(db, target, settings, datetime.now(), end=180,
                       metric_fn=lambda minute: metrics((minute // 5) % 2))
    assert snapshot is None
    assert not db.query(UserNotificationModel).all()


@pytest.mark.parametrize("change", ["runtime", "gpus", "node", "policy", "missing", "expired", "disabled"])
def test_cancel_and_identity_reset(state, change):
    db, user, target, settings = prepare(state)
    start = datetime.now()
    observe(db, target, settings, start, end=50, metric_fn=lambda _: metrics())
    data = metrics()
    if change == "runtime":
        target.container_id = "rebuilt"
    elif change == "gpus":
        target.gpu_ids = "1,0"
    elif change == "node":
        target.node_id = "new-node"
    elif change == "policy":
        settings = replace(settings, idle_gpu_duration_hours=2)
    elif change == "missing":
        data.pop(1)
    elif change == "expired":
        target.expires_at = start
    elif change == "disabled":
        settings = replace(settings, idle_gpu_shrink_enabled=False)
    assert scheduler._observe_idle_cards(db, target, [0, 1], data, settings,
        idle_policy_signature(settings), start + timedelta(minutes=55), timedelta(hours=1)) is None
    if target.gpu_idle_cards_json:
        assert json.loads(target.gpu_idle_cards_json)["cards"]["0"]["mask"] == 1
    else:
        assert change in {"missing", "expired", "disabled"}


def test_all_low_does_not_start_partial_rebuild(state):
    db, user, target, settings = prepare(state)
    low = metrics()
    low[1] = dict(low[0], index=1)
    assert observe(db, target, settings, datetime.now(), metric_fn=lambda _: low) is None
    assert not db.query(UserNotificationModel).all()


@pytest.mark.parametrize("fail_finalize", [False, True])
def test_shrink_reserves_old_resources_until_finalize_and_recovers(state, monkeypatch, fail_finalize):
    db, user, target, settings = prepare(state)
    original = (target.id, target.name, target.node_id, target.ssh_port,
                target.ssh_password, target.extra_ports, target.expires_at, target.created_at)
    calls = []
    def action(node, operation, old_id, data):
        calls.append(operation)
        assert target.gpu_ids == "0,1"  # No resource release even after new runtime is running.
        assert not db.query(UserNotificationModel).filter_by(type="gpu_idle_shrunk").all()
        if operation == "merge":
            assert data["shrink"] is True
            assert data["gpu_ids"] == [1]
            return {"container_id": "new"}
        if operation == "finalize" and fail_finalize:
            raise RuntimeError("offline")
    monkeypatch.setattr(containers, "_merge_action", action)
    if fail_finalize:
        with pytest.raises(HTTPException):
            containers._perform_merge(db, target, [], user, keep_gpus=[1])
        db.refresh(target)
        assert target.status == "merging"
        assert target.gpu_ids == "0,1"
        assert json.loads(target.pending_share_json)["shrink_gpus"] == [1]
        fail_finalize = False
        containers.recover_incomplete_merges(db)
    else:
        containers._perform_merge(db, target, [], user, keep_gpus=[1])
    assert target.status == "running"
    assert target.container_id == "new"
    assert target.gpu_ids == "1"
    assert target.pending_share_json is None
    assert original == (target.id, target.name, target.node_id, target.ssh_port,
                        target.ssh_password, target.extra_ports, target.expires_at, target.created_at)
    assert db.query(UserNotificationModel).filter_by(type="gpu_idle_shrunk").count() == 1
    containers.recover_incomplete_merges(db)
    assert db.query(UserNotificationModel).filter_by(type="gpu_idle_shrunk").count() == 1


def test_shrink_failure_restores_old_resources(state, monkeypatch):
    db, user, target, settings = prepare(state)
    def action(node, operation, old_id, data):
        if operation == "merge":
            raise RuntimeError("failed to create")
    monkeypatch.setattr(containers, "_merge_action", action)
    with pytest.raises(HTTPException):
        containers._perform_merge(db, target, [], user, keep_gpus=[1])
    assert target.status == "running"
    assert target.gpu_ids == "0,1"
    assert target.container_id == "old"
    assert not db.query(UserNotificationModel).all()


@pytest.mark.parametrize("evidence", ["still-low", "active", "missing", "all-low", "policy-changed", "runtime-changed", "expired"])
def test_final_fresh_inventory_and_revalidation(state, monkeypatch, evidence):
    db, user, target, settings = prepare(state)
    save_settings(db, settings)
    signature = idle_policy_signature(settings)
    snapshot = observe(db, target, settings, datetime.now() - timedelta(minutes=65), metric_fn=lambda _: metrics())
    calls = []
    node = SimpleNamespace(id="local", enabled=True)
    monkeypatch.setattr(scheduler, "get_node", lambda *a: node)
    def collect(*args):
        calls.append("collect")
        data = metrics()
        if evidence == "active":
            data[0]["utilization"] = 80
        elif evidence == "missing":
            data.pop(1)
        elif evidence == "all-low":
            data[1] = dict(data[0], index=1)
        elif evidence == "policy-changed":
            save_settings(db, replace(settings, idle_gpu_shrink_enabled=False))
        elif evidence == "runtime-changed":
            target.container_id = "rebuilt"
            db.commit()
        elif evidence == "expired":
            target.expires_at = datetime.now() - timedelta(seconds=1)
            db.commit()
        return {"gpus": list(data.values())}
    monkeypatch.setattr(scheduler, "inventory_for_node", collect)
    monkeypatch.setattr(containers, "_perform_merge", lambda *args, **kw: calls.append(kw["keep_gpus"]))
    scheduler._shrink_idle_cards(db, target.id, snapshot, signature)
    assert calls == (["collect", [1]] if evidence == "still-low" else ["collect"])
    if evidence != "still-low":
        assert target.gpu_idle_cards_json is None


@pytest.mark.parametrize("resident", [False, True])
def test_complete_scheduler_partial_branch(state, monkeypatch, resident):
    from sqlalchemy.orm import sessionmaker
    db, user, target, settings = prepare(state)
    save_settings(db, settings)
    start = datetime.now()
    clock = SimpleNamespace(now=start)
    class Clock(datetime):
        @classmethod
        def now(cls):
            return clock.now
    monkeypatch.setattr(scheduler, "datetime", Clock)
    monkeypatch.setattr(scheduler, "SessionLocal", sessionmaker(bind=db.bind))
    node = SimpleNamespace(id="local", enabled=True)
    monkeypatch.setattr(scheduler, "get_node", lambda *a: node)
    data = metrics()
    if resident:
        data[0].update(utilization=0, memory_percent=80, memory_used_mb=8000)
    collected = []
    def collect(*args):
        collected.append(clock.now)
        return {"gpus": list(data.values())}
    monkeypatch.setattr(scheduler, "inventory_for_node", collect)
    monkeypatch.setattr(scheduler, "_send_notify", lambda *a: None)
    calls = []
    monkeypatch.setattr(containers, "_merge_action", lambda node, operation, old_id, payload: calls.append(operation) or ({"container_id": "new"} if operation == "merge" else None))
    for minute in range(0, 71, 5):
        clock.now = start + timedelta(minutes=minute)
        scheduler._reclaim_idle_gpu_containers()
    db.expire_all()
    assert target.gpu_ids == "1"
    assert target.container_id == "new"
    assert target.status == "running"
    assert calls == ["merge", "finalize"]
    assert db.query(UserNotificationModel).filter_by(type="gpu_idle_shrink_warning").count() == 1
    assert db.query(UserNotificationModel).filter_by(type="gpu_idle_shrunk").count() == 1
    assert db.query(UserNotificationModel).filter_by(type="gpu_idle_reclaimed").count() == 0
    assert len(collected) == 16  # Fifteen round samples plus the uncached final evidence.


def test_worker_shrink_of_shared_cards_needs_no_new_approval(state, monkeypatch):
    from app import worker_share
    db, user, other, target = state
    target.gpu_ids = "0,1"
    db.commit()
    runtime = [dict(container_id="old", gpu_ids="0,1", status="running"),
               dict(container_id="other", gpu_ids="1", status="running")]
    monkeypatch.setattr(worker_share, "list_managed_containers", lambda: runtime)
    worker_share.reject_unapproved_occupancy(db, [1], "old", [0, 1])
    runtime[0]["gpu_ids"] = "0"
    with pytest.raises(HTTPException):
        worker_share.reject_unapproved_occupancy(db, [1], "old", [0, 1])


def test_remote_shrink_routes_to_dedicated_endpoint(state, monkeypatch):
    db, user, target, settings = prepare(state)
    calls = []
    fake = SimpleNamespace(shrink_container=lambda old_id, data: calls.append((old_id, data)) or {"container_id": "new"})
    monkeypatch.setattr(containers, "RemoteAgentClient", lambda *args: fake)
    node = SimpleNamespace(id="remote", base_url="http://worker", agent_token="token")
    assert containers._merge_action(node, "merge", "old", {"shrink": True, "gpu_ids": [1]}) == {"container_id": "new"}
    assert calls == [("old", {"gpu_ids": [1]})]


def test_runtime_shrink_preserves_exact_memory_and_file_commit_order(monkeypatch):
    import hashlib
    from docker.errors import NotFound
    from app import docker_service
    order, created = [], {}
    class Runtime:
        def __init__(self, name, identifier, labels, status="running"):
            self.name, self.id, self.status = name, identifier, status
            self.attrs = {"Config": {"Labels": labels, "Env": ["SSH_PASSWORD=secret"]},
                "HostConfig": {"NetworkMode": "bridge", "Memory": 12 * 1024**3,
                    "MemorySwap": -1, "PortBindings": {"22/tcp": [{"HostPort": "22001"}]}},
                "Mounts": [{"Destination": "/workspace", "Source": "/users/tester", "RW": True, "Type": "bind"}]}
        def reload(self):
            pass
        def stop(self):
            order.append("stop")
            self.status = "exited"
        def commit(self, **kwargs):
            order.append("commit")
            assert self.status == "exited"
            assert kwargs["changes"] == "ENV SSH_PASSWORD="
            return SimpleNamespace(id="image", attrs={"Config": {"Env": ["SSH_PASSWORD="]}})
        def rename(self, name):
            self.name = name
        def start(self):
            order.append("start-new")
            self.status = "running"
        def remove(self):
            order.append("remove-old")
            items.remove(self)
    old = Runtime("labgpu-tester", "old", {"compute-graveyard.managed": "true", "compute-graveyard.username": "tester", "compute-graveyard.gpu_ids": "0,1"})
    items = [old]
    def get(identifier):
        for item in items:
            if identifier in {item.id, item.name}:
                return item
        raise NotFound("missing")
    def create(image, **kwargs):
        order.append("create")
        created.update(kwargs)
        new = Runtime(kwargs["name"], "new", kwargs["labels"], "created")
        items.append(new)
        return new
    client = SimpleNamespace(containers=SimpleNamespace(get=get, create=create), images=SimpleNamespace(remove=lambda *a: None))
    monkeypatch.setattr(docker_service, "get_docker_client", lambda: client)
    monkeypatch.setattr(docker_service, "USER_DATA_BASE", "/users")
    args = ("old", "labgpu-tester", "tester", [0, 1], [1], 22001, {}, hashlib.sha256(b"secret").hexdigest(), 64)
    with pytest.raises(RuntimeError):
        docker_service.merge_container_gpus(*args)  # Original merge API remains increase-only.
    assert docker_service.merge_container_gpus(*args, shrink=True) == "new"
    assert order == ["stop", "commit", "create", "start-new"]
    assert old in items  # Removal is deferred until the durable finalize phase.
    assert created["mem_limit"] == 12 * 1024**3
    assert created["memswap_limit"] == -1
    assert created["environment"] == ["SSH_PASSWORD=secret"]
    assert created["device_requests"][0]["DeviceIDs"] == ["1"]
    docker_service.finalize_gpu_merge("old", "labgpu-tester", "new", "tester", [0, 1])
    assert order[-1] == "remove-old"
    assert items[0].name == "labgpu-tester"


@pytest.mark.parametrize("keep", [[], [0, 1], [2]])
def test_shrink_rejects_empty_equal_or_disjoint_set(state, keep):
    db, user, target, settings = prepare(state)
    with pytest.raises(HTTPException):
        containers._perform_merge(db, target, [], user, keep_gpus=keep)
