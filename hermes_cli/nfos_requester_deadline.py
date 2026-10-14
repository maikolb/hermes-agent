"""Prazo da pergunta ao solicitante (REQUESTER_DEADLINE_20261010), espera do registro desde REQUESTER_WAIT_20261014.

Maikol, 10/10/2026, na sessão que coordena as PRs do NFOS: "3. Mande colocar um prazo" e "As correções no NFOS são pra tudo.
Todos os projetos." Pergunta ao solicitante sem resposta deixava o card bloqueado para sempre: em 10/10 havia card parado há 17 dias.

A pergunta ao solicitante em `human` é uma espera de `nfos_waits` (motivo requester_answer): quem entrega é quem pediu, o
predicado é a resposta gravada na decisão e o passo único do registro avisa, confere e vence. Este módulo dá a linha da
tabela (`wait_policy`), o passo seguinte enquanto a resposta não vem (`_plan`) e o desfecho (`_expired`). A varredura
(`sweep`) só adota a pergunta que ainda não tem espera, inclusive a feita antes desta regra, com o aviso que ela já teve.

O solicitante é avisado antes de vencer, por caminho que o motor controla. No Balcão a pergunta já sai com a linha do prazo e
é republicada 24 h antes. Em card que nasceu num chat (REQUESTER_REMINDER_20261010), o motor pede o aviso com o evento
nfos_requester_reminder, o notificador do gateway o publica como mensagem nova no tópico de origem e grava o recibo
nfos_requester_reminder_delivered; só com esse recibo o prazo passa a contar. Aviso sem recibo é pedido de novo depois de uma
hora, no máximo três vezes por pergunta (REMINDER_LIMIT_20261010). Card sem nenhum dos dois caminhos não pergunta: o decide é
recusado.

No prazo, com o aviso entregue, o motor responde a decisão sozinho (continue, wait_rule.resolved_by=runtime): o card entrega
o que dá para verificar como entrega parcial e o que dependia da resposta vira critério do card de continuação. O Principal
não é acordado; a revisão final continua valendo. Sem aviso entregue, a pergunta vai ao Principal uma vez por rodada do card,
com lembretes contados; na segunda vez o motor segue sozinho, como no prazo.
"""
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

REASON = 'requester_answer'
DEFAULT_HOURS = 48
WARNING_HOURS = 24
DEFAULT_TIMEZONE = 'America/Sao_Paulo'
REMINDER_RETRY_SECONDS = 3600  # aviso pedido e não entregue pelo gateway é pedido de novo, com data nova
# REMINDER_LIMIT_20261010: sem recibo o motor não sabe se a mensagem saiu. Pedir de hora em hora para sempre viraria a
# mesma mensagem repetida no grupo de quem pediu; depois do terceiro pedido a pergunta vai ao Principal.
MAX_REMINDER_REQUESTS = 3
BUSY_RECHECK_SECONDS = 600  # no vencimento, o executor anterior ainda saindo: confere de novo em 10 min
# Conferências falsas que a espera aguenta: aviso, pedidos de aviso, recibo e o card ocupado no vencimento.
MAX_CHECKS = 48
# Vencimento sem aviso entregue: a pergunta vai ao Principal, uma vez por rodada do card.
_UNREACHED = ('no_warning_channel', 'reminder_undelivered', 'attempts')
_GAP = '\n\n'

SILENCE_REFUSAL = (
    'Uma pergunta deste card ao solicitante já foi encerrada pelo motor sem resposta (REQUESTER_DEADLINE_20261010): venceu o '
    'prazo, ou o aviso não chegou a ele pela segunda vez. Não pergunte de novo: decida com continue ou changes, siga com o que já '
    'está conferido e feche como entrega parcial, dizendo no public_delivery o que ficou dependendo dele. Se ele responder, a '
    'resposta chega a você neste card.')
NO_CHANNEL_REFUSAL = (
    'Este card não tem por onde perguntar ao solicitante (REQUESTER_WAIT_20261014): não nasceu num chamado do Balcão nem num chat '
    'com tópico de origem, então o motor não consegue avisar quem pediu nem contar prazo. Decida com continue ou changes: siga com '
    'o que já está conferido e feche como entrega parcial, dizendo no public_delivery o que ficaria dependendo de quem pediu.')

