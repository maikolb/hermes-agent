"""Fresh-process proofs for compaction and active-turn checkpoint recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_PREPARE_CHILD = r"""
import json
import sys
from pathlib import Path
from agent.turn_checkpoint import TurnCheckpointStore

root = Path(sys.argv[1])
barrier = Path(sys.argv[2])
before = json.loads(sys.argv[3])
after = json.loads(sys.argv[4])
store = TurnCheckpointStore(root)
store.start_turn("session-1", "turn-1", "continue work", before)
store.transition(
    "session-1",
    phase="planning",
    next_action="resume_exact_material_step",
)
store.prepare_compaction("session-1", before, after)
barrier.write_text("prepared", encoding="utf-8")
"""


_RESTORE_CHILD = r"""
import json
import sys
from pathlib import Path
from agent.turn_checkpoint import TurnCheckpointStore

root = Path(sys.argv[1])
live = json.loads(sys.argv[2])
outcome = Path(sys.argv[3])
state = TurnCheckpointStore(root).restore("session-1", live)
outcome.write_text(json.dumps({
    "turn_id": state.get("turn_id"),
    "phase": state.get("phase"),
    "next_action": state.get("next_action"),
    "resolution": (state.get("recovery") or {}).get("resolution"),
    "compaction": (state.get("compaction") or {}).get("state"),
}), encoding="utf-8")
"""


def _creationflags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def _run_child(code: str, *args: object) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code, *(str(arg) for arg in args)],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        creationflags=_creationflags(),
        check=False,
    )


@pytest.mark.parametrize(
    ("live_side", "expected"),
    [
        (
            "before",
            {
                "turn_id": "turn-1",
                "phase": "turn_active",
                "next_action": "retry_compaction_or_continue_original_transcript",
                "resolution": "swap_not_applied",
                "compaction": "captured",
            },
        ),
        (
            "after",
            {
                "turn_id": "turn-1",
                "phase": "turn_active",
                "next_action": "resume_current_turn_from_checkpoint",
                "resolution": "swap_committed_before_ack",
                "compaction": "committed",
            },
        ),
    ],
)
def test_prepared_compaction_recovers_exact_side_in_fresh_process(
    tmp_path, live_side, expected
):
    root = tmp_path / "turn-checkpoints"
    barrier = tmp_path / "prepared.txt"
    outcome = tmp_path / "restored.json"
    before = [
        {"role": "user", "content": "continue work"},
        {"role": "assistant", "content": "material progress"},
    ]
    after = [
        {"role": "system", "content": "bounded compaction summary"},
        {"role": "user", "content": "continue work"},
    ]

    prepared = _run_child(
        _PREPARE_CHILD,
        root,
        barrier,
        json.dumps(before),
        json.dumps(after),
    )
    assert prepared.returncode == 0, prepared.stderr
    assert barrier.read_text(encoding="utf-8") == "prepared"

    live = before if live_side == "before" else after
    restored = _run_child(
        _RESTORE_CHILD,
        root,
        json.dumps(live),
        outcome,
    )
    assert restored.returncode == 0, restored.stderr
    assert json.loads(outcome.read_text(encoding="utf-8")) == expected
