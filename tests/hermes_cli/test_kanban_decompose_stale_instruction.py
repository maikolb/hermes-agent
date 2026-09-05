"""Reject a generated graph after the instruction changes during the LLM call."""

import json
from pathlib import Path
from types import SimpleNamespace


def test_instruction_changed_during_decomposition_discards_graph(tmp_path, monkeypatch):
    from agent import auxiliary_client
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_decompose as decomp

    repo = Path(__file__).resolve().parents[2]
    assert Path(kb.__file__).resolve() == repo / "hermes_cli" / "kanban_db.py"
    assert Path(decomp.__file__).resolve() == repo / "hermes_cli" / "kanban_decompose.py"
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home / "kanban"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(decomp, "_build_roster", lambda: ([], {"default"}))
    kb.init_db()
    original_body = "Wait for nine schedule windows before preparing the migration."
    latest_body = "Prepare the migration now without waiting for schedules."
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="Prepare migration", body=original_body, triage=True,
            requires_repo=False,
        )

    calls = []
    discarded_in_transaction = []
    append_event = kb._append_event

    def observe_event(conn, tid, kind, payload=None, **kwargs):
        if kind == "decomposition_discarded":
            discarded_in_transaction.append(conn.in_transaction)
        return append_event(conn, tid, kind, payload, **kwargs)

    monkeypatch.setattr(kb, "_append_event", observe_event)

    def generate_obsolete_graph(**kwargs):
        calls.append(kwargs)
        prompt = kwargs["messages"][1]["content"]
        assert original_body in prompt
        assert latest_body not in prompt
        # A second real connection can commit an edit while the model works.
        # The task stays in triage, so a status-only guard would accept it.
        with kb.connect_closing() as editor:
            with kb.write_txn(editor):
                editor.execute(
                    "UPDATE tasks SET body = ? WHERE id = ?",
                    (latest_body, task_id),
                )
        payload = {
            "fanout": True,
            "tasks": [
                {"title": "Wait nine windows", "assignee": "default", "parents": []},
                {"title": "Prepare migration", "assignee": "default", "parents": [0]},
            ],
        }
        return SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload))),
        ])

    monkeypatch.setattr(auxiliary_client, "call_llm", generate_obsolete_graph)
    outcome = decomp.decompose_task(task_id, author="regression")

    assert not outcome.ok
    assert "instruction changed" in outcome.reason
    assert "stale output discarded" in outcome.reason
    assert not outcome.child_ids and not outcome.fanout
    assert len(calls) == 1
    assert discarded_in_transaction == [True]
    # Reopen to prove the discard event committed and no graph escaped.
    with kb.connect_closing() as conn:
        root = kb.get_task(conn, task_id)
        assert root.body == latest_body
        assert root.title == "Prepare migration"
        assert root.status == "triage"
        assert root.assignee is None
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ?", (task_id,),
        ).fetchall()
    discarded = [row for row in events if row["kind"] == "decomposition_discarded"]
    assert len(discarded) == 1
    assert json.loads(discarded[0]["payload"]) == {"reason": "instruction_changed"}
    assert not {"decomposed", "specified", "completed", "abandoned"} & {
        row["kind"] for row in events
    }
    print(json.dumps({
        "kanban_db_import": str(Path(kb.__file__).resolve()),
        "decomposer_import": str(Path(decomp.__file__).resolve()),
        "hermes_home": str(home),
        "llm_calls": len(calls),
        "children": 0,
        "dependencies": 0,
        "root_status": root.status,
        "discard_event_committed": True,
        "discard_inside_transaction": discarded_in_transaction,
    }))