# O que é da pergunta antiga e não passa para a decisão que a substitui. O resto vai junto: uma obrigação de reparo de pausa
# (maintenance_recovery) respondida com pergunta ao solicitante continua tendo dono depois do vencimento.
_NOT_CARRIED = {'human_reply', 'human_reply_history', 'superseded_by', 'superseded_reason', 'public_message', 'public_message_history',
                'requester_deadline', 'support_resume_applied_at', 'assessment', 'acceptance_identity', 'review_identity',
                'quality_refusal', 'report_delta'}


def _delivery():
    from hermes_cli import nfos_delivery
    return nfos_delivery


def _settings():
    try:
        from hermes_cli.nfos_principal_review import settings
        value = settings()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def hours():
    """Prazo em horas, da chave global kanban.delivery.requester_answer_hours, de mais de 0 a 720. Fora disso, ou ausente,
    vale o padrão de 48 h.

    DEADLINE_KEY_FAILS_OFF_20261010 (histórico): a chave ilegível ou em zero desligava o relógio. Em 10/10 uma sessão pôs a
    chave em 0 antes de uma ativação, para nenhum aviso sair, e ninguém a tirou: até 14/10 cinco perguntas a solicitante
    ficaram sem aviso nem prazo, sem um evento sequer. Decisão da coordenação em 14/10 (REQUESTER_WAIT_20261014): não há
    desligador do prazo. Se um dia for preciso, ele será um estado registrado, com evento a cada tick, nunca uma chave."""
    value = _settings().get('requester_answer_hours')
    if type(value) in (int, float) and 0 < value <= 720:
        return value
    return DEFAULT_HOURS


def _lead(limit_hours):
    return int(min(WARNING_HOURS, limit_hours / 2) * 3600)


def _project(conn, task_id):
    d = _delivery()
    try:
        workflow = d.get_workflow(conn, task_id)
        request = d.get_request(conn, workflow['request_id']) if workflow else None
        return json.loads(request['payload']) if request else {}
    except Exception:
        return {}


def channel(conn, task_id):
    """Por onde a pergunta chega a quem pediu: a plataforma de origem do pedido."""
    return str((_project(conn, task_id).get('source') or {}).get('platform') or 'unknown')


def origin(conn, task_id):
    """O chat de onde o pedido veio, quando o card nasceu num chat: é para lá que o aviso do prazo vai."""
    source = _project(conn, task_id).get('source') or {}
    if source.get('platform') in (None, '', 'portal') or not source.get('chat_id'):
        return None
    return {'platform': str(source['platform']), 'chat_id': str(source['chat_id']), 'thread_id': str(source.get('thread_id') or '')}


def _zone(conn, task_id):
    project = _project(conn, task_id).get('project') or {}
    board = project.get('board') or project.get('project_id')
    settings = _settings()
    name = ((settings.get('projects') or {}).get(board) or {}).get('timezone') or settings.get('timezone') or DEFAULT_TIMEZONE
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(name))
    except Exception:
        return timezone(timedelta(hours=-3))


def notice(due_at, zone):
    """A linha do prazo que o solicitante lê junto da pergunta."""
    due = datetime.fromtimestamp(int(due_at), zone)
    return (f'Se não recebermos sua resposta até {due:%d/%m} às {due:%H:%M}, seguimos com o que já temos e fechamos como entrega '
            'parcial. Se você responder depois, o chamado reabre.')


def topic_reminder(recipient, question, due_at, zone):
    """O aviso que vai como mensagem nova ao tópico do card: a pergunta de novo, uma vez, e a linha do prazo."""
    from hermes_cli.nfos_public_text import form_problems
    question = str(question or '').strip()
    lead = question if question.lower().startswith('pergunta') else f'PERGUNTA para {recipient}: {question}'
    lead = re.sub(r'\s*\(\d{6,}\)', '', lead, count=1)  # o id numérico do chat ao lado do nome não é para a pessoa ler
    if not question or form_problems(lead, question=True):
        lead = f'{recipient}, a pergunta deste pedido segue sem resposta.'  # pergunta antiga fora da forma não é republicada
    due = datetime.fromtimestamp(int(due_at), zone)
    return lead + _GAP + (f'Se não recebermos sua resposta até {due:%d/%m} às {due:%H:%M}, seguimos com o que já temos e fechamos como '
                          'entrega parcial. Se você responder depois, retomamos o pedido.')


