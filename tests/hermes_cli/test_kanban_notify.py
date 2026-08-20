import asyncio
import pytest

from pathlib import Path
from types import SimpleNamespace
from hermes_cli import kanban_db as kb
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Allow the kanban notifier path-validator to upload artifacts the
    # tests write under ``tmp_path``. Without this, every artifact-delivery
    # test silently drops files because ``tmp_path`` isn't inside the
    # default ``MEDIA_DELIVERY_SAFE_ROOTS`` cache dirs.
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    kb.init_db()
    return home


def _assert_inherited_notify_sub(subs: list[dict]) -> None:
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "chat1"
    assert subs[0]["thread_id"] == "topic1"
    assert subs[0]["user_id"] == "user1"
    assert subs[0]["notifier_profile"] == "default"










# ---------------------------------------------------------------------------
# Regression: gateway watchers must not double-init the kanban DB.
#
# Both the notifier watcher (`_kanban_notifier_watcher`) and the dispatcher
# tick (`_tick_once_for_board`) used to call `_kb.connect(board=slug)`
# immediately followed by `_kb.init_db(board=slug)`. Since `connect()`
# already runs the schema + idempotent migration on first open per process,
# the explicit `init_db()` was redundant — and worse, `init_db()`
# deliberately busts the per-process cache and re-runs the migration on a
# *second* connection, which races the first.  On legacy DBs this surfaced
# as `duplicate column name: <col>` (now tolerated by
# `_add_column_if_missing`) and intermittent `database is locked` errors
# (issue #21378).
#
# The fix removes the `init_db()` calls in both watchers; this regression
# test pins that behaviour so we don't reintroduce them.
# ---------------------------------------------------------------------------




@pytest.mark.asyncio
async def test_notifier_wakes_origin_for_review_and_keeps_subscription(kanban_home):
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review handoff", assignee="builder")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat1",
        )
        task = kb.claim_task(conn, task_id, claimer="builder:1")
        assert task is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="Implementation and tests ready.",
            reviewer="reviewer",
            expected_run_id=task.current_run_id,
        )

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    # This legacy subscription has no notifier-profile stamp. Current gateway
    # ownership rules intentionally expose such rows only to the singleton
    # dispatcher owner, which this focused watcher harness represents.
    runner._kanban_dispatcher_lock_handle = object()
    delivered: list[str] = []

    async def _send(chat_id, message, metadata=None):
        delivered.append(message)
        runner._running = False

    adapter = MagicMock()
    adapter.name = "telegram"
    adapter.send = AsyncMock(side_effect=_send)
    runner.adapters = {Platform.TELEGRAM: adapter}

    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds):
        await real_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    assert any("ready for review" in message for message in delivered)
    assert any("Implementation and tests ready" in message for message in delivered)
    with kb.connect() as conn:
        assert kb.list_notify_subs(conn), "review is non-final; subscription must survive"


