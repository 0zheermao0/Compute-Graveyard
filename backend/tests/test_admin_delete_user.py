import ast
from pathlib import Path


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