def requester(context, answer):
    """Quem tem de responder quando a pergunta é ao solicitante; vazio quando é ao dono ou não é pergunta."""
    d = _delivery()
    public = context.get('public_message') if isinstance(context, dict) else None
    if isinstance(public, dict):
        return str(public.get('to') or 'solicitante') if d._requester_question(public) else ''
    addressee = d._human_addressee(answer)  # pergunta antiga, sem mensagem pública: o destinatário está no texto
    # ONE_ADDRESSEE_RULE_20261010: dono ou solicitante se decide num lugar só, pelo destinatário inteiro.
    return addressee if addressee and d._asked_to_requester(context if isinstance(context, dict) else {}, answer) else ''


def _context(decision):
    try:
        context = json.loads((decision or {})['context'] or '{}') if decision else {}
    except (ValueError, TypeError):
        context = {}
    return context if isinstance(context, dict) else {}


def arm(conn, task_id, context, *, now=None):
    """Na publicação da pergunta ao solicitante: recusa a pergunta que o motor não consegue avisar, grava o início e, no
    Balcão, acrescenta a linha do prazo ao texto. A espera do registro é declarada pela varredura do tick (`sweep`)."""
    d = _delivery()
    public = context.get('public_message')
    if not isinstance(public, dict) or not d._requester_question(public):
        return
    route = channel(conn, task_id)
    if route != 'portal' and not origin(conn, task_id):
        raise d.WorkflowError(NO_CHANNEL_REFUSAL)
    limit = hours()
    now = int(now or time.time())
    seed = {'asked_at': now, 'hours': limit, 'channel': route, 'question_text': public['text']}
    if route == 'portal':
        seed['due_at'] = now + int(limit * 3600)
        public['text'] = public['text'].rstrip() + _GAP + notice(seed['due_at'], _zone(conn, task_id))
    context['requester_deadline'] = seed


def refusal_after_silence(conn, task_id):
    """A recusa de uma segunda pergunta ao solicitante depois de um silêncio vencido; None enquanto ele não ficou devendo resposta."""
    silence = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='nfos_requester_silence'", (task_id,)).fetchone()[0]
    if not silence:
        return None
    answered_after = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND id>? AND kind IN "
                                  "('nfos_human_answer_received','nfos_late_requester_answer') LIMIT 1", (task_id, silence)).fetchone()
    return None if answered_after else SILENCE_REFUSAL


def wait_policy():
    """A linha de `nfos_waits.policy` da pergunta ao solicitante: prazo e aviso da chave global, iguais em todo projeto."""
    limit = hours()
    return dict(executors=('requester',), predicates=('human_reply',), holds_decision=False, on_expire='worker',
                outcomes={why: 'principal_handoff' for why in _UNREACHED},
                first_check=int(limit * 3600) - _lead(limit), recheck=REMINDER_RETRY_SECONDS, deadline=int(limit * 3600),
                max_attempts=MAX_CHECKS, withdrawn=_withdrawn, plan=_plan, expired=_expired)


def answered(conn, wait):
    """Predicado human_reply: a resposta de quem pediu está gravada na decisão."""
    context = _context(_delivery().get_decision(conn, wait['predicate']['decision_id']))
    return bool(context.get('human_reply')), {'answered': bool(context.get('human_reply'))}


def _withdrawn(conn, wait):
    """A pergunta deixou de esperar resposta sem que ela viesse: outra rota respondeu ou substituiu a decisão."""
    decision = _delivery().get_decision(conn, wait['decision_id'])
    if not decision:
        return 'decision_missing'
    if decision['status'] != 'human' and not _context(decision).get('human_reply'):
        return 'decision_closed'
    return None


