"""PRIORITY_INHERITANCE_20261016: o card que outros esperam por dependência entra na fila com a prioridade de quem espera.

Medido em 16/10/2026 no quadro concursa-ai: o card de correção t_fa37e8d2 nasceu com prioridade 0 e ficou 28 h em `ready` sem
nenhuma execução, com 38 cards de prioridade 10 a 100 na frente e 2 vagas. Dois cards de prioridade 100 esperavam por ele
(`_declare_dependency_wait`). O despacho ordena por prioridade; o card que destrava os outros era o último da fila.
"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d
from hermes_cli import nfos_waits

CARD = {"kind": "card", "name": "executor do card da causa"}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    (tmp_path / "profiles" / "default").mkdir(parents=True)
    with kb.connect_closing() as c:
        d.init_schema(c)
        nfos_waits.ensure(c)
        yield c


def _card(conn, tid, priority, status="ready", age=0):
    """Card de trabalho com id fixo. `age` em segundos: maior é mais velho, e a igual prioridade vale o mais velho."""
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, task_role, priority, assignee) VALUES (?, ?, ?, ?, 'work', ?, 'default')",
        (tid, tid, status, int(time.time()) - age, priority),
    )
    conn.commit()
    return tid


def _wait(conn, waiter, target, status="open", until="done"):
    """Espera aberta de `waiter` pelo card `target`, igual à que `_declare_dependency_wait` grava."""
    now = int(time.time())
    conn.execute(
        "INSERT INTO nfos_waits(id,task_id,episode,reason,executor,predicate,next_check_at,deadline_at,max_attempts,status,"
        "created_at,updated_at) VALUES(?,?,0,'external_dependency',?,?,?,?,5,?,?,?)",
        (f"wait_{waiter}_{target}", waiter, json.dumps(CARD), json.dumps({"kind": "card", "task_id": target, "until": until}),
         now + 300, now + 7200, status, now - 10, now - 10),
    )
    conn.commit()
    return f"wait_{waiter}_{target}"


def _order(conn):
    return [r["id"] for r in kb._dispatch_queue(conn, "ready", work_only=True)]


def _original_order(conn):
    """A consulta que o despacho usava antes da herança."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE status = 'ready' AND task_role = 'work' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC").fetchall()]


def _board_like_concursa(conn):
    """O quadro medido: o card de correção com 0, dois cards de 100 que esperam por ele, uma fila de 10 a 90 na frente."""
    _card(conn, "t_causa", 0, age=100)
    # `triage`: o despacho de teste não promove o card esperante, que no quadro real fica bloqueado com a decisão aberta.
    _card(conn, "t_espera_a", 100, status="triage", age=500)
    _card(conn, "t_espera_b", 100, status="triage", age=500)
    for i, priority in enumerate((90, 50, 50, 10)):
        _card(conn, f"t_fila{i}", priority, age=400 - i)
    _wait(conn, "t_espera_a", "t_causa")
    _wait(conn, "t_espera_b", "t_causa")


def test_awaited_card_leaves_before_lower_priority_cards(conn):
    _board_like_concursa(conn)
    assert _original_order(conn)[-1] == "t_causa", "sem a herança o card que destrava os outros é o último da fila"
    assert _order(conn)[0] == "t_causa" and _order(conn)[1:] == ["t_fila0", "t_fila1", "t_fila2", "t_fila3"]

    spawned = []
    with kb.connect_closing() as other:
        kb.dispatch_once(other, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 4242, max_spawn=2)
    assert spawned[:2] == ["t_causa", "t_fila0"], "com 2 vagas, o card esperado ocupa a primeira"


def test_inheritance_lasts_only_while_the_wait_is_open(conn):
    _board_like_concursa(conn)
    assert _order(conn)[0] == "t_causa"
    conn.execute("UPDATE nfos_waits SET status='withdrawn' WHERE task_id='t_espera_a'")
    conn.commit()
    assert _order(conn)[0] == "t_causa", "a outra espera, ainda aberta, segura a herança"
    for closed in ("satisfied", "expired", "withdrawn"):
        conn.execute("UPDATE nfos_waits SET status=?, closed_at=? WHERE task_id='t_espera_b'", (closed, int(time.time())))
        conn.commit()
        assert _order(conn) == _original_order(conn) and _order(conn)[-1] == "t_causa", closed
        conn.execute("UPDATE nfos_waits SET status='open', closed_at=NULL WHERE task_id='t_espera_b'")
        conn.commit()
        assert _order(conn)[0] == "t_causa", "reaberta a espera, volta a herança"


def test_closed_waiter_does_not_lend_priority(conn):
    _board_like_concursa(conn)
    for waiter in ("t_espera_a", "t_espera_b"):
        conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (int(time.time()), waiter))
    conn.commit()
    assert _order(conn) == _original_order(conn)


