"""Card numa etapa que só quem decide tira do lugar não recebe worker (PROCESS_HOLD_20261014).

Caso t_45ae522c (concursa-ai, 14/10/2026): o Principal registrou p7 -> bloqueio no processo do projeto (etapa ``wait``,
raia do Principal) e mandou o worker encerrar o segmento. Para o Kanban o card seguia ``running``/``ready``: nada lia a
etapa. O motor recusou o ``kanban_complete`` pelo gancho do plugin, que não grava evento no Kanban; o laço de objetivo
pediu a conclusão de novo e abriu a mesma pergunta ao Principal; o run venceu o tempo, a continuação abriu outro run e a
resposta CHANGES relançou o worker. Sete perguntas em seis horas, até o Principal pausar o card.

A retenção é um fato do processo: a etapa atual do card é de um tipo que o worker não deixa
(``project_workflow.enforce.DECIDER_KINDS``, ``decision`` e ``wait``). Enquanto ele vale, a reivindicação do card é recusada
(``claim_hold``, chamado de ``kanban_db.claim_task``, por onde passam o relançamento depois de uma resposta CONTINUE ou
CHANGES e a volta de uma continuação por tempo). A espera fica no registro (``nfos_waits``, motivo ``process_hold``), com
o predicado que o código confere (``process_step``: a etapa atual não é mais a etapa retida), prazo e teto da tabela
única. O vencimento grava o desfecho ``still_held`` e não solta o card: a reivindicação continua a ler a etapa, porque
liberar um worker numa etapa do Principal só repete o laço. Quem tira o card da etapa é o Principal (ou a pessoa da raia
humana), registrando a próxima etapa no processo.

PROCESS_HOLD_WAKE_20261016: a retenção era muda. Card retido em etapa ``decision`` da raia do Principal (``_asks``) abre UMA
decisão ao Principal (``request``, o padrão de ``request_execution_budget_review``: pedido antes de qualquer execução, o
despacho pula o card e ``nudge_open_decisions`` lembra), na primeira conferência da espera, 60 s depois da retenção. Vale
também para a espera que já venceu (a reivindicação pede, porque o passo só roda em espera aberta). Pai aberto não pede: o
pedido sairia antes de o card poder andar. Quando a etapa sai (``release``), o motor fecha o pedido como a espera vencida
do Principal fecha o dele (``wait_rule.resolved_by = runtime``), e ``resolve_decision`` recusa responder enquanto o card
segue na etapa (``standing``). Etapa ``wait`` não acorda ninguém: esperar o mundo não é decisão do Principal.
"""
import hashlib
import json
import logging
import time

logger = logging.getLogger(__name__)

REASON = 'process_hold'
HOLD = 'process_hold'  # o motivo da recusa da reivindicação, como os demais de claim_task
DECIDER_KINDS = ('decision', 'wait')  # project_workflow.enforce.DECIDER_KINDS, lido sem o módulo
RECHECK_SECONDS = 300
GRACE_SECONDS = 60  # o Principal que registra a etapa e a seguinte no mesmo turno não recebe pedido
DEADLINE_SECONDS = 24 * 3600


def _delivery():
    from hermes_cli import nfos_delivery
    return nfos_delivery


def held_step(conn, task_id):
    """A etapa do processo que retém o card, ou None. Só no board com o motor em ``enforce`` e no card que o processo
    governa. A consulta que falha não retém: o motor recusa por conta própria a troca que o worker tentar."""
    from hermes_cli import nfos_process
    try:
        position = nfos_process.position(conn, task_id)
    except Exception:  # noqa: BLE001 - a leitura do processo não pode travar o despacho
        logger.warning('PROCESS_HOLD_20261014 could not read the process position of %s', task_id, exc_info=True)
        return None
    if not position or position.get('error') or position.get('mode') != 'enforce' or not position.get('governed'):
        return None
    if position.get('current_kind') not in DECIDER_KINDS:
        return None
    return {'step': position['current'], 'name': position.get('current_name') or position['current'],
            'kind': position['current_kind'], 'lane': position.get('current_lane')}