def sweep(conn, *, now=None, dry_run=False):
    """Toda pergunta ao solicitante em `human`, sem resposta, tem a sua espera no registro. A feita antes desta regra é
    adotada com o que já teve (aviso, recibo e prazo). A espera aberta cuja decisão já tem resposta, ou saiu de `human`, é
    conferida no próximo passo do registro, sem esperar a hora marcada. Devolve o que fez (ou faria, em dry_run)."""
    from hermes_cli import nfos_waits
    d = _delivery()
    now = int(now or time.time())
    out = []
    for row in [dict(r) for r in conn.execute("SELECT * FROM nfos_decisions WHERE status='human' ORDER BY created_at,id").fetchall()]:
        try:
            context = _context(row)
            if context.get('human_reply'):
                continue
            recipient = requester(context, row.get('answer'))
            # Só a espera da resposta conta: a decisão que voltou ao Principal tem a espera dele e pode virar pergunta de novo.
            if not recipient or (nfos_waits._table(conn) and conn.execute(
                    'SELECT 1 FROM nfos_waits WHERE decision_id=? AND reason=? LIMIT 1', (row['id'], REASON)).fetchone()):
                continue
            task = d._kb().get_task(conn, row['task_id'])
            if not task or task.status in ('done', 'archived'):
                continue
            adopted = _adoption(conn, row, context, recipient, now)
            out.append({'action': 'adopt', 'decision_id': row['id'], 'task_id': task.id, 'recipient': recipient[:80],
                        'channel': adopted['evidence']['channel'], 'next_check_at': adopted['next_check_at'],
                        'deadline_at': adopted['deadline_at']})
            if not dry_run:
                nfos_waits.declare(conn, task.id, row['run_id'], reason=REASON, executor={'kind': 'requester', 'name': recipient},
                                   predicate={'kind': 'human_reply', 'decision_id': row['id']}, decision_id=row['id'],
                                   subject=f'resposta de {recipient[:80]}', **adopted)
        except Exception:
            import logging
            logging.getLogger(__name__).warning('REQUESTER_WAIT_20261014 adoption failed for %s', row.get('id'), exc_info=True)
    if nfos_waits._table(conn):
        moved = conn.execute("SELECT w.id, w.decision_id FROM nfos_waits w LEFT JOIN nfos_decisions d ON d.id=w.decision_id "
                             "WHERE w.status='open' AND w.reason=? AND w.next_check_at>? AND (d.id IS NULL OR d.status<>'human' "
                             "OR (json_valid(d.context) AND json_extract(d.context,'$.human_reply') IS NOT NULL))",
                             (REASON, now)).fetchall()
        for wait_id, decision_id in moved:
            out.append({'action': 'wake', 'wait_id': wait_id, 'decision_id': decision_id})
            if not dry_run:
                nfos_waits.wake(conn, wait_id, now)
    return out


def _adoption(conn, row, context, recipient, now):
    """Início, evidência, próxima conferência e prazo da espera de uma pergunta: o registro gravado na publicação e, na
    pergunta de antes desta regra, o aviso, o pedido de aviso e o prazo que a varredura antiga já tinha gravado."""
    d = _delivery()
    seed = context.get('requester_deadline') if isinstance(context.get('requester_deadline'), dict) else {}
    public = context.get('public_message') if d._requester_question(context.get('public_message')) else None
    asked = int(seed.get('asked_at') or (public or {}).get('created_at') or row.get('resolved_at') or row['created_at'])
    limit = seed['hours'] if type(seed.get('hours')) in (int, float) and 0 < seed['hours'] <= 720 else hours()
    evidence = {'asked_at': asked, 'hours': limit, 'channel': channel(conn, row['task_id']), 'recipient': recipient[:160]}
    question = seed.get('question_text') or (public or {}).get('text')
    if question:
        evidence['question_text'] = question
    for key in ('warned_at', 'due_at', 'reminder', 'reminder_message_id', 'unarmed'):
        if seed.get(key):
            evidence[key] = seed[key]
    due = int(evidence.get('due_at') or asked + int(limit * 3600))
    reminder = evidence.get('reminder') if isinstance(evidence.get('reminder'), dict) else None
    if evidence.get('unarmed'):
        first = now
    elif evidence.get('warned_at'):
        first = due
    elif reminder:
        first = int(reminder.get('requested_at') or now) + REMINDER_RETRY_SECONDS
        due = max(due, int(reminder.get('due_at') or 0))
    else:
        first = due - _lead(limit)
    return dict(now=asked, evidence=evidence, next_check_at=first, deadline_at=max(due, first, asked + 1))


def _later(wait, evidence, next_check_at, due):
    return {'evidence': evidence, 'next_check_at': int(next_check_at), 'deadline_at': int(max(due, next_check_at))}