@pytest.mark.asyncio
async def test_gateway_create_autosubscribes_on_explicit_board(kanban_home):
    """`/kanban --board <slug> create ...` must subscribe on that board.

    The gateway handler currently auto-subscribes after `/kanban create`,
    but the create detection must still work when the shared `--board`
    flag appears before the subcommand, and the subscription must land in
    that board's DB rather than the ambient/default board.
    """
    from gateway.run import GatewayRunner
    from gateway.config import Platform

    kb.create_board("projx")

    runner = object.__new__(GatewayRunner)
    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="chat1",
        chat_type="dm",
        thread_id="20197",
        user_id="u1",
    )
    event = SimpleNamespace(
        text='/kanban --board projx create "hello" --assignee alice',
        source=source,
        message_id="462",
        reply_to_message_id=None,
    )

    out = await GatewayRunner._handle_kanban_command(runner, event)

    assert "subscribed" in out.lower()

    conn = kb.connect(board="projx")
    try:
        subs = kb.list_notify_subs(conn)
        tasks = kb.list_tasks(conn)
    finally:
        conn.close()

    assert [t.title for t in tasks] == ["hello"]
    assert len(subs) == 1
    assert subs[0]["chat_id"] == "chat1"
    assert subs[0]["thread_id"] == "20197"
    assert subs[0]["delivery_metadata"] == {
        "chat_type": "dm",
        "direct_messages_topic_id": "20197",
        "telegram_dm_topic_reply_fallback": True,
        "telegram_reply_to_message_id": "462",
        "thread_id": "20197",
    }

    conn = kb.connect(board="default")
    try:
        assert kb.list_notify_subs(conn) == []
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_gateway_kanban_rejects_cross_project_board(kanban_home):
    """A project Topic cannot select a different global board via slash."""
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from gateway.project_router import ProjectContext

    kb.create_board("project-alpha")
    kb.create_board("project-beta")

    runner = object.__new__(GatewayRunner)
    runner._resolve_project_context_for_message = lambda _event, _source: (
        ProjectContext(
            project_id="p_alpha",
            slug="alpha",
            board_slug="project-alpha",
            workdir=None,
            status="active",
            platform="telegram",
            chat_id="team",
            thread_id="topic",
            sender_user_id="member",
            is_management=False,
        ),
        None,
    )
    event = SimpleNamespace(
        text="/kanban --board project-beta list",
        source=SimpleNamespace(
            platform=Platform.TELEGRAM,
            chat_id="team",
            thread_id="topic",
            user_id="member",
        ),
    )

    out = await GatewayRunner._handle_kanban_command(runner, event)

    assert "refusing explicit board" in out


@pytest.mark.asyncio
async def test_gateway_project_create_is_bound_and_event_idempotent(kanban_home):
    """A redelivered slash event creates one card on the Topic board."""
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from gateway.project_router import ProjectContext

    kb.create_board("project-alpha")
    runner = object.__new__(GatewayRunner)
    runner._kanban_notifier_profile = "default"
    runner._resolve_project_context_for_message = lambda _event, _source: (
        ProjectContext(
            project_id="p_alpha",
            slug="alpha",
            board_slug="project-alpha",
            workdir=None,
            status="active",
            platform="telegram",
            chat_id="team",
            thread_id="topic",
            sender_user_id="member",
            is_management=False,
        ),
        None,
    )
    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        profile="team-profile",
        chat_id="team",
        thread_id="topic",
        user_id="member",
    )
    event = SimpleNamespace(
        text='/kanban create "same request" --assignee alice',
        message_id="message-42",
        source=source,
    )

    first = await GatewayRunner._handle_kanban_command(runner, event)
    second = await GatewayRunner._handle_kanban_command(runner, event)

    assert "created" in first.lower()
    assert second
    conn = kb.connect(board="project-alpha")
    try:
        tasks = kb.list_tasks(conn)
    finally:
        conn.close()
    assert [task.title for task in tasks] == ["same request"]


