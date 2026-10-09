"""Aviso ao Principal que espera demais passa na frente; abaixo do limite, urgente primeiro (PRINCIPAL_FAIR_ADMISSION_20261009)."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from agent.turn_checkpoint import initialize_agent_turn_checkpoint
from gateway import kanban_watchers as kw
from gateway.config import Platform, PlatformConfig
from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from tests.gateway.test_kanban_notifier import _make_runner, _run_one_notifier_tick
from tests.gateway.test_kanban_notifier_durable import RealAdapter
from tests.gateway.test_nfos_urgent_principal import waiting_card

LIMIT = kw.PRINCIPAL_FAIR_AFTER_SECONDS


def item(priority, age, now=10_000.0):
    return {"task": SimpleNamespace(priority=priority), "label": f"{priority}/{age}",
            "events": [SimpleNamespace(created_at=now - age)] if age is not None else []}


def labels(order):
    return [entry["label"] for entry in order]


def test_order_keeps_urgent_first_below_the_limit():
    order = kw._admission_order([item(10, 600), item(100, 30)], now=10_000.0)
    assert labels(order) == ["100/30", "10/600"]


def test_order_puts_overdue_wake_ahead_of_fresh_urgent():
    order = kw._admission_order([item(100, 30), item(10, LIMIT + 300), item(100, 60)], now=10_000.0)
    assert labels(order) == [f"10/{LIMIT + 300}", "100/60", "100/30"]


def test_order_serves_overdue_wakes_oldest_first_regardless_of_priority():
    order = kw._admission_order([item(100, LIMIT + 100), item(10, LIMIT + 900)], now=10_000.0)
    assert labels(order) == [f"10/{LIMIT + 900}", f"100/{LIMIT + 100}"]


def test_order_without_timestamp_is_not_treated_as_overdue():
    order = kw._admission_order([item(10, None), item(100, 30)], now=10_000.0)
    assert labels(order) == ["100/30", "10/None"]


@pytest.fixture
def cards(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'board.db'))
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: {'kanban': {'agent_wake_on_events': True}})
    return waiting_card('normal', 10), waiting_card('urgent', 100)


def age_wake(task_id, seconds):
    """Envelhece o aviso não entregue e a assinatura (a assinatura não pode ser mais nova que o aviso)."""
    old = int(time.time()) - seconds
    with kb.connect_closing() as conn:
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='nfos_principal_requested'", (old, task_id))
        conn.execute("UPDATE kanban_notify_subs SET created_at=? WHERE task_id=?", (old - 60, task_id))
        conn.commit()


def first_admitted(cards, tmp_path, monkeypatch):
    """Um ciclo real do notificador com o Principal livre: devolve quem entrou e o recibo de quem ficou."""
    started = []

    async def run():
        adapter = RealAdapter(PlatformConfig(), Platform.TELEGRAM)
        adapter.config.typing_indicator = False
        entered, release = asyncio.Event(), asyncio.Event()

        async def principal(event):
            task, did = next(row for row in cards if row[0].id in event.text)
            started.append(task.id)
            agent = SimpleNamespace(session_id='principal-fair', _session_db=SimpleNamespace(db_path=tmp_path/'state.db'))
            await asyncio.to_thread(initialize_agent_turn_checkpoint, agent,
                                    turn_id=did, user_content=event.text, messages=[])
            entered.set()
            await release.wait()

        adapter._message_handler = principal
        runner = _make_runner(adapter)
        try:
            await _run_one_notifier_tick(monkeypatch, runner)
            await asyncio.wait_for(entered.wait(), 3)
            assert len(adapter._active_sessions) == 1
        finally:
            tasks = list(adapter._session_tasks.values())
            release.set()
            await asyncio.gather(*tasks)

    asyncio.run(run())
    return started


def test_overdue_lower_priority_wake_reaches_the_principal_before_fresh_urgent(cards, tmp_path, monkeypatch):
    """Caso de 08/10: t_39e0a22e (prioridade 10) com aviso parado há horas atrás de cards de prioridade 100."""
    normal, urgent = cards
    age_wake(normal[0].id, LIMIT + 300)
    assert first_admitted(cards, tmp_path, monkeypatch) == [normal[0].id]
    with kb.connect_closing() as conn:
        assert delivery.get_decision(conn, urgent[1])['status'] == 'pending'
        assert conn.execute('SELECT wake_accepted FROM kanban_notify_claims WHERE task_id=?',
                            (urgent[0].id,)).fetchone()[0] == 0


def test_wake_below_the_limit_keeps_urgent_first(cards, tmp_path, monkeypatch):
    normal, urgent = cards
    age_wake(normal[0].id, LIMIT - 300)
    assert first_admitted(cards, tmp_path, monkeypatch) == [urgent[0].id]
    with kb.connect_closing() as conn:
        assert delivery.get_decision(conn, normal[1])['status'] == 'pending'
        assert conn.execute('SELECT wake_accepted FROM kanban_notify_claims WHERE task_id=?',
                            (normal[0].id,)).fetchone()[0] == 0