def _give_up(conn, wait, evidence, reason='no_warning_channel'):
    """O aviso não chega a quem pediu. A pergunta vai ao Principal uma vez por rodada do card; na segunda, o motor segue
    sozinho como no prazo vencido, sem acordar ninguém."""
    again = conn.execute("SELECT 1 FROM nfos_waits WHERE task_id=? AND reason=? AND episode=? AND outcome='principal_handoff' "
                         'AND id<>? LIMIT 1', (wait['task_id'], REASON, wait['episode'], wait['id'])).fetchone()
    return {'expire': 'unreached_again' if again else reason, 'evidence': dict(evidence, unarmed=reason)}


def _busy(conn, task, run_id):
    from hermes_cli.nfos_runtime import run_termination_pending
    d = _delivery()
    return (task.status not in ('blocked', 'ready') or d._run_process_alive(conn, task.id, run_id)
            or run_termination_pending(conn, task.id, run_id))


def _plan(conn, wait, evidence, now):
    """O passo seguinte enquanto a resposta não vem: a próxima conferência, o prazo e o que gravar ao aplicá-los (`act`), ou
    o motivo do vencimento (`expire`). Ninguém vence sem aviso: o prazo fica a 24 h, no mínimo, do aviso que saiu."""
    d = _delivery()
    if evidence.get('unarmed'):
        return _give_up(conn, wait, evidence, evidence['unarmed'])
    decision = d.get_decision(conn, wait['decision_id'])
    context = _context(decision)
    limit = evidence.get('hours') or hours()
    lead = _lead(limit)
    asked = int(evidence['asked_at'])
    if evidence.get('warned_at'):
        due = int(evidence['due_at'])
        if now < due:
            return _later(wait, evidence, due, due)
        task = d._kb().get_task(conn, wait['task_id'])
        if task and _busy(conn, task, decision['run_id']):
            return _later(wait, evidence, now + BUSY_RECHECK_SECONDS, due)
        return {'expire': 'deadline'}
    if evidence['channel'] == 'portal':
        public = context.get('public_message')
        if not d._requester_question(public):
            return _give_up(conn, wait, evidence)
        due = int(evidence.get('due_at') or asked + int(limit * 3600))
        if now < due - lead:
            return _later(wait, evidence, due - lead, due)
        due = max(due, now + lead)  # ninguém vence sem aviso
        text = (evidence.get('question_text') or public['text']).rstrip() + _GAP + notice(due, _zone(conn, wait['task_id']))
        warned = dict(evidence, warned_at=now, due_at=due)
        return dict(_later(wait, warned, due, due), act=lambda c: _warn(c, wait, warned, text, now))
    where = origin(conn, wait['task_id'])
    if not where:
        return _give_up(conn, wait, evidence)
    reminder = evidence.get('reminder') if isinstance(evidence.get('reminder'), dict) else None
    if reminder:
        receipt = conn.execute("SELECT payload, created_at FROM task_events WHERE task_id=? AND kind='nfos_requester_reminder_delivered' "
                               "AND json_extract(payload,'$.decision_id')=? AND json_extract(payload,'$.due_at')=? ORDER BY id DESC LIMIT 1",
                               (wait['task_id'], wait['decision_id'], reminder['due_at'])).fetchone()
        if receipt:
            warned = dict(evidence, warned_at=int(receipt['created_at']), due_at=int(reminder['due_at']),
                          reminder_message_id=json.loads(receipt['payload']).get('message_id'))
            return dict(_later(wait, warned, max(warned['due_at'], now), warned['due_at']), act=lambda c: _topic_warned(c, wait, warned))
        if now - int(reminder['requested_at']) < REMINDER_RETRY_SECONDS:
            return _later(wait, evidence, int(reminder['requested_at']) + REMINDER_RETRY_SECONDS, reminder['due_at'])
        if int(reminder.get('attempts') or 1) >= MAX_REMINDER_REQUESTS:
            return _give_up(conn, wait, evidence, reason='reminder_undelivered')
    elif now < asked + int(limit * 3600) - lead:
        return _later(wait, evidence, asked + int(limit * 3600) - lead, asked + int(limit * 3600))
    due = max(asked + int(limit * 3600), now + lead)  # ninguém vence sem aviso
    recipient = evidence.get('recipient') or requester(context, decision['answer'])
    question = (evidence.get('question_text') or (context.get('public_message') or {}).get('text')
                or d._human_question_part(decision['answer']))
    text = topic_reminder(recipient, question, due, _zone(conn, wait['task_id']))
    attempts = int(reminder.get('attempts') or 1) + 1 if reminder else 1  # pedido gravado antes da contagem vale por um
    asking = dict(evidence, reminder={'requested_at': now, 'due_at': due, 'attempts': attempts})
    return dict(_later(wait, asking, now + REMINDER_RETRY_SECONDS, due),
                act=lambda c: _request_reminder(c, wait, asking, text, where, recipient))


