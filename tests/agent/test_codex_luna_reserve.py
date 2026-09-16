"""Reserve routing and worker notification contracts."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from agent import auxiliary_client as aux


def entry(label):
    return SimpleNamespace(last_status=None, runtime_api_key=label,
                           runtime_base_url="https://chatgpt.com/backend-api")


def usage(normal=False, reserve=True):
    return {"rate_limit": {"allowed": normal, "limit_reached": not normal},
            "additional_rate_limits": ([{"limit_name": "gpt-reserve",
                "normal_model_slug": "gpt-5.6-luna",
                "rate_limit": {"allowed": True, "limit_reached": False}}]
                if reserve else [])}


@pytest.mark.parametrize("normal_index", [0, 1, 2])
def test_any_normal_quota_prevents_reserve(monkeypatch, normal_index):
    rows = [entry(str(i)) for i in range(3)]
    pool = SimpleNamespace(_entries=rows, _entry_needs_refresh=lambda e: False)
    monkeypatch.setattr(aux, "load_pool", lambda provider: pool)
    monkeypatch.setattr(aux, "_codex_cloudflare_headers", lambda token: {})
    before = [vars(e).copy() for e in rows]
    def get(url, headers, timeout):
        index = int(headers["Authorization"].split()[-1])
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: usage(index == normal_index))
    monkeypatch.setattr("httpx.get", get)
    assert aux._read_codex_reserve_access_token() is None
    assert before == [vars(e) for e in rows]


@pytest.mark.parametrize("reserve_index", [0, 1, 2])
def test_reserve_can_come_from_every_account(monkeypatch, reserve_index):
    rows = [entry(str(i)) for i in range(3)]
    monkeypatch.setattr(aux, "load_pool", lambda provider: SimpleNamespace(
        _entries=rows, _entry_needs_refresh=lambda e: False))
    monkeypatch.setattr(aux, "_codex_cloudflare_headers", lambda token: {})
    def get(url, headers, timeout):
        index = int(headers["Authorization"].split()[-1])
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: usage(reserve=index == reserve_index))
    monkeypatch.setattr("httpx.get", get)
    assert aux._read_codex_reserve_access_token() == str(reserve_index)
    assert aux._read_codex_reserve_access_token(exclude_tokens={str(reserve_index)}) is None


def test_unknown_quota_does_not_enable_reserve(monkeypatch):
    monkeypatch.setattr(aux, "load_pool", lambda provider: SimpleNamespace(
        _entries=[entry("one")], _entry_needs_refresh=lambda e: False))
    monkeypatch.setattr(aux, "_codex_cloudflare_headers", lambda token: {})
    monkeypatch.setattr("httpx.get", Mock(side_effect=TimeoutError))
    assert aux._read_codex_reserve_access_token() is None


def test_switch_survives_completion_in_same_poll():
    from gateway.kanban_watchers import _coalesce_notify_events, _notify_kind_allowed
    events = [SimpleNamespace(id=1, kind="claimed"),
              SimpleNamespace(id=2, kind="model_fallback", payload={"message": "Reserve active"}),
              SimpleNamespace(id=3, kind="completed")]
    assert _coalesce_notify_events(events) == ([events[-1]], ["Reserve active"])
    assert _notify_kind_allowed("model_fallback", lambda: {
        "kanban": {"notify_kinds": ["completed", "blocked", "model_fallback"]}})


def test_worker_notice_uses_kanban_authority(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from run_agent import AIAgent
    path = tmp_path / "board.db"
    with kb.connect_closing(db_path=path) as conn:
        # Actual schema and event write; no gateway or external delivery.
        task_id = kb.create_task(conn, title="Reserve notice", task_role="activity", triage=True)
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_runs (id,task_id,status,started_at) VALUES (7,?,'running',1)", (task_id,))
            conn.execute("UPDATE tasks SET current_run_id=7 WHERE id=?", (task_id,))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    agent = object.__new__(AIAgent)
    agent._pending_fallback_notice = ["Reserva Luna ativada"]
    agent._emit_status = Mock()
    agent._emit_pending_fallback_notice()
    agent._emit_pending_fallback_notice()
    with kb.connect_closing(db_path=path) as conn:
        events = [e for e in kb.list_events(conn, task_id) if e.kind == "model_fallback"]
    assert len(events) == 1
    assert events[0].payload["message"] == "Reserva Luna ativada"
    assert events[0].run_id == 7


def test_principal_notice_survives_telegram_filter():
    from gateway.run import _prepare_gateway_status_message
    from gateway.config import Platform
    message = "🌙 Reserva Luna ativada: cota normal esgotada em todas as contas."
    assert _prepare_gateway_status_message(Platform.TELEGRAM, "lifecycle", message) == message


def test_reserve_rotation_does_not_cycle_accounts(monkeypatch):
    import run_agent
    from agent.chat_completion_helpers import try_activate_fallback
    from agent.error_classifier import FailoverReason
    attempted = []
    def select(exclude_tokens=None):
        attempted.append(set(exclude_tokens or set()))
        return next((token for token in ("one", "two", "three")
                     if token not in (exclude_tokens or set())), None)
    monkeypatch.setattr(aux, "_read_codex_reserve_access_token", select)
    monkeypatch.setattr(aux, "_codex_cloudflare_headers", lambda token: {})
    agent = SimpleNamespace(_fallback_activated=True, model="gpt-reserve",
        provider="openai-codex", api_key="one", _client_kwargs={},
        _replace_primary_openai_client=Mock(), _buffer_status=Mock())
    assert try_activate_fallback(agent, FailoverReason.rate_limit)
    assert agent.api_key == "two"
    assert try_activate_fallback(agent, FailoverReason.rate_limit)
    assert agent.api_key == "three"
    assert not try_activate_fallback(agent, FailoverReason.rate_limit)
    assert attempted[-1] == {"one", "two", "three"}
