"""Fresh-process proofs for final-response delivery recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


_PREPARE_PENDING = r"""
import json
import os
import sys
from pathlib import Path

home = Path(sys.argv[1]).resolve()
os.environ["HERMES_HOME"] = str(home)
from agent.turn_checkpoint import (
    TurnCheckpointStore,
    bind_checkpoint_delivery_obligation,
    checkpoint_delivery_fence,
)
from gateway.delivery_ledger import compute_obligation_id, record_obligation

content = "durable final answer"
session_key = "agent:main:slack:channel:C1"
store = TurnCheckpointStore(home / "sessions" / "turn-checkpoints")
store.start_turn(
    "session-1",
    "turn-1",
    "deliver",
    [{"role": "user", "content": "deliver"}],
    routing={"platform": "slack", "chat_id": "C1", "thread_id": ""},
)
state = store.mark_deliverable(
    "session-1",
    content,
    verification_pending=False,
    verification_kind="ordinary_final",
)
fence = checkpoint_delivery_fence(state)
oid = compute_obligation_id(
    session_key,
    f"checkpoint:{fence['turn_id']}:{fence['deliverable_revision']}",
    fence["content_sha256"],
)
assert bind_checkpoint_delivery_obligation(
    "session-1",
    obligation_id=oid,
    routing={"platform": "slack", "chat_id": "C1", "thread_id": ""},
    checkpoint_root=home / "sessions" / "turn-checkpoints",
    **fence,
)
record_obligation(
    obligation_id=oid,
    session_key=session_key,
    platform="slack",
    chat_id="C1",
    thread_id=None,
    content=content,
    session_id="session-1",
    checkpoint_turn_id=fence["turn_id"],
    checkpoint_revision=fence["deliverable_revision"],
    checkpoint_content_sha256=fence["content_sha256"],
    storage_home=home,
)
Path(sys.argv[2]).write_text(json.dumps({"obligation_id": oid}), encoding="utf-8")
os._exit(0)
"""


_RECOVER = r"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

home = Path(sys.argv[1]).resolve()
sentinel = Path(sys.argv[2])
os.environ["HERMES_HOME"] = str(home)
from gateway.config import Platform
from gateway.run import GatewayRunner

class Adapter:
    supports_exact_text_delivery = True
    def can_deliver_exact_text(self, content, metadata=None):
        return True
    async def send(self, **kwargs):
        previous = int(sentinel.read_text(encoding="utf-8")) if sentinel.exists() else 0
        sentinel.write_text(str(previous + 1), encoding="utf-8")
        return SimpleNamespace(success=True, error="")

class Store:
    _store = None
    async def clear_resume_pending(self, _session_key):
        return True

runner = object.__new__(GatewayRunner)
runner.config = SimpleNamespace(multiplex_profiles=False)
runner.adapters = {Platform.SLACK: Adapter()}
runner._profile_adapters = {}
runner.session_store = None
runner._async_session_store = Store()
count = asyncio.run(runner._redeliver_pending_obligations())
print(count)
"""


_CLAIM_ONLY = r"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

home = Path(sys.argv[1]).resolve()
os.environ["HERMES_HOME"] = str(home)
from gateway.config import Platform
from gateway.run import GatewayRunner

class Adapter:
    supports_exact_text_delivery = True
    def can_deliver_exact_text(self, content, metadata=None):
        return True

runner = object.__new__(GatewayRunner)
runner.config = SimpleNamespace(multiplex_profiles=False)
runner.adapters = {Platform.SLACK: Adapter()}
runner._profile_adapters = {}
claims = asyncio.run(runner._claim_pending_obligations())
assert len(claims) == 1
assert claims[0]["attempt_token"]
# Exit at the exact process boundary under test: ownership was claimed, but
# no checkpoint handoff or adapter network call has happened.
os._exit(0)
"""


_PREPARE_AMBIGUOUS = r"""
import os
import sys
from pathlib import Path

home = Path(sys.argv[1]).resolve()
sentinel = Path(sys.argv[2])
os.environ["HERMES_HOME"] = str(home)
from agent.turn_checkpoint import (
    TurnCheckpointStore,
    bind_checkpoint_delivery_obligation,
    checkpoint_delivery_fence,
)
from gateway.delivery_ledger import (
    compute_obligation_id,
    mark_attempting,
    record_obligation,
)