# PREVIOUS_RELEASE_RECORD_20261014: a release anterior lê o prazo em context.requester_deadline. Todo aviso que esta espera dá
# fica gravado também lá, no formato dela: numa volta de pacote o relógio antigo vê o aviso já dado (ou pedido) e não avisa o
# cliente de novo. Quem decide é a linha do registro; este registro só é lido pela release anterior e pela adoção.
_PREVIOUS_RELEASE_KEYS = ('asked_at', 'hours', 'channel', 'question_text', 'warned_at', 'due_at', 'reminder', 'reminder_message_id')


def _keep_for_previous_release(conn, decision_id, evidence, context=None):
    d = _delivery()
    if context is None:
        context = _context(d.get_decision(conn, decision_id))
    record = {key: evidence[key] for key in _PREVIOUS_RELEASE_KEYS if evidence.get(key) is not None}
    if record.get('warned_at') and record.get('channel') == 'portal':
        record['warn_at'] = record['warned_at']
    context['requester_deadline'] = record
    conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?', (d._json(context), decision_id))


def _request_reminder(conn, wait, asking, text, where, recipient):
    """Pede ao notificador do gateway o aviso no tópico de origem. O prazo só conta do recibo que ele grava."""
    _keep_for_previous_release(conn, wait['decision_id'], asking)
    _delivery()._event(conn, wait['task_id'], wait['run_id'], 'nfos_requester_reminder',
                       {'decision_id': wait['decision_id'], 'due_at': asking['reminder']['due_at'], 'text': text,
                        'recipient': recipient[:80], 'origin': where, 'wait_id': wait['id']})


def _topic_warned(conn, wait, warned):
    _keep_for_previous_release(conn, wait['decision_id'], warned)
    _delivery()._event(conn, wait['task_id'], wait['run_id'], 'nfos_requester_reminded',
                       {'decision_id': wait['decision_id'], 'due_at': warned['due_at'], 'message_id': warned.get('reminder_message_id'),
                        'wait_id': wait['id']})


def _warn(conn, wait, warned, text, now):
    """No Balcão o aviso é a própria pergunta republicada, com a linha do prazo e hora nova: o Balcão a mostra como nova."""
    d = _delivery()
    decision = d.get_decision(conn, wait['decision_id'])
    context = _context(decision)
    public = context['public_message']
    context.setdefault('public_message_history', []).append(dict(public))
    context['public_message'] = dict(public, text=text, created_at=now)
    _keep_for_previous_release(conn, decision['id'], warned, context)
    d._event(conn, wait['task_id'], wait['run_id'], 'nfos_requester_reminded',
             {'decision_id': decision['id'], 'due_at': warned['due_at'], 'wait_id': wait['id']})


def continuation_criterion(recipient, question):
    """O critério que o card de continuação leva quando a pergunta ao solicitante se encerra sem resposta."""
    return (f'Concluir o que ficou pendente quando chegar a resposta à pergunta feita a {str(recipient)[:60]}: '
            f'"{" ".join(str(question or "").split())[:240]}"')


def _when(at):
    return time.strftime('%d/%m %H:%MZ', time.gmtime(int(at)))


def _expired(conn, wait, why, now):
    """Desfecho da espera vencida. No prazo, com aviso entregue (ou sem aviso pela segunda vez na rodada): o motor responde
    a decisão e o card segue como entrega parcial. Sem aviso entregue, a primeira vez: a pergunta vai ao Principal."""
    d = _delivery()
    decision = d.get_decision(conn, wait['decision_id'])
    context = _context(decision)
    if not decision or decision['status'] != 'human' or context.get('human_reply'):
        return
    task = d._kb().get_task(conn, wait['task_id'])
    workflow = d.get_workflow(conn, wait['task_id'])
    if not task or not workflow:
        return
    if why in _UNREACHED:
        _hand_over(conn, wait, decision, context, task, workflow, why, now)
    else:
        _continue_partial(conn, wait, decision, context, task, workflow, why, now)


