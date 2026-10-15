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
"""
import logging

logger = logging.getLogger(__name__)

REASON = 'process_hold'
HOLD = 'process_hold'  # o motivo da recusa da reivindicação, como os demais de claim_task
DECIDER_KINDS = ('decision', 'wait')  # project_workflow.enforce.DECIDER_KINDS, lido sem o módulo
RECHECK_SECONDS = 300
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
    try:
        nfos_waits.declare(conn, task.id, task.current_run_id, reason=REASON, executor=executor,
                           predicate={'kind': 'process_step', 'step': step['step']},
                           subject=f"a etapa {step['name']} do processo", origin={'step': step})
    except _delivery().WorkflowError:
        pass  # a mesma etapa já venceu nesta rodada: o desfecho está gravado, a retenção segue pela etapa
    return HOLD


def wait_policy():
    """A linha de `nfos_waits.policy` da retenção pela etapa do processo, igual em todo projeto."""
    return dict(executors=('principal', 'operator'), predicates=('process_step',), holds_decision=False,
                holds_card=True, on_expire='still_closed',
                outcomes={why: 'still_held' for why in ('deadline', 'attempts', 'unreadable')},
                first_check=RECHECK_SECONDS, recheck=RECHECK_SECONDS, deadline=DEADLINE_SECONDS,
                max_attempts=DEADLINE_SECONDS // RECHECK_SECONDS, withdrawn=_withdrawn, expired=_expired)


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
