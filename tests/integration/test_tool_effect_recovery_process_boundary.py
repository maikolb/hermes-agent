"""Hard-process proof for the primordial tool-effect recovery boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


_CHILD = r"""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from agent import relay_tools, tool_executor
from agent.turn_checkpoint import TurnCheckpointStore
from hermes_cli import middleware

root = Path(sys.argv[1])
sentinel = Path(sys.argv[2])
ready = Path(sys.argv[3])
outcome_path = Path(sys.argv[4])
mode = sys.argv[5]
session_id = "process-session"
messages = [{"role": "user", "content": "write the sentinel"}]
args = {"path": str(sentinel), "content": "effect"}
store = TurnCheckpointStore(root)
if mode == "effect":
    store.start_turn(session_id, "turn-1", "write the sentinel", messages)
else:
    store.restore(session_id, messages)

agent = SimpleNamespace(
    session_id=session_id,
    _turn_checkpoint_store=store,
    _turn_checkpoint_state=store.load(session_id),
    _tool_guardrails=SimpleNamespace(
        before_call=lambda *_a, **_k: SimpleNamespace(allows_execution=True)
    ),
    _turns_since_memory=0,
    _iters_since_skill=0,
    _touch_activity=lambda *_a, **_k: None,
    _incremental_persistence_failed=False,
)
tool_executor._begin_tool_execution = lambda *_a, **_k: None
relay_tools.execute = (
    lambda _name, call_args, callback, **_kwargs:
    (callback(call_args), call_args)
)
middleware.apply_tool_request_middleware = (
    lambda _name, call_args, **_kwargs:
    SimpleNamespace(payload=call_args, trace=[])
)
middleware.run_tool_execution_middleware = (
    lambda _name, call_args, callback, **_kwargs: callback(call_args)
)

def effect(_args):
    count = int(sentinel.read_text(encoding="utf-8")) if sentinel.exists() else 0
    sentinel.write_text(str(count + 1), encoding="utf-8")
    ready.write_text("effect-applied", encoding="utf-8")
    if mode == "effect":
        while True:
            time.sleep(1)
    return "effect-applied"

result = tool_executor._run_agent_tool_execution_middleware(
    agent,
    function_name="write_file",
    function_args=args,
    effective_task_id="task-1",
    tool_call_id="call-1" if mode == "effect" else "call-retry",
    execute=effect,
)
outcome_path.write_text(
    json.dumps({"result": str(result.result), "executed": sentinel.read_text()}),
    encoding="utf-8",
)
"""


_CHECKER = r"""
import json
import sys
from pathlib import Path
from agent.turn_checkpoint import TurnCheckpointStore

root = Path(sys.argv[1])
sentinel = Path(sys.argv[2])
outcome_path = Path(sys.argv[3])
messages = [{"role": "user", "content": "write the sentinel"}]
args = {"path": str(sentinel), "content": "effect"}
store = TurnCheckpointStore(root)
state = store.restore("process-session", messages)
first = store.guard_unknown_replay("process-session", "write_file", args)
second = store.guard_unknown_replay("process-session", "write_file", args)
outcome_path.write_text(
    json.dumps({
        "phase": state.get("phase"),
        "pending_tools": state.get("pending_tools"),
        "unknown_count": len(state.get("unknown_outcomes") or []),
        "first_blocked": bool(first),
        "second_blocked": bool(second),
    }),
    encoding="utf-8",
)
"""


def _creationflags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def _spawn(code: str, *args: Path | str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code, *(str(arg) for arg in args)],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=_creationflags(),
    )


def _wait_for(path: Path, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(
                f"child exited before barrier (code={proc.returncode}): {stderr}"
            )
        time.sleep(0.025)
    proc.kill()
    proc.wait(timeout=5)
    raise AssertionError(f"child did not reach barrier within {timeout}s")


def test_hard_exit_after_effect_blocks_every_fresh_process_replay(tmp_path):
    checkpoint_root = tmp_path / "turn-checkpoints"
    sentinel = tmp_path / "effect-count.txt"
    ready = tmp_path / "effect-ready.txt"
    first_outcome = tmp_path / "first-outcome.json"

    child = _spawn(
        _CHILD,
        checkpoint_root,
        sentinel,
        ready,
        first_outcome,
        "effect",
    )
    try:
        _wait_for(ready, child)
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    assert sentinel.read_text(encoding="utf-8") == "1"
    assert not first_outcome.exists(), "the killed process must not ACK the tool result"

    check_outcome = tmp_path / "check-outcome.json"
    checker = _spawn(_CHECKER, checkpoint_root, sentinel, check_outcome)
    checker_stderr = checker.communicate(timeout=15)[1]
    assert checker.returncode == 0, checker_stderr
    recovered = json.loads(check_outcome.read_text(encoding="utf-8"))
    assert recovered == {
        "phase": "reconcile_required",
        "pending_tools": [],
        "unknown_count": 1,
        "first_blocked": True,
        "second_blocked": True,
    }

    retry_outcome = tmp_path / "retry-outcome.json"
    retry_ready = tmp_path / "retry-ready.txt"
    retry = _spawn(
        _CHILD,
        checkpoint_root,
        sentinel,
        retry_ready,
        retry_outcome,
        "retry",
    )
    retry_stderr = retry.communicate(timeout=15)[1]
    assert retry.returncode == 0, retry_stderr
    assert sentinel.read_text(encoding="utf-8") == "1"
    retry_result = json.loads(retry_outcome.read_text(encoding="utf-8"))
    assert retry_result["executed"] == "1"
    assert "unknown outcome" in retry_result["result"].lower()