def test_declared_priority_is_never_rewritten(conn):
    _board_like_concursa(conn)
    rows = {r["id"]: r for r in kb._dispatch_queue(conn, "ready", work_only=True, record=True)}
    assert rows["t_causa"]["priority"] == 100 and rows["t_causa"]["declared_priority"] == 0
    assert rows["t_causa"]["priority_inherited_from"]["origin_card"] in ("t_espera_a", "t_espera_b")
    assert rows["t_fila0"]["priority"] == rows["t_fila0"]["declared_priority"] == 90
    assert rows["t_fila0"]["priority_inherited_from"] is None
    column = dict(conn.execute("SELECT id, priority FROM tasks").fetchall())
    assert column["t_causa"] == 0 and column["t_espera_a"] == 100 and column["t_fila3"] == 10
    assert kb.get_task(conn, "t_causa").priority == 0


def test_board_without_card_waits_keeps_its_order(conn):
    for i, priority in enumerate((10, 100, 0, 50, 50, 90, 0)):
        _card(conn, f"t_{i}", priority, age=300 - i * 10)
    _card(conn, "t_bloqueado", 100, status="blocked", age=900)
    _card(conn, "t_review", 100, status="review", age=900)
    assert _order(conn) == _original_order(conn)
    assert all(r["priority_inherited_from"] is None and r["priority"] == r["declared_priority"]
               for r in kb._dispatch_queue(conn, "ready", work_only=True))


def test_wait_that_is_not_for_a_card_does_not_lend(conn):
    _board_like_concursa(conn)
    # Predicado de outro tipo que cita um card: só o tipo `card` é dependência de card.
    conn.execute("UPDATE nfos_waits SET predicate=?", (json.dumps({"kind": "decision_resolved", "decision_id": "dec_x", "task_id": "t_causa"}),))
    conn.commit()
    assert _order(conn) == _original_order(conn)


def test_never_lowers_a_card_that_already_outranks_the_waiter(conn):
    _card(conn, "t_espera", 50, status="blocked")
    _card(conn, "t_causa", 90)
    _card(conn, "t_outro", 70)
    _wait(conn, "t_espera", "t_causa")
    row = next(r for r in kb._dispatch_queue(conn, "ready", work_only=True) if r["id"] == "t_causa")
    assert row["priority"] == 90 and row["priority_inherited_from"] is None
    assert _order(conn) == ["t_causa", "t_outro"]


def test_chain_passes_the_priority_down_to_the_last_card(conn):
    _card(conn, "t_topo", 100, status="blocked")
    _card(conn, "t_meio", 5, status="blocked")
    _card(conn, "t_fundo", 0, age=10)
    _card(conn, "t_rival", 60, age=50)
    _wait(conn, "t_topo", "t_meio")
    _wait(conn, "t_meio", "t_fundo")
    inherited = nfos_waits.inherited_priorities(conn)
    assert inherited["t_fundo"]["priority"] == 100 and inherited["t_fundo"]["origin_card"] == "t_topo"
    assert inherited["t_fundo"]["waiter"] == "t_meio" and inherited["t_meio"]["priority"] == 100
    assert _order(conn) == ["t_fundo", "t_rival"]


def test_origin_is_recorded_once_per_wait(conn):
    _board_like_concursa(conn)
    for _ in range(3):
        kb._dispatch_queue(conn, "ready", work_only=True, record=True)
    events = [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id='t_causa' AND kind='nfos_priority_inherited'")]
    assert len(events) == 1, "um evento por espera e prioridade, não um por tick"
    event = events[0]
    assert event["priority"] == 100 and event["declared_priority"] == 0 and event["origin_priority"] == 100
    assert event["waiter"] in ("t_espera_a", "t_espera_b") and event["wait_id"].startswith("wait_t_espera_")
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='nfos_priority_inherited' AND task_id!='t_causa'").fetchone()[0] == 0
    kb._dispatch_queue(conn, "ready", work_only=True, record=False)
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='nfos_priority_inherited'").fetchone()[0] == 1


def test_real_dependency_declaration_feeds_the_dispatch_order(conn):
    """A espera gravada por `nfos_waits.declare`, a mesma de `_declare_dependency_wait`, e não uma linha escrita à mão."""
    _card(conn, "t_causa", 0, age=100)
    _card(conn, "t_espera", 100, status="blocked", age=500)
    _card(conn, "t_fila", 50, age=300)
    wait = nfos_waits.declare(conn, "t_espera", None, reason="external_dependency", executor=CARD,
                              predicate={"kind": "card", "task_id": "t_causa"}, subject="correção do portão")
    assert wait["status"] == "open" and _order(conn) == ["t_causa", "t_fila"]
    conn.execute("UPDATE nfos_waits SET status='satisfied', closed_at=? WHERE id=?", (int(time.time()), wait["id"]))
    conn.commit()
    assert _order(conn) == ["t_fila", "t_causa"]
    assert kb.get_task(conn, "t_causa").priority == 0
