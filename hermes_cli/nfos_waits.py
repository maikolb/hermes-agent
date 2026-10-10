"""Regra única de espera do NFOS (WAIT_RULE_20261010).

Maikol, 10/10/2026: "O NFOS tem que ser autonomo, eu não quero ser babá de IA" e "Se não está fluindo temos que corrigir o
NFOS e não cards". A auditoria daquele dia achou onze estados em que um card parava sem saída que o motor alcançasse: prazo
guardado no JSON de uma decisão, lembrete que só acordava de novo o mesmo agente, espera sem prazo final.

Toda espera é uma linha de `nfos_waits`: card, execução e episódio, motivo tipado, executor responsável, predicado que o
código confere, evidência, próxima conferência, prazo final e teto de tentativas. Quem declara é o agente, em campos
estruturados; nada aqui lê texto livre. `step` é a única transição: predicado satisfeito, o card segue; falso, a próxima
conferência tem hora marcada; prazo ou tentativas esgotados, a espera vence com o desfecho gravado. Acordar o Principal
não satisfaz predicado nenhum: lembrete é tentativa e gasta o teto.

O que a regra não faz: não fecha card como [CANCELADO], não conta falha como entrega e não concede saldo, acesso ou
permissão. Prazo e teto vêm de `policy`, igual para todos os projetos.
"""
import json
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS nfos_waits (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), run_id INTEGER, episode INTEGER NOT NULL,
    reason TEXT NOT NULL, executor TEXT NOT NULL, predicate TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','satisfied','expired','withdrawn')), outcome TEXT,
    next_check_at INTEGER NOT NULL, deadline_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    decision_id TEXT, origin TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, closed_at INTEGER,
    CHECK(deadline_at > created_at)
);
CREATE INDEX IF NOT EXISTS nfos_waits_due ON nfos_waits(status,next_check_at);
CREATE INDEX IF NOT EXISTS nfos_waits_task ON nfos_waits(task_id,status);
"""

OPEN, SATISFIED, EXPIRED, WITHDRAWN = 'open', 'satisfied', 'expired', 'withdrawn'
# Quem pode tornar o predicado verdadeiro. requester, operator e external são pessoas ou terceiros; os demais são internos.
EXECUTOR_KINDS = ('requester', 'operator', 'external', 'lab', 'card', 'runtime', 'principal')
REASONS = ('lab_queue', 'principal_decision')
PRINCIPAL = {'kind': 'principal', 'name': 'Principal (sessão coordenadora)'}
_JSON_FIELDS = ('executor', 'predicate', 'evidence', 'origin')


def _delivery():
    from hermes_cli import nfos_delivery
    return nfos_delivery


def policy(reason):
    """A única tabela de prazo, teto e saída, por motivo e igual em todo projeto.

    first_check e recheck: segundos até a primeira conferência e entre as seguintes. deadline: segundos até o prazo final.
    max_attempts: conferências falsas que a espera aguenta. holds_decision: a espera segura uma decisão pendente e perde o
    objeto quando outra rota a responde. on_expire: 'principal' entrega a decisão ao Principal uma vez, com lembretes
    contados (o reparo interno limitado); 'worker' responde a decisão e devolve o card ao executor sem a dependência."""
    d = _delivery()
    if reason == 'lab_queue':
        return dict(executors=('lab',), predicate='lab_receipt', network=True, holds_decision=True, on_expire='principal',
                    first_check=d.LAB_WAIT_RECHECK_SECONDS, recheck=d.LAB_WAIT_RECHECK_SECONDS,
                    deadline=d.LAB_WAIT_ESCALATE_SECONDS,
                    max_attempts=max(1, d.LAB_WAIT_ESCALATE_SECONDS // d.LAB_WAIT_RECHECK_SECONDS),
                    max_unreadable=d.LAB_WAIT_MAX_READ_ERRORS,
                    check=d._lab_queue_check, satisfied=d._lab_queue_released, expired=d._lab_queue_expired)
    if reason == 'principal_decision':
        return dict(executors=('principal',), predicate='decision_resolved', network=False, holds_decision=False,
                    on_expire='worker', first_check=d.DECISION_REMINDER_AFTER, recheck=d.DECISION_REMINDER_GAP,
                    deadline=d.DECISION_REMINDER_AFTER + (d.DECISION_MAX_REMINDERS + 1) * d.DECISION_REMINDER_GAP,
                    max_attempts=d.DECISION_MAX_REMINDERS,
                    check=_decision_resolved, retry=_remind_principal, expired=_return_to_worker)
    raise _delivery().WorkflowError('Unknown wait reason; use one of: ' + ', '.join(REASONS))


class Budget:
    """Teto de conferências de rede de uma varredura, que roda dentro do tick do despacho."""

    def __init__(self, checks, until):
        self.checks, self.until = int(checks), until

    def take(self):
        if self.checks <= 0 or time.monotonic() >= self.until:
            return False
        self.checks -= 1
        return True


def _table(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nfos_waits'").fetchone())


def ensure(conn):
    """Quadro criado antes desta regra ganha a tabela no primeiro uso."""
    if not _table(conn):
        for statement in SCHEMA.split(';'):
            if statement.strip():
                conn.execute(statement)


def _decode(row):
    wait = dict(row)
    for field in _JSON_FIELDS:
        try:
            wait[field] = json.loads(wait[field] or '{}')
        except ValueError:
            wait[field] = {}
    return wait


def get(conn, wait_id):
    if not _table(conn):
        return None
    row = conn.execute('SELECT * FROM nfos_waits WHERE id=?', (wait_id,)).fetchone()
    return _decode(row) if row else None


def open_for_decision(conn, decision_id):
    """A espera aberta que governa esta decisão: prazo, lembrete e saída são dela, não de quem lê a decisão."""
    if not decision_id or not _table(conn):
        return None
    row = conn.execute("SELECT * FROM nfos_waits WHERE decision_id=? AND status='open' ORDER BY created_at DESC,id LIMIT 1",
                       (decision_id,)).fetchone()
    return _decode(row) if row else None


def known_decision(conn, decision_id):
    """Esta decisão já teve espera no registro, aberta ou encerrada?"""
    return _table(conn) and conn.execute('SELECT 1 FROM nfos_waits WHERE decision_id=? LIMIT 1', (decision_id,)).fetchone() is not None


def card_waits(conn, task_id, closed=5):
    """Esperas do card para o `show`: todas as abertas e as últimas encerradas."""
    if not _table(conn):
        return []
    rows = conn.execute("SELECT * FROM nfos_waits WHERE task_id=? AND status='open' ORDER BY created_at,id", (task_id,)).fetchall()
    rows += conn.execute("SELECT * FROM nfos_waits WHERE task_id=? AND status<>'open' ORDER BY closed_at DESC,id LIMIT ?",
                         (task_id, closed)).fetchall()
    return [_decode(row) for row in rows]


def episode(conn, task_id):
    """A rodada do card: quantas vezes ele saiu de concluído e voltou. Espera e recusa valem dentro da rodada."""
    return conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='status' AND json_valid(payload) "
                        "AND json_extract(payload,'$.previous_status')='done'", (task_id,)).fetchone()[0]


def _predicate(reason, spec, value):
    d = _delivery()
    if not isinstance(value, dict) or value.get('kind') != spec['predicate']:
        raise d.WorkflowError(f"A {reason} wait needs the predicate {spec['predicate']}")
    if value['kind'] == 'lab_receipt':
        receipt = value.get('receipt')
        if not isinstance(receipt, str) or not d._LAB_RECEIPT_RX.fullmatch(receipt):
            raise d.WorkflowError('lab_receipt needs the id of the laboratory request')
        return {'kind': 'lab_receipt', 'receipt': receipt}
    decision_id = value.get('decision_id')
    if not isinstance(decision_id, str) or not decision_id:
        raise d.WorkflowError('decision_resolved needs the decision id')
    return {'kind': 'decision_resolved', 'decision_id': decision_id}


def _executor(reason, spec, value):
    if (not isinstance(value, dict) or value.get('kind') not in spec['executors']
            or not isinstance(value.get('name'), str) or not value['name'].strip()):
        raise _delivery().WorkflowError(f"A {reason} wait needs its executor: kind in {list(spec['executors'])} and a name")
    return {'kind': value['kind'], 'name': value['name'].strip()[:160]}


def _same(conn, task_id, reason, predicate, status, round_=None):
    sql = 'SELECT * FROM nfos_waits WHERE task_id=? AND reason=? AND predicate=? AND status=?'
    args = [task_id, reason, _delivery()._json(predicate), status]
    if round_ is not None:
        sql += ' AND episode=?'
        args.append(round_)
    row = conn.execute(sql + ' ORDER BY created_at DESC,id LIMIT 1', args).fetchone()
    return _decode(row) if row else None


def _subject(wait):
    return str((wait.get('origin') or {}).get('subject') or wait['reason'])


def refusal(conn, task_id, *, reason, predicate):
    """A recusa de esperar de novo pelo que já venceu nesta rodada do card; None quando a espera ainda cabe."""
    if not _table(conn):
        return None
    spec = policy(reason)
    expired = _same(conn, task_id, reason, _predicate(reason, spec, predicate), EXPIRED, episode(conn, task_id))
    if not expired:
        return None
    until = time.strftime('%d/%m %H:%MZ', time.gmtime(int(expired['closed_at'] or expired['deadline_at'])))
    return (f"A espera por {_subject(expired)} já venceu neste card em {until}, depois de {expired['attempts']} conferências "
            '(WAIT_RULE_20261010). Não espere de novo pela mesma coisa: siga com o que não depende dela e deixe o resto no card '
            'de continuação (relatório com partial_delivery=true e continuation).')


def declare(conn, task_id, run_id, *, reason, executor, predicate, decision_id=None, subject=None, origin=None, now=None,
            next_check_at=None, attempts=0, evidence=None):
    """Grava a espera. Motivo fora da tabela, executor que o motivo não admite ou predicado de outro tipo: recusada.
    O prazo final e o teto de tentativas saem de `policy`; quem declara não os omite nem os estica. A mesma espera
    declarada de novo devolve a que está aberta; a que já venceu nesta rodada do card é recusada.

    `now` é o início da espera. Um registro antigo adotado traz o início, a conferência e as tentativas que já tinha."""
    d = _delivery()
    spec = policy(reason)
    executor = _executor(reason, spec, executor)
    predicate = _predicate(reason, spec, predicate)
    started = int(now or time.time())
    with d._kb().write_txn(conn, allow_nested=True):
        ensure(conn)
        if not d._kb().get_task(conn, task_id):
            raise d.WorkflowError('Unknown card')
        existing = _same(conn, task_id, reason, predicate, OPEN)
        if existing:
            return existing
        round_ = episode(conn, task_id)
        if _same(conn, task_id, reason, predicate, EXPIRED, round_):
            raise d.WorkflowError(refusal(conn, task_id, reason=reason, predicate=predicate))
        wait_id = 'wait_' + uuid.uuid4().hex[:20]
        saved = dict(origin or {})
        if subject:
            saved['subject'] = str(subject)[:200]
        task = d._kb().get_task(conn, task_id)
        workflow = d.get_workflow(conn, task_id)
        saved.setdefault('instruction_revision', task.instruction_revision)
        saved.setdefault('spec_revision', workflow['spec_revision'] if workflow else None)
        deadline = started + int(spec['deadline'])
        first = int(next_check_at) if next_check_at is not None else started + int(spec['first_check'])
        conn.execute('INSERT INTO nfos_waits(id,task_id,run_id,episode,reason,executor,predicate,evidence,next_check_at,'
                     'deadline_at,attempts,max_attempts,decision_id,origin,created_at,updated_at) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (wait_id, task_id, run_id, round_, reason, d._json(executor), d._json(predicate),
                      d._json(evidence or {}), min(first, deadline), deadline, int(attempts), int(spec['max_attempts']),
                      decision_id, d._json(saved), started, started))
        d._event(conn, task_id, run_id, 'nfos_wait_declared',
                 {'wait_id': wait_id, 'reason': reason, 'executor': executor, 'predicate': predicate,
                  'decision_id': decision_id, 'deadline_at': deadline, 'max_attempts': int(spec['max_attempts'])})
        return get(conn, wait_id)


def _move(conn, wait, **fields):
    """A transição grava só sobre a revisão que leu: dois ticks não aplicam o mesmo passo duas vezes."""
    d = _delivery()
    fields = {key: d._json(value) if key in _JSON_FIELDS else value for key, value in fields.items()}
    names = ','.join(f'{key}=?' for key in fields)
    done = conn.execute(f"UPDATE nfos_waits SET {names},revision=revision+1 WHERE id=? AND revision=? AND status='open'",
                        (*fields.values(), wait['id'], wait['revision']))
    return done.rowcount == 1


def _withdrawn_because(conn, wait, spec):
    d = _delivery()
    task = d._kb().get_task(conn, wait['task_id'])
    if not task or task.status in ('done', 'archived'):
        return 'card_closed'
    if spec['holds_decision']:
        decision = d.get_decision(conn, wait['decision_id']) if wait['decision_id'] else None
        if not decision or decision['status'] != 'pending':
            return 'decision_closed'
    return None


def step(conn, wait_id, *, now=None, budget=None):
    """O passo único de uma espera. Devolve o que fez ('satisfied', 'wait', 'unreadable', 'expired', 'withdrawn') ou None
    quando ainda não é hora, não há orçamento de rede ou outro tick já aplicou o passo."""
    d = _delivery()
    now = int(now or time.time())
    wait = get(conn, wait_id)
    if not wait or wait['status'] != OPEN:
        return None
    spec = policy(wait['reason'])
    gone = _withdrawn_because(conn, wait, spec)
    if gone:
        with d._kb().write_txn(conn, allow_nested=True):
            if not _move(conn, wait, status=WITHDRAWN, outcome=gone, closed_at=now, updated_at=now):
                return None
            d._event(conn, wait['task_id'], wait['run_id'], 'nfos_wait_withdrawn', {'wait_id': wait['id'], 'why': gone})
        return 'withdrawn'
    if now < wait['next_check_at'] and now < wait['deadline_at']:
        return None
    if spec['network']:
        if conn.in_transaction:
            raise d.WorkflowError('A wait is checked outside a write transaction')
        if budget is not None and not budget.take():
            return None
    state, observed = spec['check'](conn, wait)
    evidence = dict(wait['evidence'], checked_at=now, **(observed or {}))
    evidence['unreadable'] = 0 if state is not None else int(wait['evidence'].get('unreadable') or 0) + 1
    with d._kb().write_txn(conn, allow_nested=True):
        if state is True:
            if not _move(conn, wait, status=SATISFIED, outcome='resumed', evidence=evidence, closed_at=now, updated_at=now):
                return None
            if spec.get('satisfied'):
                spec['satisfied'](conn, wait, evidence, now)
            d._event(conn, wait['task_id'], wait['run_id'], 'nfos_wait_satisfied',
                     {'wait_id': wait['id'], 'reason': wait['reason'], 'decision_id': wait['decision_id']})
            return 'satisfied'
        why = ('deadline' if now >= wait['deadline_at'] else 'attempts' if wait['attempts'] >= wait['max_attempts']
               else 'unreadable' if state is None and evidence['unreadable'] >= spec.get('max_unreadable', wait['max_attempts'] + 1)
               else None)
        if why is None:
            later = min(now + int(spec['recheck']), wait['deadline_at'])
            if not _move(conn, wait, attempts=wait['attempts'] + 1, evidence=evidence, next_check_at=later, updated_at=now):
                return None
            if spec.get('retry'):
                spec['retry'](conn, dict(wait, attempts=wait['attempts'] + 1), now)
            return 'wait' if state is False else 'unreadable'
        outcome = 'principal_handoff' if spec['on_expire'] == 'principal' else 'returned_to_worker'
        if not _move(conn, wait, status=EXPIRED, outcome=outcome, evidence=dict(evidence, expired_because=why),
                     closed_at=now, updated_at=now):
            return None
        d._event(conn, wait['task_id'], wait['run_id'], 'nfos_wait_expired',
                 {'wait_id': wait['id'], 'reason': wait['reason'], 'why': why, 'attempts': wait['attempts'],
                  'deadline_at': wait['deadline_at'], 'outcome': outcome, 'decision_id': wait['decision_id']})
        spec['expired'](conn, wait, why, now)
        if spec['on_expire'] == 'principal':
            hand_to_principal(conn, wait, now)
        return 'expired'


def sweep(conn, *, now=None, budget=None):
    """Um passo por espera na hora de conferir, a mais atrasada primeiro. Roda uma vez por tick do despacho."""
    out = []
    if not _table(conn):
        return out
    now = int(now or time.time())
    due = [r[0] for r in conn.execute("SELECT id FROM nfos_waits WHERE status='open' AND (next_check_at<=? OR deadline_at<=?) "
                                      'ORDER BY next_check_at,created_at,id', (now, now))]
    for wait_id in due:
        try:
            done = step(conn, wait_id, now=now, budget=budget)
        except Exception:
            import logging
            logging.getLogger(__name__).warning('WAIT_RULE_20261010 step failed for %s', wait_id, exc_info=True)
            continue
        if done:
            out.append((wait_id, done))
    return out


def hand_to_principal(conn, wait, now):
    """O reparo interno limitado de uma espera vencida: a decisão dela passa ao Principal com lembretes contados. Sem
    decisão dele até o teto, `_return_to_worker` responde e o card segue sem a dependência."""
    return declare(conn, wait['task_id'], wait['run_id'], reason='principal_decision', executor=PRINCIPAL,
                   predicate={'kind': 'decision_resolved', 'decision_id': wait['decision_id']},
                   decision_id=wait['decision_id'], subject=_subject(wait), now=now,
                   origin={'expired_wait': wait['id'], 'expired_reason': wait['reason']})


def _decision_resolved(conn, wait):
    decision = _delivery().get_decision(conn, wait['predicate']['decision_id'])
    if not decision:
        return True, {'decision_status': 'missing'}
    if decision['status'] == 'pending':
        return False, {'decision_status': 'pending'}
    return True, {'decision_status': decision['status'], 'action': decision['action'], 'resolved_at': decision['resolved_at']}


def _remind_principal(conn, wait, now):
    d = _delivery()
    decision = d.get_decision(conn, wait['decision_id'])
    if not decision:
        return
    d._event(conn, wait['task_id'], decision['run_id'], 'nfos_principal_requested',
             {'decision_id': decision['id'], 'kind': decision['kind'], 'question': decision['question'],
              'reminder': wait['attempts'], 'of': wait['max_attempts'], 'wait_id': wait['id'],
              'waiting_minutes': (now - int(wait['created_at'])) // 60})


def _return_to_worker(conn, wait, why, now):
    """Espera vencida que o Principal não decidiu no teto: o motor responde a decisão e o card volta ao executor sem a
    dependência. A resposta leva `wait_rule` no contexto: é do motor, não uma liberação do Principal."""
    d = _delivery()
    decision = d.get_decision(conn, wait['decision_id'])
    if not decision or decision['status'] != 'pending':
        return
    context = json.loads(decision['context'] or '{}') or {}
    context['wait_rule'] = {'wait_id': wait['id'], 'resolved_by': 'runtime', 'outcome': 'returned_to_worker', 'at': now}
    conn.execute("UPDATE nfos_decisions SET context=? WHERE id=? AND status='pending'", (d._json(context), decision['id']))
    answer = (f"CONTINUE (runtime do NFOS, espera vencida): a espera por {_subject(wait)} venceu e o Principal não decidiu em "
              f"{wait['attempts']} lembretes. Não espere de novo pela mesma coisa neste card. Siga com o que não depende dela; "
              'o que depende vai para o card de continuação (relatório com partial_delivery=true e continuation).')
    d.resolve_decision(conn, decision['id'], action='continue', answer=answer, author='Principal')