def claim_hold(conn, task):
    """O motivo de recusar a reivindicação do card pela etapa do processo, ou None. Chamado por `kanban_db.claim_task`
    depois de `nfos_waits.card_held`: com a espera aberta, o passo dela é quem confere a etapa. Sem ela, a etapa é lida e,
    se retém, a espera é declarada. Depois do vencimento a espera não é declarada de novo na mesma rodada, mas a etapa
    continua retendo o card."""
    if task is None or not _delivery().get_workflow(conn, task.id):
        return None
    step = held_step(conn, task.id)
    if step is None:
        return None
    from hermes_cli import nfos_waits
    executor = {'kind': 'principal' if step['lane'] == 'principal' else 'operator',
                'name': f"{step['name']} (etapa {step['kind']} do processo)"}
    grace = {'next_check_at': int(time.time()) + GRACE_SECONDS} if _asks(step) else {}
    try:
        nfos_waits.declare(conn, task.id, task.current_run_id, reason=REASON, executor=executor,
                           predicate={'kind': 'process_step', 'step': step['step']},
                           subject=f"a etapa {step['name']} do processo", origin={'step': step}, **grace)
    except _delivery().WorkflowError:
        # A mesma etapa já venceu nesta rodada: o passo da espera não roda mais, então o pedido sai daqui.
        row = conn.execute("SELECT * FROM nfos_waits WHERE task_id=? AND reason=? AND json_extract(predicate,'$.step')=? "
                           'ORDER BY created_at DESC,id LIMIT 1', (task.id, REASON, step['step'])).fetchone()
        if row:
            request(conn, task.id, nfos_waits._decode(row))
    return HOLD


def wait_policy():
    """A linha de `nfos_waits.policy` da retenção pela etapa do processo, igual em todo projeto."""
    return dict(executors=('principal', 'operator'), predicates=('process_step',), holds_decision=False,
                holds_card=True, on_expire='still_closed',
                outcomes={why: 'still_held' for why in ('deadline', 'attempts', 'unreadable')},
                first_check=RECHECK_SECONDS, recheck=RECHECK_SECONDS, deadline=DEADLINE_SECONDS,
                max_attempts=DEADLINE_SECONDS // RECHECK_SECONDS, withdrawn=_withdrawn, expired=_expired,
                plan=plan, satisfied=lambda conn, wait, evidence, now: release(conn, wait['task_id']))


def left_step(conn, wait, budget=None):
    """Predicado process_step: a etapa atual do card não é mais a etapa retida."""
    from hermes_cli import nfos_process
    try:
        position = nfos_process.position(conn, wait['task_id'])
    except Exception as exc:  # noqa: BLE001
        return None, {'process_error': type(exc).__name__}
    if not position or position.get('error'):
        return None, {'process_error': (position or {}).get('error', 'no position')[:200]}
    current = position.get('current')
    return current != wait['predicate']['step'], {'process_step': current}


def _withdrawn(conn, wait):
    """O processo deixou de governar o card (motor desligado no board, card fora do processo): não há etapa que retenha."""
    from hermes_cli import nfos_process
    try:
        position = nfos_process.position(conn, wait['task_id'])
    except Exception:  # noqa: BLE001 - leitura que falha é conferência ilegível, não retirada
        return None
    if not position or position.get('error'):
        return None if position else 'process_off'
    if position.get('mode') != 'enforce' or not position.get('governed'):
        return 'process_off'
    return None


def _expired(conn, wait, why, now):
    """O vencimento só grava o desfecho (`still_held`, pelo passo do registro). A reivindicação segue lendo a etapa."""
    return None


def _asks(step):
    """A etapa que só o Principal tira do lugar e que ele precisa saber que reteve o card."""
    return (step or {}).get('kind') == 'decision' and (step or {}).get('lane') == 'principal'


def plan(conn, wait, evidence, now):
    """Passo da espera enquanto o card segue na etapa: vence no prazo (como sem plano) e pede ao Principal na conferência."""
    if now >= wait['deadline_at']:
        return {'expire': 'deadline'}
    return {'next_check_at': min(now + RECHECK_SECONDS, wait['deadline_at']), 'deadline_at': wait['deadline_at'],
            'act': lambda conn: request(conn, wait['task_id'], wait)}