def _supersede(conn, decision, context, task, workflow, question, carried, reason, now):
    d = _delivery()
    new_id = 'dec_' + uuid.uuid4().hex[:20]
    successor = {key: value for key, value in context.items() if key not in _NOT_CARRIED}
    successor.update(supersedes=decision['id'], **carried)
    context.update(superseded_by=new_id, superseded_reason=reason)
    conn.execute("UPDATE nfos_decisions SET status='superseded',context=? WHERE id=?", (d._json(context), decision['id']))
    conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES(?,?,?,?,?,?,?,?)',
                 (new_id, task.id, decision['run_id'], 'impediment', question, d._json(successor), workflow['spec_revision'], now))
    return new_id


def _release(conn, task, context):
    """O card sai do bloqueio da pergunta. Card em pausa de manutenção segue bloqueado: espera o reparo, não a resposta."""
    d = _delivery()
    if task.status == 'blocked' and not d._carried_pause(context):
        d._kb().unblock_task(conn, task.id)


def _continue_partial(conn, wait, decision, context, task, workflow, why, now):
    d = _delivery()
    evidence = wait['evidence']
    refused = d._human_question_part(decision['answer'])
    recipient = evidence.get('recipient') or 'O solicitante'
    limit = evidence.get('hours') or hours()
    if why == 'deadline':
        cause = (f'{recipient} não respondeu à pergunta deste card até {_when(evidence["due_at"])} ({limit:g} h, com aviso '
                 'antes de vencer)')
        record = {'requester_silence': {'asked_at': evidence['asked_at'], 'due_at': evidence['due_at'], 'hours': limit,
                                        'warned_at': evidence.get('warned_at')}}
    else:
        cause = f'o aviso da pergunta deste card não chegou a {recipient}, pela segunda vez nesta rodada'
        record = {'requester_unreached': {'asked_at': evidence['asked_at'], 'why': evidence.get('unarmed') or why}}
    question = (f'Pergunta ao solicitante encerrada pelo motor (REQUESTER_DEADLINE_20261010): {cause}. Pergunta sem resposta: '
                + refused[:700])
    criterion = continuation_criterion(recipient, evidence.get('question_text') or refused)
    record['continuation_criterion'] = criterion
    record['wait_rule'] = {'wait_id': wait['id'], 'resolved_by': 'runtime', 'outcome': 'returned_to_worker', 'at': now}
    new_id = _supersede(conn, decision, context, task, workflow, question, record, 'requester_silence', now)
    d._event(conn, task.id, decision['run_id'], 'nfos_requester_silence',
             {'decision_id': decision['id'], 'new_decision_id': new_id, 'asked_at': evidence['asked_at'],
              'due_at': evidence.get('due_at'), 'why': why, 'wait_id': wait['id'], 'question': refused[:400]})
    # O worker relançado lê esta resposta cortada em 600 caracteres (CURRENT_DECISIONS_20261010): o que ele tem de fazer e o
    # critério vêm primeiro; o motivo e a pergunta inteira vêm depois.
    answer = ('CONTINUE (runtime do NFOS): feche como entrega parcial (partial_delivery=true) com o que já está conferido; o card '
              f'de continuação (continuation) leva este critério obrigatório: {criterion}. Não pergunte de novo. Motivo: {cause}. Em '
              'public_delivery.summary diga a quem pediu o que foi entregue e o que ficou dependendo da resposta. Se ele responder '
              'depois, a resposta chega ao Principal neste card, ou o chamado reabre. Pergunta sem resposta: ' + refused[:700])
    d.resolve_decision(conn, new_id, action='continue', answer=answer, author='Principal')
    _release(conn, d._kb().get_task(conn, task.id), context)


