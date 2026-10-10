"""NO_OWNER_QUESTIONS_UNIVERSAL_20261010: nenhum projeto pergunta ao dono, e pausa de manutenção não fica sem dono.

Maikol, 10/10/2026: "Não perguntar ao dono vale pra todos os projetos. As correções no NFOS são pra tudo. Todos os projetos."
Nesse dia havia cinco perguntas ao dono abertas fora do Concursa. Duas eram a obrigação de reparo de uma pausa de manutenção
respondida com pergunta (dovcrm t_e49b4951, conferencia-folha t_56d65bd8): a pausa seguia aberta sem nenhuma decisão pendente,
e a sucessora que a varredura cria não levava a obrigação.
"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as d
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_workspace_repair as repair
from tests.hermes_cli.test_nfos_maintenance_backoff import ANSWER, _context, _obligation, _resume
from tests.hermes_cli.test_nfos_maintenance_pause import _paused_and_exited, running  # noqa: F401
from tests.hermes_cli.test_nfos_no_owner_questions import QUESTION, _ready_card_with_open_question, board, worker  # noqa: F401
from tests.hermes_cli.test_nfos_principal_acceptance import assessment, task_context  # noqa: F401

GRANT = 'PERGUNTA para Maikol: Autoriza uma única concessão adicional de 2700 segundos e 100 iterações para retomar o saldo no mesmo card?'


def _no_switch():
    return {'principal_validation': False}


def _pause_obligations(conn, task, statuses=('pending',)):
    marks = ','.join('?' * len(statuses))
    return [r[0] for r in conn.execute(f"SELECT id FROM nfos_decisions WHERE task_id=? AND status IN ({marks}) "
                                       "AND json_extract(context,'$.maintenance_recovery.pause_run_id')=? ORDER BY created_at, id",
                                       (task.id, *statuses, task.current_run_id))]


def test_a_project_with_no_switch_at_all_refuses_the_owner_question(board, worker, monkeypatch):
    monkeypatch.setattr(review, 'settings', _no_switch)
    task, decision = _ready_card_with_open_question(board, worker.pid)
    with pytest.raises(d.WorkflowError, match='não há pergunta ao Maikol') as refused:
        d.resolve_decision(board, decision, action='human', answer=QUESTION, author='Principal')
    text = str(refused.value)
    assert 'todo projeto' in text and 'entrega parcial' in text and 'não conceda orçamento sem autorização já registrada' in text
    assert 'aluno' not in text and 'US$' not in text and 'Balcão' not in text, 'a recusa não fala de um projeto só'
    assert d.get_decision(board, decision)['status'] == 'pending'
    # O solicitante segue podendo ser consultado, em qualquer projeto.
    d.resolve_decision(board, decision, action='human', author='Principal', answer='Pergunta ao solicitante: pode enviar um vídeo da tela?',
                       public_message={'kind': 'question', 'text': 'Pode enviar um vídeo curto da tela com o erro?'})
    assert d.get_decision(board, decision)['status'] == 'human'


def test_instructions_no_longer_send_anyone_to_the_owner():
    from hermes_cli import nfos_runtime as runtime
    principal = ' '.join(runtime.principal_instructions().split())
    assert 'The owner is never asked, in any project' in principal
    assert 'no budget grant without an authorization already on record' in principal
    assert 'and only from the requester' in principal


def test_reading_the_configuration_fails_and_the_question_is_still_refused(board, worker, monkeypatch):
    def broken():
        raise RuntimeError('config ilegível')
    monkeypatch.setattr(review, 'settings', broken)
    task, _ = _ready_card_with_open_question(board, worker.pid)
    assert d._owner_questions_refused(board, task.id) is True


@pytest.mark.parametrize('conf', [{'principal_validation': False, 'projects': {'pilot': {'owner_questions': True}}},
                                  {'principal_validation': False, 'owner_questions': True}])
def test_only_an_explicit_true_turns_the_owner_question_back_on(board, worker, monkeypatch, conf):
    monkeypatch.setattr(review, 'settings', lambda: conf)
    task, decision = _ready_card_with_open_question(board, worker.pid)
    d.resolve_decision(board, decision, action='human', answer=QUESTION, author='Principal')
    assert d.get_decision(board, decision)['status'] == 'human'
    assert d.review_owner_questions(board) == []


def test_owner_question_open_before_the_rule_returns_to_the_principal_in_any_project(board, worker, monkeypatch):
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation': False, 'owner_questions': True})
    task, decision = _ready_card_with_open_question(board, worker.pid)
    d.resolve_decision(board, decision, action='human', answer=QUESTION, author='Principal')
    worker.kill()
    worker.wait()
    monkeypatch.setattr(review, 'settings', _no_switch)
    (old, new), = d.review_owner_questions(board)
    assert old == decision and d.get_decision(board, old)['status'] == 'superseded'
    assert d.get_decision(board, new)['status'] == 'pending' and 'canal autorizado' in d.get_decision(board, new)['question']
    assert kb.get_task(board, task.id).status == 'ready', 'card sem pausa sai do bloqueio'
    assert d.review_owner_questions(board) == [], 'a varredura não repete'


@pytest.mark.parametrize('answer, returns', [
    ('PERGUNTA para Jhonatan (7550030839): Qual servidor PostgreSQL separado devemos usar?\nAguardar o destino externo.', False),
    ('PERGUNTA para solicitante: Pode enviar um vídeo curto da tela com o erro?', False),
    ('PERGUNTA para Maikol: Autoriza uma concessão adicional de 2700 segundos?', True),
    ('PERGUNTA para o dono do projeto: qual conta de IA usar?', True),
    ('Diagnóstico confirmado pelo card: este card deve permanecer bloqueado.', True),
])
def test_question_recorded_before_public_message_is_read_by_who_it_was_asked_to(board, worker, monkeypatch, answer, returns):
    """Decisão antiga não tem public_message: o destinatário está no prefixo que o decide grava."""
    monkeypatch.setattr(review, 'settings', _no_switch)
    task, decision = _ready_card_with_open_question(board, worker.pid)
    worker.kill()
    worker.wait()
    board.execute("UPDATE nfos_decisions SET status='human', action='human', answer=?, author='Principal', resolved_at=? WHERE id=?",
                  (answer, int(time.time()), decision))
    kb.block_task(board, task.id, reason=answer, kind='needs_input')
    board.commit()
    moved = d.review_owner_questions(board)
    assert [old for old, _ in moved] == ([decision] if returns else [])
    assert d.get_decision(board, decision)['status'] == ('superseded' if returns else 'human')


def test_owner_question_that_was_a_pause_repair_keeps_the_pause_owned(running, monkeypatch):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    did = _obligation(conn, task)
    # Estado real de 10/10: a obrigação de reparo foi respondida com pergunta ao dono quando o projeto ainda perguntava.
    conn.execute("UPDATE nfos_decisions SET status='human', action='human', answer=?, author='Principal', resolved_at=? WHERE id=?",
                 (GRANT, int(time.time()), did))
    conn.commit()
    assert _pause_obligations(conn, task) == [], 'reprodução: pausa aberta e nenhuma obrigação pendente'
    (old, new), = d.review_owner_questions(conn)
    assert old == did and d.get_decision(conn, old)['status'] == 'superseded'
    assert _context(conn, new)['maintenance_recovery']['pause_run_id'] == task.current_run_id, 'a sucessora herda a obrigação'
    assert _pause_obligations(conn, task) == [new]
    assert kb.get_task(conn, task.id).status == 'blocked', 'card em pausa segue bloqueado até o reparo'
    d.resolve_decision(conn, new, action='continue', answer=ANSWER, author='Principal')
    assert d.get_decision(conn, new)['status'] == 'pending', 'resposta sem reparo não solta a obrigação'
    assert _context(conn, new)['maintenance_deferrals'] == 1 and repair.maintenance_pause_pending(conn, task.id)
    d.sweep_awaiting_principal(conn)
    assert _pause_obligations(conn, task) == [new], 'nada duplica enquanto a sucessora deve o reparo'
    _resume(conn, task)
    assert d.sweep_awaiting_principal(conn) == [task.id]
    assert d.get_decision(conn, new)['status'] == 'resolved' and not repair.maintenance_pause_pending(conn, task.id)
    assert kb.claim_task(conn, task.id).id == task.id


def test_maintenance_human_question_that_was_a_pause_repair_also_carries_it(running, monkeypatch):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    did = _obligation(conn, task)
    context = dict(_context(conn, did), assessment={'failure': {'kind': 'runtime_maintenance'}})
    conn.execute("UPDATE nfos_decisions SET status='human', action='human', answer=?, author='Principal', resolved_at=?, context=? WHERE id=?",
                 ('PERGUNTA: quem repara a sonda do runtime?', int(time.time()), json.dumps(context), did))
    conn.commit()
    (old, new), = d.review_maintenance_human_decisions(conn)
    assert _context(conn, new)['maintenance_recovery']['pause_run_id'] == task.current_run_id
    assert kb.get_task(conn, task.id).status == 'blocked'


def test_open_pause_whose_obligation_nobody_owes_gets_it_again(running, monkeypatch):
    conn, task, artifact, process, args = running
    _paused_and_exited(conn, task, artifact, process, args, monkeypatch)
    did = _obligation(conn, task)
    # A obrigação saiu sem o adiamento (resposta humana em board sem portal, ou sucessora antiga sem a herança).
    conn.execute("UPDATE nfos_decisions SET status='resolved', action='continue', answer='Respondido pelo humano', resolved_at=? WHERE id=?",
                 (int(time.time()), did))
    conn.commit()
    assert _pause_obligations(conn, task, ('pending', 'human')) == [] and repair.maintenance_pause_pending(conn, task.id)
    d.sweep_awaiting_principal(conn)
    again = _pause_obligations(conn, task)
    assert len(again) == 1 and again[0] != did, 'pausa aberta ganha a obrigação de novo'
    requested = [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_principal_requested'", (task.id,))]
    assert any(p.get('decision_id') == again[0] and p.get('maintenance_recovery') for p in requested), 'o Principal é avisado'
    d.sweep_awaiting_principal(conn)
    d.sweep_awaiting_principal(conn)
    assert _pause_obligations(conn, task) == again, 'a varredura não duplica'
    assert kb.get_task(conn, task.id).status == 'blocked'
    d.resolve_decision(conn, again[0], action='continue', answer=ANSWER, author='Principal')
    assert d.get_decision(conn, again[0])['status'] == 'pending'
    _resume(conn, task)
    assert d.sweep_awaiting_principal(conn) == [task.id]
    assert d.get_decision(conn, again[0])['status'] == 'resolved'
    d.sweep_awaiting_principal(conn)
    assert _pause_obligations(conn, task, ('pending', 'human')) == [], 'pausa reparada não ganha obrigação'
    assert kb.claim_task(conn, task.id).id == task.id
