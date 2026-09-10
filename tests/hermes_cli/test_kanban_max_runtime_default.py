"""MAX_RUNTIME_DEFAULT_20260910: card com max_runtime_seconds NULL herda o default do projeto no despacho."""
from hermes_cli import kanban_db


def _db(tmp_path):
    conn = kanban_db.connect(tmp_path / "kanban.db")
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t_null', 'a', 'ready', 0), ('t_set', 'b', 'ready', 0)"
    )
    conn.execute("UPDATE tasks SET max_runtime_seconds = 2700 WHERE id = 't_set'")
    conn.commit()
    return conn


def _value(conn, tid):
    return conn.execute("SELECT max_runtime_seconds FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def test_null_inherits_project_default(tmp_path):
    conn = _db(tmp_path)
    assert kanban_db._apply_project_max_runtime_default(conn, {"max_runtime_seconds": 7200}, "t_null") == 7200
    assert _value(conn, "t_null") == 7200


def test_explicit_budget_is_kept(tmp_path):
    conn = _db(tmp_path)
    assert kanban_db._apply_project_max_runtime_default(conn, {"max_runtime_seconds": 7200}, "t_set") is None
    assert _value(conn, "t_set") == 2700


def test_missing_or_invalid_default_changes_nothing(tmp_path):
    conn = _db(tmp_path)
    for project in (None, {}, {"max_runtime_seconds": None}, {"max_runtime_seconds": "x"}, {"max_runtime_seconds": 0}):
        assert kanban_db._apply_project_max_runtime_default(conn, project, "t_null") is None
    assert _value(conn, "t_null") is None
