import ast
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.api.admin import delete_user
from app.database import Base
from app.database_models import UserModel, UserNotificationModel


def test_delete_user_deletes_lease_records_before_container():
    source = Path(__file__).parents[1] / "app" / "api" / "admin.py"
    tree = ast.parse(source.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "delete_user"
    )
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]

    lease_delete = next(
        node
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "delete"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Attribute)
        and node.func.value.func.attr == "filter"
        and "LeaseRecordModel" in ast.unparse(node.func.value)
    )
    container_delete = next(
        node
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == "delete"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "c"
    )

    assert lease_delete.lineno < container_delete.lineno


def test_delete_user_removes_notifications_with_foreign_keys_enforced():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = UserModel(username="alice", hashed_password="x", role="user")
        other = UserModel(username="bob", hashed_password="x", role="user")
        db.add_all([user, other])
        db.flush()
        db.add_all([
            UserNotificationModel(user_id=user.id, event_key="alice-event", type="test", title="test", message="test"),
            UserNotificationModel(user_id=other.id, event_key="bob-event", type="test", title="test", message="test"),
        ])
        db.commit()

        assert delete_user(user.id, admin=other, db=db)["message"]
        assert db.get(UserModel, user.id) is None
        assert [(n.user_id, n.event_key) for n in db.query(UserNotificationModel).all()] == [
            (other.id, "bob-event"),
        ]
    engine.dispose()
