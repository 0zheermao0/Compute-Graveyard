import json
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app.api import admin, agent
from app import scheduler, worker_share
from app.container_lifecycle import RemovalResult, remove_container_record
from app.database_models import ContainerModel, ShareRequestModel
from app.share_lifecycle import reject_shares_for_exit
from test_worker_share import worker, request


def pending(db, owner, **changes):
    values = dict(name="pending-" + str(db.query(ContainerModel).count()), user_id=2,
                  node_id=owner.node_id, gpu_ids="0", status="pending_share_approval",
                  expires_at=datetime.now() + timedelta(days=1), ssh_port=0,
                  pending_share_json=json.dumps({"approvers": [{"user_id": owner.user_id, "approved": True}]}))
    values.update(changes)
    row = ContainerModel(**values)
    db.add(row)
    db.commit()
    return row


def test_local_exit_including_approved_owner_and_target(worker):
    _, db, _, _ = worker
    owner = db.query(ContainerModel).first()
    approved = pending(db, owner)
    snapshot = pending(db, owner, pending_share_json=json.dumps({"occupancy": [{"id": owner.id}]}))
    target = pending(db, owner, target_container_id=owner.id, gpu_ids="1", pending_share_json="{}")
    unrelated = [pending(db, owner, gpu_ids="1"), pending(db, owner, node_id="other"),
                 pending(db, owner, pending_share_json=json.dumps({"approvers": [{"user_id": 2}]})),
                 pending(db, owner, pending_share_json=json.dumps({"occupancy": [{"id": 999}]})),
                 pending(db, owner, pending_share_json=json.dumps({"request_id": "remote"}))]
    names = [row.name for row in (approved, snapshot, target)]
    now = datetime.now()
    reject_shares_for_exit(db, owner, now=now)
    db.commit()
    for row, name in zip((approved, snapshot, target), names):
        assert row.status == "share_rejected"
        assert row.pending_share_json is None
        assert row.stopped_at == now
        assert row.name != name
    assert all(row.status == "pending_share_approval" for row in unrelated)


@pytest.mark.parametrize("path", ["remove", "scheduler", "admin"])
@pytest.mark.parametrize("success", [False, True])
def test_core_exit_only_rejects_after_runtime_success(worker, path, success):
    _, db, _, monkeypatch = worker
    owner = db.query(ContainerModel).first()
    local = pending(db, owner)
    assert request(worker[0]).status_code == 200
    remote = db.get(ShareRequestModel, "a" * 32)
    if path == "scheduler":
        owner.expires_at = datetime.now() - timedelta(hours=1)
        db.commit()
    def runtime_result(*_):
        assert worker_share.worker_gpu_lock._state.depth > 0
        return success
    original_commit = db.commit
    def locked_commit():
        assert worker_share.worker_gpu_lock._state.depth > 0
        original_commit()
    monkeypatch.setattr(db, "commit", locked_commit)
    if path == "remove":
        result = remove_container_record(db, owner, "test", docker_remover=lambda _: RemovalResult(success=runtime_result()))
        assert result.success == success
    elif path == "scheduler":
        monkeypatch.setattr(scheduler, "stop_container", runtime_result)
        result = scheduler._stop_and_mark(db, owner, "expired", datetime.now())
        assert bool(result) == success
    else:
        monkeypatch.setattr(admin, "stop_on_node", runtime_result)
        if success:
            admin.force_stop(owner.id, admin=None, db=db)
        else:
            with pytest.raises(HTTPException):
                admin.force_stop(owner.id, admin=None, db=db)
    assert local.status == ("share_rejected" if success else "pending_share_approval")
    assert remote.state == ("rejected" if success else "pending")


@pytest.mark.parametrize("method", ["stop", "delete"])
@pytest.mark.parametrize("state,result,rejected", [
    ("pending", None, True), ("approved", None, True),
    ("provisioning", None, False), ("uncertain", None, False),
    ("provisioned", '{"container_id":"new"}', False),
    ("approved", "{}", False),
])
def test_worker_agent_exit_precise_and_safe(worker, method, state, result, rejected):
    client, db, _, monkeypatch = worker
    assert request(client).status_code == 200
    row = db.get(ShareRequestModel, "a" * 32)
    row.state, row.provision_result = state, result
    unrelated = ShareRequestModel(id="b" * 32, payload=json.dumps({"occupancy": [{"container_id": "unrelated"}]}),
                                 approvers="[]", state="pending", expires_at=row.expires_at)
    db.add(unrelated)
    db.commit()
    # No DB owner record is required for Master-managed runtime containers.
    db.delete(db.query(ContainerModel).first())
    db.commit()
    monkeypatch.setattr(agent, "_require_managed", lambda _: None)
    monkeypatch.setattr(agent, "_require_not_merging", lambda _: None)
    monkeypatch.setattr(agent, "stop_container", lambda _: True)
    monkeypatch.setattr(agent, "remove_container", lambda _: True)
    url = "/api/agent/v1/containers/owner-id"
    response = client.post(url + "/stop", headers={"Authorization": "Bearer secret"}) if method == "stop" else client.delete(url, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert row.state == ("rejected" if rejected else state)
    assert row.provision_result == result
    assert unrelated.state == "pending"


@pytest.mark.parametrize("source", ["occupancy", "approvers"])
def test_worker_exit_matches_either_snapshot_source(worker, source):
    client, db, _, _ = worker
    assert request(client).status_code == 200
    row = db.get(ShareRequestModel, "a" * 32)
    if source == "occupancy":
        row.approvers = "[]"
    else:
        row.payload = "{}"
    db.commit()
    reject_shares_for_exit(db, docker_id="owner-id")
    db.commit()
    assert row.state == "rejected"


@pytest.mark.parametrize("method", ["stop", "delete"])
def test_worker_agent_failure_does_not_reject(worker, method):
    client, db, _, monkeypatch = worker
    assert request(client).status_code == 200
    monkeypatch.setattr(agent, "_require_managed", lambda _: None)
    monkeypatch.setattr(agent, "_require_not_merging", lambda _: None)
    monkeypatch.setattr(agent, "stop_container", lambda _: False)
    monkeypatch.setattr(agent, "remove_container", lambda _: False)
    url = "/api/agent/v1/containers/owner-id"
    response = client.post(url + "/stop", headers={"Authorization": "Bearer secret"}) if method == "stop" else client.delete(url, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 500
    assert db.get(ShareRequestModel, "a" * 32).state == "pending"