def _hand_over(conn, wait, decision, context, task, workflow, why, now):
    from hermes_cli import nfos_waits
    d = _delivery()
    evidence = wait['evidence']
    refused = d._human_question_part(decision['answer'])
    if why == 'no_warning_channel':
        cause = ('a pergunta deste card ao solicitante não tem caminho de aviso: no Balcão ela não saiu como public_message '
                 '{kind: "question"}, ou o card não tem chat de origem')
        retry = ('Num chamado do Balcão, perguntar de novo com public_message {kind: "question"} é permitido uma vez: o motor '
                 'avisa e conta o prazo. ')
    elif why == 'reminder_undelivered':
        cause = (f'o aviso do prazo não chegou ao tópico de origem: o gateway não gravou o recibo depois de '
                 f'{MAX_REMINDER_REQUESTS} pedidos, um por hora (REQUESTER_REMINDER_20261010)')
        retry = 'Perguntar de novo é permitido uma vez: se o aviso falhar de novo, o motor segue sozinho como entrega parcial. '
    else:
        cause = 'a espera pela resposta gastou o teto de conferências sem chegar ao prazo (o card ficou ocupado no vencimento)'
        retry = ''
    question = (f'Pergunta ao solicitante sem aviso entregue (REQUESTER_WAIT_20261014): {cause}. O prazo não correu, porque '
                'ninguém vence sem aviso. Decida com continue ou changes: siga com o que já está conferido e feche como entrega '
                'parcial, dizendo em public_delivery.summary o que ficou dependendo de quem pediu. ' + retry
                + 'Pergunta sem aviso: ' + refused[:700])
    carried = {'requester_unreached': {'asked_at': evidence['asked_at'], 'why': why, 'channel': evidence.get('channel'),
                                       'wait_id': wait['id']}}
    new_id = _supersede(conn, decision, context, task, workflow, question, carried, 'requester_unreached', now)
    d._event(conn, task.id, decision['run_id'], 'nfos_requester_unreached',
             {'decision_id': decision['id'], 'new_decision_id': new_id, 'why': why, 'channel': evidence.get('channel'),
              'wait_id': wait['id'], 'question': refused[:400]})
    d._event(conn, task.id, decision['run_id'], 'nfos_principal_requested', {'decision_id': new_id, 'kind': 'impediment', 'question': question})
    if not d._carried_pause(context):  # a obrigação de reparo da pausa já tem dono e lembrete próprios
        nfos_waits.hand_to_principal(conn, dict(wait, decision_id=new_id), now)
    _release(conn, task, context)


def late_answer(conn, task_id, *, answer, source):
    """Resposta que chega depois do prazo, com o card ainda aberto: vai ao Principal como informação. False quando não é o caso."""
    d = _delivery()
    task = d._kb().get_task(conn, task_id)
    if not task or task.status in ('done', 'archived'):
        return False  # card fechado segue o caminho que já existe: nota do suporte ou reabertura do chamado
    silence = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_requester_silence' ORDER BY id DESC LIMIT 1",
                           (task_id,)).fetchone()
    if not silence:
        return False
    message_id = source.get('message_id')
    if message_id and conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='nfos_late_requester_answer' "
                                   "AND json_extract(payload,'$.source.message_id')=?", (task_id, str(message_id))).fetchone():
        return True  # a mesma mensagem reenviada não vira segunda decisão
    successor = d.get_decision(conn, json.loads(silence['payload']).get('new_decision_id'))
    workflow = d.get_workflow(conn, task_id)
    if not successor or not workflow:
        return False
    reply = {'answer': answer, 'source': source, 'author': source.get('actor') or 'Human', 'received_at': int(time.time())}
    if successor['status'] == 'pending':
        target = successor['id']
        context = json.loads(successor['context'] or '{}') or {}
        context['human_reply'] = reply
        conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?', (d._json(context), target))
        question = successor['question']
    else:
        target = 'dec_' + uuid.uuid4().hex[:20]
        question = ('O solicitante respondeu depois do prazo da pergunta (REQUESTER_DEADLINE_20261010). A resposta está em human_reply. '
                    'Decida com continue ou changes o que ela muda no que já foi feito; não pergunte de novo a mesma coisa.')
        conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (target, task_id, successor['run_id'], 'impediment', question,
                      d._json({'late_requester_answer': True, 'supersedes': successor['id'], 'human_reply': reply}),
                      workflow['spec_revision'], reply['received_at']))
    d._event(conn, task_id, None, 'nfos_late_requester_answer', {'answer': answer, 'source': source, 'decision_id': target})
    d._event(conn, task_id, None, 'nfos_principal_requested',
             {'decision_id': target, 'kind': 'impediment', 'question': question, 'human_reply_available': True})
    return True