def request(conn, task_id, wait):
    """Uma decisão ao Principal por retenção (id fixo pela espera, aberta ou já fechada pelo motor), ou None."""
    step = (wait.get('origin') or {}).get('step') or {}
    if not _asks(step) or conn.execute(
            "SELECT 1 FROM task_links l JOIN tasks p ON p.id=l.parent_id WHERE l.child_id=? "
            "AND p.status NOT IN ('done','archived') LIMIT 1", (task_id,)).fetchone():
        return None  # pai aberto: o card ainda não poderia andar, o pedido não teria o que destravar
    d = _delivery()
    decision_id = 'dec_' + hashlib.sha256(d._json([task_id, REASON, wait['created_at']]).encode()).hexdigest()[:24]
    if d.get_decision(conn, decision_id):
        return decision_id
    from hermes_cli import nfos_process
    try:
        position = nfos_process.position(conn, task_id)
        exits = [f"{n['step']} ({n['name']})" for n in position['next']]
    except Exception:  # noqa: BLE001 - sem as saídas o pedido sai igual, só com o comando
        position, exits = {}, []
    workflow = d.get_workflow(conn, task_id)
    last = conn.execute('SELECT MAX(id) FROM task_runs WHERE task_id=?', (task_id,)).fetchone()[0]
    question = (f"O card está retido na etapa {step['name']} do processo, que só o Principal tira do lugar, e nenhum worker foi "
                'lançado. Registre a próxima etapa no processo (project-workflow step --to <etapa>)'
                + (f": {', '.join(exits)}." if exits else '.') + ' Responder esta decisão sem sair da etapa é recusado.'
                + guide(position))
    context = {'process_hold': {'step': step['step'], 'name': step['name'], 'wait_id': wait['id'], 'exits': exits}}
    conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) '
                 "VALUES(?,?,?,'impediment',?,?,?,?)",
                 (decision_id, task_id, last, question, d._json(context), workflow['spec_revision'], int(time.time())))
    d._event(conn, task_id, last, 'nfos_principal_requested',
             {'decision_id': decision_id, 'kind': 'impediment', 'question': question, 'retained_card': True})
    return decision_id


def guide(position):
    """PROCESS_HOLD_GUIDE_20261017: a meta e a saída que a definição do projeto escreveu para esta etapa, no pedido ao Principal.
    O código não conhece nenhum projeto: a regra de cada um (por exemplo, o caminho curto do dado do aluno no Concursa) vem do texto
    da etapa. Sem esse texto o pedido sai como antes. Cada campo é cortado para o pedido não virar um documento."""
    parts = []
    for label, key in (('Meta', 'current_goal'), ('Saída', 'current_exit')):
        text = ' '.join(str((position or {}).get(key) or '').split())
        if text:
            parts.append(f"{label}: {text[:2500]}{'...' if len(text) > 2500 else ''}")
    return (' Guia desta etapa no processo do projeto. ' + ' '.join(parts)) if parts else ''


def standing(conn, task_id, step):
    """O card segue retido nesta etapa? True; False se saiu ou o processo deixou de governá-lo; None se a leitura falhou."""
    from hermes_cli import nfos_process
    try:
        position = nfos_process.position(conn, task_id)
    except Exception:  # noqa: BLE001
        return None
    if position is None:
        return False
    if position.get('error'):
        return None
    return position.get('mode') == 'enforce' and bool(position.get('governed')) and position.get('current') == step


def release(conn, task_id):
    """Fecha o pedido de retenção quando o card saiu da etapa. É do motor, não do Principal: não conta como decisão dele."""
    for row in conn.execute("SELECT id,context FROM nfos_decisions WHERE task_id=? AND status='pending' AND json_valid(context) "
                            "AND json_extract(context,'$.process_hold.step') IS NOT NULL", (task_id,)).fetchall():
        context = json.loads(row['context'])
        if standing(conn, task_id, context['process_hold']['step']) is False:
            context['wait_rule'] = {'resolved_by': 'runtime', 'outcome': 'left_step', 'at': int(time.time())}
            conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?', (_delivery()._json(context), row['id']))
            _delivery().resolve_decision(conn, row['id'], action='continue', author='Principal',
                                         answer='CONTINUE (runtime do NFOS): o card saiu da etapa retida; nada a decidir aqui.')
