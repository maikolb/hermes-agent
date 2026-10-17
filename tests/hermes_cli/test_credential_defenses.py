"""CREDENTIAL_DEFENSES_20261017: a secret that reached the Concursa board on 13 to 16/10 never enters it again.

The DSN password of the production pooler sat in clear in kanban.db (command argv and stderr of a worker), the probe vault accepted
a DSN whose password was `***`, and a worker whose contract did not load went on reading the server. One test per defense.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from tests.hermes_cli.test_nfos_human_last_resort import _card, _http_probe, _spec, board, dns  # noqa: F401
from tests.hermes_cli.test_nfos_tool import adapter, read  # noqa: F401
from tests.hermes_cli.test_nfos_tool import board as tool_board  # noqa: F401

PASSWORD = "Zx9" * 21 + "q"  # 64 characters, like the pooler password found in kanban.db
DSN = f"postgresql://postgres.abcdefghijklmnopqrst:{PASSWORD}@aws-0-sa-east-1.pooler.supabase.com:6543/postgres"


# --- (a) the spawn refuses a worker whose contract does not load ---------------------------------------------------------------

def _task(assignee):
    return kb.Task(
        id="t_guard", title="guard", body=None, assignee=assignee, status="running", priority=0,
        created_by="test", created_at=1, started_at=None, completed_at=None, workspace_kind="dir",
        workspace_path=None, claim_lock="lock", claim_expires=None, tenant=None, current_run_id=7,
    )


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    kb._PROFILE_REFUSED.clear()
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    launched = []

    class Proc:
        pid = 4242

    real_popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: (launched.append(cmd) or Proc()) if cmd[0] == "hermes" else real_popen(cmd, *a, **k))
    (tmp_path / "ws").mkdir()

    def spawn(assignee):
        return kb._default_spawn(_task(assignee), str(tmp_path / "ws"))

    def profile(name, requires, policy=None, contract=b"approved contract text\n"):
        home = root / "profiles" / name
        home.mkdir()
        document = tmp_path / f"{name}-contract.md"
        if contract is not None:
            document.write_bytes(contract)
        if policy is not False:
            home.joinpath("policy.json").write_text(json.dumps(policy or {
                "contract_path": str(document), "contract_sha256": hashlib.sha256(b"approved contract text\n").hexdigest()}), encoding="utf-8")
        home.joinpath("config.yaml").write_text(json.dumps({"toolsets": ["hermes-cli"], "kanban": {"launch_requires": requires}}), encoding="utf-8")
        return home

    return spawn, profile, launched


REQUIRES = [{"name": "Concursa contract", "policy": "policy.json"}]


@pytest.mark.real_spawn_guard
def test_worker_with_its_approved_contract_launches(host):
    spawn, profile, launched = host
    profile("ok", REQUIRES)
    assert spawn("ok") == 4242 and launched


@pytest.mark.real_spawn_guard
def test_worker_without_the_contract_file_is_refused_before_it_starts(host):
    spawn, profile, launched = host
    profile("nocontract", REQUIRES, contract=None)
    with pytest.raises(kb.WorkerProfileRefused, match="not available") as refused:
        spawn("nocontract")
    assert refused.value.code == "contract_unavailable" and not launched


@pytest.mark.real_spawn_guard
def test_worker_with_a_divergent_contract_is_refused(host):
    spawn, profile, launched = host
    profile("divergent", REQUIRES, contract=b"somebody edited the contract\n")
    with pytest.raises(kb.WorkerProfileRefused, match="differs") as refused:
        spawn("divergent")
    assert refused.value.code == "contract_divergent" and not launched


@pytest.mark.real_spawn_guard
def test_worker_without_the_policy_file_is_refused(host):
    spawn, profile, launched = host
    profile("nopolicy", REQUIRES, policy=False)
    with pytest.raises(kb.WorkerProfileRefused) as refused:
        spawn("nopolicy")
    assert refused.value.code == "contract_unavailable" and not launched


@pytest.mark.real_spawn_guard
def test_the_refusal_is_a_hold_that_counts_no_failure(host):
    spawn, profile, launched = host
    profile("nocontract", REQUIRES, contract=None)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_events(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER)")
    with pytest.raises(kb.WorkerProfileRefused) as refused:
        spawn("nocontract")
    exc = refused.value
    assert isinstance(exc, ValueError) and (exc.code, exc.profile) == ("contract_unavailable", "nocontract")
    kb._note_launch_refusal(conn, "t_guard", exc)
    kb._note_launch_refusal(conn, "t_guard", exc)  # the same refusal on the next tick adds nothing
    rows = conn.execute("SELECT kind, payload FROM task_events").fetchall()
    assert [r["kind"] for r in rows] == ["spawn_refused"] and json.loads(rows[0]["payload"])["code"] == "contract_unavailable"


# --- (c) probe-env refuses a masked value ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "***", "****", "<redacted>", "[REDACTED]", "<oculto>", "sk-or-v...abcd",
    "postgresql://postgres.abcdefghijklmnopqrst:***@aws-0-sa-east-1.pooler.supabase.com:6543/postgres",
    "postgresql://postgres.abcdefghijklmnopqrst:<redacted>@aws-0-sa-east-1.pooler.supabase.com:6543/postgres",
    "postgresql://postgres.abcdefghijklmnopqrst:@aws-0-sa-east-1.pooler.supabase.com:6543/postgres",
])
def test_probe_env_refuses_a_masked_or_empty_credential(board, value):
    with kb.connect_closing() as conn:
        task = _card(conn, 5, spec=_spec(_http_probe()))
        with pytest.raises(delivery.WorkflowError, match="refuses this value"):
            delivery.probe_env_command(conn, task.id, set_name="PROBE_DATABASE_URL", value=value)
    assert not (board / "secrets" / "infotributos" / "probe.env").exists()


def test_probe_env_still_stores_a_real_dsn_and_an_ordinary_cookie(board):
    with kb.connect_closing() as conn:
        task = _card(conn, 6, spec=_spec(_http_probe()))
        assert delivery.probe_env_command(conn, task.id, set_name="PROBE_DATABASE_URL", value=DSN)["saved"]
        assert delivery.probe_env_command(conn, task.id, set_name="PROFILE_COOKIE", value="sid=s3cr3t-value")["saved"]
    assert f"PROBE_DATABASE_URL={DSN}" in (board / "secrets" / "infotributos" / "probe.env").read_text(encoding="utf-8")


# --- (d) the board never stores a secret from a command ------------------------------------------------------------------------

def test_argv_and_output_are_masked_before_the_board_stores_them(adapter, tool_board):
    marker = tool_board.parent / "real-argv.txt"
    code = ("import sys;from pathlib import Path;Path(%r).write_text(sys.argv[1]);"
            "print('connecting to', sys.argv[1]);sys.stderr.write('FATAL: could not connect ' + sys.argv[1] + '\\n')") % str(marker)
    result = adapter.run_command(tool_board, task_id="t_one", run_id=7, argv=[sys.executable, "-u", "-c", code, DSN],
                                 cwd=tool_board.parent, timeout_seconds=20, call_id="secret-argv")
    assert result["status"] == "succeeded"
    assert marker.read_text() == DSN, "the command itself runs with the real argv"
    stored = json.dumps(read(tool_board, "SELECT * FROM nfos_tool_calls"), default=str)
    chunks = b"".join(r["content"] for r in read(tool_board, "SELECT * FROM nfos_tool_chunks ORDER BY seq"))
    assert PASSWORD not in stored and PASSWORD.encode() not in chunks
    assert "pooler.supabase.com" in stored and b"pooler.supabase.com" in chunks, "the rest of the text is kept"
    assert b"***" in chunks and b"could not connect" in chunks


def test_a_call_id_still_matches_its_own_masked_argv(adapter, tool_board):
    argv = [sys.executable, "-c", "print('ok')", DSN]
    first = adapter.run_command(tool_board, task_id="t_one", run_id=7, argv=argv, cwd=tool_board.parent, timeout_seconds=20, call_id="same")
    again = adapter.run_command(tool_board, task_id="t_one", run_id=7, argv=argv, cwd=tool_board.parent, timeout_seconds=20, call_id="same")
    assert first["id"] == again["id"]