@pytest.mark.asyncio
async def test_notifier_uploads_artifacts_on_completion(kanban_home, tmp_path, monkeypatch):
    """When a completed event carries ``artifacts`` in its payload, the
    notifier uploads each file to the subscribed chat as a native
    attachment. Images batch through send_multiple_images; documents
    route through send_document. See the artifacts wiring in
    gateway/run.py._deliver_kanban_artifacts.
    """
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from tools import kanban_tools as kt

    # ``_deliver_kanban_artifacts`` routes candidates through
    # ``BasePlatformAdapter.filter_local_delivery_paths``, which only accepts
    # paths under ``MEDIA_DELIVERY_SAFE_ROOTS`` or roots explicitly allowlisted
    # via ``HERMES_MEDIA_ALLOW_DIRS``. Test fixtures live under ``tmp_path``,
    # so allowlist it for the duration of the test.
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))

    # Materialize real files so os.path.isfile passes inside the helper.
    chart_path = tmp_path / "q3-revenue.png"
    chart_path.write_bytes(b"PNG-fake-bytes")
    report_path = tmp_path / "report.pdf"
    report_path.write_bytes(b"%PDF-fake")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="render q3 chart", assignee="worker1")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")
    finally:
        conn.close()

    # Use the production handler so we exercise the full path: tool args
    # → metadata.artifacts → event payload promotion.
    import os
    os.environ["HERMES_KANBAN_TASK"] = tid
    try:
        out = kt._handle_complete({
            "summary": "rendered the chart",
            "artifacts": [str(chart_path), str(report_path)],
        })
    finally:
        os.environ.pop("HERMES_KANBAN_TASK", None)
    import json as _json
    assert _json.loads(out)["ok"] is True

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}

    fake_adapter = MagicMock()
    fake_adapter.name = "telegram"

    sends: list = []
    images_uploaded: list = []
    documents_uploaded: list = []

    async def _send(chat_id, msg, metadata=None):
        sends.append((chat_id, msg))
        runner._running = False

    async def _send_images(chat_id, images, metadata=None, **_kw):
        images_uploaded.extend(p for p, _ in images)

    async def _send_document(chat_id, file_path, metadata=None, **_kw):
        documents_uploaded.append(file_path)

    fake_adapter.send = AsyncMock(side_effect=_send)
    fake_adapter.send_multiple_images = AsyncMock(side_effect=_send_images)
    fake_adapter.send_document = AsyncMock(side_effect=_send_document)
    # extract_local_files is used internally for legacy path fallback;
    # the real BasePlatformAdapter implementation lives there, so wire it.
    from gateway.platforms.base import BasePlatformAdapter
    fake_adapter.extract_local_files = BasePlatformAdapter.extract_local_files

    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    # The text completion notification fired.
    assert len(sends) == 1
    # The PNG rode the image-batch path.
    assert any("q3-revenue.png" in p for p in images_uploaded), images_uploaded
    # The PDF rode the document path.
    assert any("report.pdf" in p for p in documents_uploaded), documents_uploaded


@pytest.mark.asyncio
async def test_notifier_artifact_delivery_skips_missing_files(kanban_home, tmp_path, monkeypatch):
    """Missing artifact paths are silently skipped — they may have been
    referenced by name only. The notifier must not crash and must still
    deliver any artifacts that do exist."""
    import hermes_cli.kanban_db as kb
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from tools import kanban_tools as kt

    # Allow ``tmp_path`` through the media-delivery safety filter. See the
    # companion test for the full explanation.
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))

    real_pdf = tmp_path / "real.pdf"
    real_pdf.write_bytes(b"%PDF-fake")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="worker1")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat1")
    finally:
        conn.close()

    import os
    os.environ["HERMES_KANBAN_TASK"] = tid
    try:
        kt._handle_complete({
            "summary": "one real, one ghost",
            "artifacts": [str(real_pdf), "/tmp/definitely-does-not-exist.pdf"],
        })
    finally:
        os.environ.pop("HERMES_KANBAN_TASK", None)

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()

    fake_adapter = MagicMock()
    fake_adapter.name = "telegram"

    documents_uploaded: list = []

    async def _send(chat_id, msg, metadata=None):
        runner._running = False

    async def _send_document(chat_id, file_path, metadata=None, **_kw):
        documents_uploaded.append(file_path)

    fake_adapter.send = AsyncMock(side_effect=_send)
    fake_adapter.send_document = AsyncMock(side_effect=_send_document)
    fake_adapter.send_multiple_images = AsyncMock()
    from gateway.platforms.base import BasePlatformAdapter
    fake_adapter.extract_local_files = BasePlatformAdapter.extract_local_files

    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)

    with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep):
        await asyncio.wait_for(
            runner._kanban_notifier_watcher(interval=1),
            timeout=10.0,
        )

    # Only the real file was uploaded.
    assert len(documents_uploaded) == 1
    assert "real.pdf" in documents_uploaded[0]
