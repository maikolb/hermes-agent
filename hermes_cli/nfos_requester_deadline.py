"""Prazo da pergunta ao solicitante (REQUESTER_DEADLINE_20261010).

Maikol, 10/10/2026, na sessão que coordena as PRs do NFOS: "3. Mande colocar um prazo" e "As correções no NFOS são pra tudo.
Todos os projetos." Pergunta ao solicitante sem resposta deixava o card bloqueado para sempre: em 10/10 havia card parado há 17 dias.

O relógio vale para a decisão em `human` cuja pergunta vai a quem pediu, não ao dono. O solicitante é avisado antes: a pergunta
já sai com a linha do prazo e é republicada 24 h antes de vencer. No vencimento o motor não fecha nada: a pergunta volta ao
Principal como decisão pendente, com a instrução de seguir com o que já está conferido e fechar como entrega parcial. A revisão
final continua valendo.

O prazo só vence onde o aviso chega ao solicitante por caminho que o motor controla. No Balcão é a mensagem pública da
decisão. Em card que nasceu num chat (REQUESTER_REMINDER_20261010), o motor pede o aviso com o evento
nfos_requester_reminder, o notificador do gateway o publica como mensagem nova no tópico de origem e grava o recibo
nfos_requester_reminder_delivered; só com esse recibo o prazo passa a contar. Card sem um dos dois caminhos fica sem prazo, com
o motivo registrado.
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone

DEFAULT_HOURS = 48
WARNING_HOURS = 24
DEFAULT_TIMEZONE = 'America/Sao_Paulo'
REMINDER_RETRY_SECONDS = 3600  # aviso pedido e não entregue pelo gateway é pedido de novo, com data nova
_GAP = '\n\n'

SILENCE_REFUSAL = (
    'O solicitante já deixou uma pergunta deste card sem resposta até o prazo (REQUESTER_DEADLINE_20261010). Não pergunte de novo: '
    'decida com continue ou changes, siga com o que já está conferido e feche como entrega parcial, dizendo no public_delivery o que '
    'ficou dependendo dele. Se ele responder, a resposta chega a você neste card.')

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
    """Prazo em horas, da chave global kanban.delivery.requester_answer_hours. Zero desliga o relógio.

    DEADLINE_KEY_FAILS_OFF_20261010: a chave é o que segura aviso e vencimento para cliente. Presente e ilegível (false,
    "0" entre aspas, vazia, fora de 0 a 720) desliga o relógio; antes caía no padrão e ligava o prazo sozinha. Só a chave
    ausente vale o padrão."""
    settings = _settings()
    if 'requester_answer_hours' not in settings:
        return DEFAULT_HOURS
    value = settings['requester_answer_hours']
    if type(value) in (int, float) and 0 <= value <= 720:
        return value
    return 0


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


def arm(conn, task_id, context, *, now=None):
    """Na publicação da pergunta ao solicitante: grava o prazo e acrescenta a linha dele ao texto que o Balcão mostra."""
    limit = hours()
    public = context.get('public_message')
    if not limit or not isinstance(public, dict) or not _delivery()._requester_question(public):
        return
    now = int(now or time.time())
    route = channel(conn, task_id)
    deadline = {'asked_at': now, 'hours': limit, 'channel': route, 'question_text': public['text']}
    if route == 'portal':
        due = now + int(limit * 3600)
        deadline.update(due_at=due, warn_at=due - _lead(limit), warned_at=None)
        public['text'] = public['text'].rstrip() + '\n\n' + notice(due, _zone(conn, task_id))
    elif origin(conn, task_id):
        deadline['warn_at'] = now + int(limit * 3600) - _lead(limit)  # o prazo conta do recibo do aviso no tópico
    else:
        deadline['unarmed'] = 'no_warning_channel'
    context['requester_deadline'] = deadline


def refusal_after_silence(conn, task_id):
    """A recusa de uma segunda pergunta ao solicitante depois de um silêncio vencido; None enquanto ele não ficou devendo resposta."""
    silence = conn.execute("SELECT max(id) FROM task_events WHERE task_id=? AND kind='nfos_requester_silence'", (task_id,)).fetchone()[0]
    if not silence:
        return None
    answered = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND id>? AND kind IN "
                            "('nfos_human_answer_received','nfos_late_requester_answer') LIMIT 1", (task_id, silence)).fetchone()
    return None if answered else SILENCE_REFUSAL


def sweep(conn, *, now=None, dry_run=False):
    """Avisa, vence e devolve ao Principal as perguntas ao solicitante sem resposta. Devolve o que fez (ou faria, em dry_run)."""
    d = _delivery()
    limit = hours()
    out = []
    if not limit:
        return out
    from hermes_cli.nfos_runtime import run_termination_pending
    now = int(now or time.time())
    for row in [dict(r) for r in conn.execute("SELECT * FROM nfos_decisions WHERE status='human' ORDER BY created_at,id").fetchall()]:
        try:
            context = json.loads(row.get('context') or '{}') or {}
        except Exception:
            context = {}
        if not isinstance(context, dict) or context.get('human_reply'):
            continue
        recipient = requester(context, row.get('answer'))
        if not recipient:
            continue
        task = d._kb().get_task(conn, row['task_id'])
        workflow = d.get_workflow(conn, row['task_id'])
        if not task or not workflow or task.status not in ('blocked', 'ready'):
            continue
        if d._run_process_alive(conn, task.id, row['run_id']) or run_termination_pending(conn, task.id, row['run_id']):
            continue
        public = context.get('public_message') if isinstance(context.get('public_message'), dict) else None
        deadline = context.get('requester_deadline') if isinstance(context.get('requester_deadline'), dict) else {}
        asked = int(deadline.get('asked_at') or (public or {}).get('created_at') or row.get('resolved_at') or row['created_at'])
        route = channel(conn, task.id)
        base = {'decision_id': row['id'], 'task_id': task.id, 'recipient': recipient[:80], 'channel': route, 'asked_at': asked}
        if route != 'portal':
            out.append(_topic(conn, row, task, workflow, context, deadline, recipient, asked, limit, now, base, dry_run))
            continue
        if not public or public.get('kind') != 'question':
            out.append(dict(base, action='no_warning_channel'))
            if not dry_run and deadline.get('unarmed') != 'no_warning_channel':
                _unarmed(conn, row, asked, limit, route)
            continue
        due = int(deadline.get('due_at') or asked + int(limit * 3600))
        if not deadline.get('warned_at'):
            warn_at = int(deadline.get('warn_at') or due - _lead(limit))
            if now < warn_at:
                out.append(dict(base, action='waiting', warn_at=warn_at, due_at=due))
                continue
            due = max(due, now + _lead(limit))  # ninguém vence sem aviso
            text = (deadline.get('question_text') or public['text']).rstrip() + '\n\n' + notice(due, _zone(conn, task.id))
            out.append(dict(base, action='warn', due_at=due, text=text))
            if not dry_run:
                _warn(conn, row, asked, limit, due, text, now)
            continue
        if now < due:
            out.append(dict(base, action='waiting', due_at=due, warned_at=deadline['warned_at']))
            continue
        out.append(dict(base, action='expire', due_at=due, warned_at=deadline['warned_at']))
        if not dry_run:
            _expire(conn, row, task, workflow, asked, limit, due, now)
    return out


def _topic(conn, row, task, workflow, context, deadline, recipient, asked, limit, now, base, dry_run):
    """Card que nasceu num chat: pede o aviso ao gateway, espera o recibo e só então conta o prazo."""
    d = _delivery()
    where = origin(conn, task.id)
    if not where:
        if not dry_run and deadline.get('unarmed') != 'no_warning_channel':
            _unarmed(conn, row, asked, limit, base['channel'])
        return dict(base, action='no_warning_channel')
    if deadline.get('warned_at'):
        due = int(deadline['due_at'])
        if now < due:
            return dict(base, action='waiting', due_at=due, warned_at=deadline['warned_at'])
        if not dry_run:
            _expire(conn, row, task, workflow, asked, limit, due, now)
        return dict(base, action='expire', due_at=due, warned_at=deadline['warned_at'])
    requested = deadline.get('reminder') if isinstance(deadline.get('reminder'), dict) else None
    if requested:
        receipt = conn.execute("SELECT payload, created_at FROM task_events WHERE task_id=? AND kind='nfos_requester_reminder_delivered' "
                               "AND json_extract(payload,'$.decision_id')=? AND json_extract(payload,'$.due_at')=? ORDER BY id DESC LIMIT 1",
                               (task.id, row['id'], requested['due_at'])).fetchone()
        if receipt:
            if not dry_run:
                _topic_warned(conn, row, int(receipt['created_at']), json.loads(receipt['payload']).get('message_id'))
            return dict(base, action='warned', due_at=requested['due_at'], warned_at=int(receipt['created_at']))
        if now - int(requested['requested_at']) < REMINDER_RETRY_SECONDS:
            return dict(base, action='waiting_delivery', due_at=requested['due_at'])
    else:
        warn_at = int(deadline.get('warn_at') or asked + int(limit * 3600) - _lead(limit))
        if now < warn_at:
            return dict(base, action='waiting', warn_at=warn_at)
    due = max(asked + int(limit * 3600), now + _lead(limit))  # ninguém vence sem aviso
    public = context.get('public_message') if isinstance(context.get('public_message'), dict) else None
    question = deadline.get('question_text') or (public or {}).get('text') or d._human_question_part(row.get('answer'))
    text = topic_reminder(recipient, question, due, _zone(conn, task.id))
    if not dry_run:
        _request_reminder(conn, row, asked, limit, due, text, where, recipient, now)
    return dict(base, action='request_reminder', due_at=due, text=text, origin=where)


def _request_reminder(conn, row, asked, limit, due, text, where, recipient, now):
    d = _delivery()
    with d._kb().write_txn(conn, allow_nested=True):
        context = _still_waiting(conn, row['id'])
        if context is None:
            return
        previous = context.get('requester_deadline') if isinstance(context.get('requester_deadline'), dict) else {}
        context['requester_deadline'] = {'asked_at': asked, 'hours': limit, 'channel': where['platform'],
                                         'reminder': {'requested_at': now, 'due_at': due}}
        if previous.get('question_text'):
            context['requester_deadline']['question_text'] = previous['question_text']
        conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?', (d._json(context), row['id']))
        d._event(conn, row['task_id'], row['run_id'], 'nfos_requester_reminder',
                 {'decision_id': row['id'], 'due_at': due, 'text': text, 'recipient': recipient[:80], 'origin': where})


def _topic_warned(conn, row, delivered_at, message_id):
    d = _delivery()
    with d._kb().write_txn(conn, allow_nested=True):
        context = _still_waiting(conn, row['id'])
        if context is None:
            return
        deadline = context['requester_deadline']
        deadline.update(warned_at=delivered_at, due_at=deadline['reminder']['due_at'], reminder_message_id=message_id)
        conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?', (d._json(context), row['id']))
        d._event(conn, row['task_id'], row['run_id'], 'nfos_requester_reminded',
                 {'decision_id': row['id'], 'due_at': deadline['due_at'], 'message_id': message_id})


def _still_waiting(conn, decision_id):
    current = _delivery().get_decision(conn, decision_id)
    if not current or current['status'] != 'human':
        return None
    context = json.loads(current['context'] or '{}') or {}
    return None if context.get('human_reply') else context


def _unarmed(conn, row, asked, limit, route):
    d = _delivery()
    with d._kb().write_txn(conn, allow_nested=True):
        context = _still_waiting(conn, row['id'])
        if context is None:
            return
        context['requester_deadline'] = {'asked_at': asked, 'hours': limit, 'channel': route, 'unarmed': 'no_warning_channel'}
        conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?', (d._json(context), row['id']))
        d._event(conn, row['task_id'], row['run_id'], 'nfos_requester_deadline_unarmed',
                 {'decision_id': row['id'], 'channel': route, 'reason': 'no_warning_channel'})


def _warn(conn, row, asked, limit, due, text, now):
    d = _delivery()
    with d._kb().write_txn(conn, allow_nested=True):
        context = _still_waiting(conn, row['id'])
        if context is None:
            return
        public = context['public_message']
        question = (context.get('requester_deadline') or {}).get('question_text') or public['text']
        context.setdefault('public_message_history', []).append(dict(public))
        context['public_message'] = dict(public, text=text, created_at=now)  # hora nova: o Balcão mostra como mensagem nova
        context['requester_deadline'] = {'asked_at': asked, 'hours': limit, 'channel': 'portal', 'question_text': question,
                                         'warn_at': now, 'warned_at': now, 'due_at': due}
        conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?', (d._json(context), row['id']))
        d._event(conn, row['task_id'], row['run_id'], 'nfos_requester_reminded', {'decision_id': row['id'], 'due_at': due})


def _expire(conn, row, task, workflow, asked, limit, due, now):
    d = _delivery()
    import uuid
    refused = d._human_question_part(row.get('answer'))
    question = ('O solicitante não respondeu à pergunta deste card até o prazo (REQUESTER_DEADLINE_20261010): ' + f'{limit:g}' + ' h, com '
                'aviso antes de vencer. O card voltou para você. Decida com continue ou changes, sem perguntar de novo: siga com o que já '
                'está conferido, entregue o que dá para verificar sem a resposta e feche como entrega parcial (partial_delivery=true), '
                'dizendo em public_delivery.summary o que ficou dependendo do solicitante. Se ele responder depois, a resposta chega a '
                'você neste card; se o card já estiver fechado, o chamado reabre. Pergunta sem resposta: ' + refused[:700])
    new_id = 'dec_' + uuid.uuid4().hex[:20]
    with d._kb().write_txn(conn, allow_nested=True):
        context = _still_waiting(conn, row['id'])
        if context is None:
            return
        carried = {key: value for key, value in context.items() if key not in _NOT_CARRIED}
        carried.update(supersedes=row['id'], requester_silence={'asked_at': asked, 'due_at': due, 'hours': limit,
                                                               'warned_at': (context.get('requester_deadline') or {}).get('warned_at')})
        context.update(superseded_by=new_id, superseded_reason='requester_silence')
        conn.execute("UPDATE nfos_decisions SET status='superseded',context=? WHERE id=?", (d._json(context), row['id']))
        conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (new_id, task.id, row['run_id'], 'impediment', question, d._json(carried), workflow['spec_revision'], now))
        d._event(conn, task.id, row['run_id'], 'nfos_requester_silence',
                 {'decision_id': row['id'], 'new_decision_id': new_id, 'asked_at': asked, 'due_at': due, 'question': refused[:400]})
        d._event(conn, task.id, row['run_id'], 'nfos_principal_requested', {'decision_id': new_id, 'kind': 'impediment', 'question': question})
    if task.status == 'blocked':
        d._kb().unblock_task(conn, task.id)


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
        import uuid
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