content = "possibly accepted answer"
session_key = "agent:main:slack:channel:C1"
store = TurnCheckpointStore(home / "sessions" / "turn-checkpoints")
store.start_turn(
    "session-1",
    "turn-1",
    "deliver",
    [{"role": "user", "content": "deliver"}],
    routing={"platform": "slack", "chat_id": "C1", "thread_id": ""},
)
state = store.mark_deliverable(
    "session-1",
    content,
    verification_pending=False,
    verification_kind="ordinary_final",
)
fence = checkpoint_delivery_fence(state)
oid = compute_obligation_id(
    session_key,
    f"checkpoint:{fence['turn_id']}:{fence['deliverable_revision']}",
    fence["content_sha256"],
)
assert bind_checkpoint_delivery_obligation(
    "session-1",
    obligation_id=oid,
    routing={"platform": "slack", "chat_id": "C1", "thread_id": ""},
    checkpoint_root=home / "sessions" / "turn-checkpoints",
    **fence,
)
record_obligation(
    obligation_id=oid,
    session_key=session_key,
    platform="slack",
    chat_id="C1",
    thread_id=None,
    content=content,
    session_id="session-1",
    checkpoint_turn_id=fence["turn_id"],
    checkpoint_revision=fence["deliverable_revision"],
    checkpoint_content_sha256=fence["content_sha256"],
    storage_home=home,
)
assert mark_attempting(oid, storage_home=home)
# Model a remote acceptance followed by process death before local ACK.
sentinel.write_text("1", encoding="utf-8")
os._exit(0)
"""


_PERSIST_RECOVERY_ARTIFACT = r"""
import json
import os
import sys
from pathlib import Path

home = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2])
from hermes_state import SessionDB

db = SessionDB(db_path=home / "state.db")
db.create_session(session_id="session-artifact", source="gateway")
first = db.append_delivery_recovery_artifact(
    "session-artifact", "exact transformed final"
)
second = db.append_delivery_recovery_artifact(
    "session-artifact", "exact transformed final"
)
output.write_text(json.dumps({"first": first, "second": second}), encoding="utf-8")
os._exit(0)
"""


_READ_RECOVERY_ARTIFACT = r"""
import json
import sys
from pathlib import Path

from hermes_state import SessionDB

db = SessionDB(db_path=Path(sys.argv[1]).resolve() / "state.db")
active = db.get_messages("session-artifact")
all_rows = db.get_messages("session-artifact", include_inactive=True)
print(json.dumps({"active": active, "all": all_rows}))
"""


def _creationflags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def _child(code: str, *args: object) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code, *(str(arg) for arg in args)],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        creationflags=_creationflags(),
        check=False,
    )


def _state(home: Path) -> str:
    with sqlite3.connect(home / "state.db") as conn:
        return str(
            conn.execute("SELECT state FROM delivery_obligations").fetchone()[0]
        )


def test_pending_final_recovers_once_across_two_fresh_processes(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    prepared = tmp_path / "prepared.json"
    sentinel = tmp_path / "remote-count.txt"

    first = _child(_PREPARE_PENDING, home, prepared)
    assert first.returncode == 0, first.stderr
    assert json.loads(prepared.read_text(encoding="utf-8"))["obligation_id"]

    recovered = _child(_RECOVER, home, sentinel)
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout.strip() == "1"
    assert sentinel.read_text(encoding="utf-8") == "1"
    assert _state(home) == "delivered"

    replay = _child(_RECOVER, home, sentinel)
    assert replay.returncode == 0, replay.stderr
    assert replay.stdout.strip() == "0"
    assert sentinel.read_text(encoding="utf-8") == "1"
    assert _state(home) == "delivered"


def test_process_death_after_claim_before_send_remains_recoverable(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    prepared = tmp_path / "prepared.json"
    sentinel = tmp_path / "remote-count.txt"

    first = _child(_PREPARE_PENDING, home, prepared)
    assert first.returncode == 0, first.stderr

    claimed_only = _child(_CLAIM_ONLY, home)
    assert claimed_only.returncode == 0, claimed_only.stderr
    assert _state(home) == "claimed"
    assert not sentinel.exists()

    recovered = _child(_RECOVER, home, sentinel)
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout.strip() == "1"
    assert sentinel.read_text(encoding="utf-8") == "1"
    assert _state(home) == "delivered"


def test_remote_accept_before_local_ack_is_not_retried(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    sentinel = tmp_path / "remote-count.txt"

    prepared = _child(_PREPARE_AMBIGUOUS, home, sentinel)
    assert prepared.returncode == 0, prepared.stderr
    assert sentinel.read_text(encoding="utf-8") == "1"

    recovered = _child(_RECOVER, home, sentinel)
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout.strip() == "0"
    assert sentinel.read_text(encoding="utf-8") == "1"
    assert _state(home) == "delivery_ambiguous"


def test_exact_transformed_payload_survives_process_death_outside_model_history(
    tmp_path,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    persisted = tmp_path / "artifact.json"

    writer = _child(_PERSIST_RECOVERY_ARTIFACT, home, persisted)
    assert writer.returncode == 0, writer.stderr
    artifact_ids = json.loads(persisted.read_text(encoding="utf-8"))
    assert artifact_ids["first"] == artifact_ids["second"]

    reader = _child(_READ_RECOVERY_ARTIFACT, home)
    assert reader.returncode == 0, reader.stderr
    rows = json.loads(reader.stdout)
    assert rows["active"] == []
    assert len(rows["all"]) == 1
    assert rows["all"][0]["content"] == "exact transformed final"
    assert rows["all"][0]["display_kind"] == "delivery_checkpoint_artifact"
