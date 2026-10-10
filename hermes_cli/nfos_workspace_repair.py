"""Maintainer recovery of an idle card's repository binding, with provenance."""
import json
import hashlib
import os
import secrets
import time
from dataclasses import replace
from pathlib import Path

from hermes_cli import kanban_db as kb, nfos_delivery as delivery
from hermes_cli.nfos_workspaces import _git


def maintenance_pause_pending(conn, task_id):
    return conn.execute("SELECT 1 FROM task_runs WHERE task_id=? "
                        "AND json_type(metadata,'$.maintenance_pause')='object' "
                        "AND json_extract(metadata,'$.maintenance_pause.repaired_at') IS NULL LIMIT 1",
                        (task_id,)).fetchone() is not None


# LAB_TRANSPORT_WAIT_20261009: a pausa cuja causa é o laboratório fora do ar volta quando ele responder, sem o Principal.
# Antes ela virava "Execute o reparo desta pausa", que o Principal não alcança (PC, WSL ou túnel do laboratório): cada
# resposta voltava a pendente e o lembrete o acordava a cada 15 min por card (09/10/2026, seis cards do Concursa).
RESUME_WHEN = ('lab_available',)


def _lab_wait_decision(conn, row, pause, identity, accumulated):
    """Decisão de espera do laboratório para a pausa: segura o card sem lembrete nem turno do Principal até o laboratório
    responder (sweep_lab_waits encerra a pausa e responde) e só vai ao Principal depois de 12 h. Leva a mesma obrigação
    maintenance_recovery da pausa, para resposta explicativa não deixar o trabalho retido órfão. Obrigação de reparo
    pendente da mesma pausa sai como superseded, com o vínculo para esta espera."""
    wid = 'dec_' + hashlib.sha256(delivery._json(['maintenance-lab-wait', identity]).encode()).hexdigest()[:24]
    if delivery.get_decision(conn, wid):
        return
    now = int(time.time())
    reason = pause.get('reason') or 'Laboratório fora do ar.'
    superseded = []
    for old in conn.execute("SELECT id,context FROM nfos_decisions WHERE task_id=? AND status='pending' "
                            "AND json_extract(context,'$.maintenance_recovery.pause_run_id')=?",
                            (row['task_id'], row['id'])).fetchall():
        old_context = json.loads(old['context'] or '{}')
        old_context.update(superseded_by=wid, superseded_reason='lab_transport_wait')
        conn.execute("UPDATE nfos_decisions SET status='superseded',context=? WHERE id=? AND status='pending'",
                     (delivery._json(old_context), old['id']))
        superseded.append(old['id'])
    context = dict(
        lab_wait=dict(kind='transport', hold=True, since=now, checked_at=None, next_check_at=now,
                      status='indisponivel', pause_run_id=row['id']),
        maintenance_recovery=dict(pause_run_id=row['id'], reason=reason, resume_route='lab_available',
                                  budget_mode='accumulated_escalation' if accumulated else 'per_run'))
    question = (delivery.lab_transport_wait_question() + ' Quando o laboratório responder, o runtime encerra a pausa '
                + str(row['id']) + ' e devolve o card à fila no mesmo checkpoint. Causa registrada na pausa: ' + reason)
    conn.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) "
                 "VALUES(?,?,?,'impediment',?,?,?,?)",
                 (wid, row['task_id'], row['id'], question, delivery._json(context), row['spec_revision'], now))
    conn.execute('UPDATE nfos_workflows SET next_action=?,updated_at=? WHERE task_id=?',
                 ('Laboratório fora do ar: o card volta sozinho quando ele responder. Causa: ' + reason, now, row['task_id']))
    kb._append_event(conn, row['task_id'], 'nfos_lab_wait', dict(decision_id=wid, transport=True, status='indisponivel',
                     source='maintenance_pause', pause_run_id=row['id'], superseded=superseded), run_id=row['id'])


def reconcile_maintenance_recovery(conn):
    """Keep each native pause owned by the Principal until its repair receipt exists.

    Uses existing decisions/wakes, never a second task or a speculative worker retry.
    Reconciles legacy pauses too; explanatory answers cannot orphan the retained work.
    A pause that resumes when the lab answers (resume_when) waits natively instead (LAB_TRANSPORT_WAIT_20261009).
    """
    from hermes_cli.nfos_principal_review import worker_escalation
    with kb.write_txn(conn, allow_nested=True):
        rows = conn.execute("SELECT r.id,r.task_id,r.metadata,w.spec_revision FROM task_runs r "
                            "JOIN tasks t ON t.id=r.task_id JOIN nfos_workflows w ON w.task_id=t.id "
                            "WHERE t.status NOT IN ('done','archived') "
                            "AND (t.status<>'blocked' OR t.block_kind='awaiting_principal') AND "
                            "json_type(r.metadata,'$.maintenance_pause')='object'").fetchall()
        for row in rows:
            pause = json.loads(row['metadata'])['maintenance_pause']
            identity = [row['task_id'], row['id'], pause.get('identity'), pause.get('at')]
            did = 'dec_' + hashlib.sha256(delivery._json(['maintenance-recovery', identity]).encode()).hexdigest()[:24]
            decision = delivery.get_decision(conn, did)
            if pause.get('repaired_at') is not None:
                # Reconsideration supersedes a decision but carries the same pause obligation.
                # Close only pending obligations for this repaired pause, not their history or other holds.
                obligations = conn.execute("SELECT id FROM nfos_decisions WHERE task_id=? AND status='pending' "
                                           "AND json_extract(context,'$.maintenance_recovery.pause_run_id')=?",
                                           (row['task_id'], row['id'])).fetchall()
                for obligation in obligations:
                    answer = 'Native maintenance repair confirmed: ' + str(pause.get('repair_kind'))
                    conn.execute("UPDATE nfos_decisions SET status='resolved',action='continue',answer=?,"
                                 "author='NFOS automation',resolved_at=? WHERE id=?", (answer, int(time.time()), obligation['id']))
                    kb._append_event(conn, row['task_id'], 'nfos_maintenance_recovery_completed',
                                     dict(decision_id=obligation['id'], pause_run_id=row['id'], repaired_at=pause['repaired_at']), run_id=row['id'])
                continue
            if pause.get('resume_when') == 'lab_available':
                _lab_wait_decision(conn, row, pause, identity, bool(worker_escalation(conn, row['task_id']).get('first_run_id')))
                continue
            if decision:
                # PAUSE_OBLIGATION_REISSUED_20261010: a obrigação existe mas ninguém a deve mais (saiu por pergunta humana,
                # por resposta humana ou por sucessora sem a herança). Pausa aberta sem decisão pendente nem pergunta humana
                # que a carregue ganha a obrigação de novo, em vez de ficar em awaiting_principal sem lembrete.
                held = conn.execute("SELECT COUNT(*), SUM(status IN ('pending','human')) FROM nfos_decisions WHERE task_id=? "
                                    "AND json_extract(context,'$.maintenance_recovery.pause_run_id')=?", (row['task_id'], row['id'])).fetchone()
                if held[1]:
                    continue
                did = 'dec_' + hashlib.sha256(delivery._json(['maintenance-recovery', identity, 'reissued', held[0]]).encode()).hexdigest()[:24]
                if delivery.get_decision(conn, did):
                    continue
            reason = pause.get('reason') or 'Pausa de manutenção sem motivo registrado; recuperar a causa no histórico antes de retomar.'
            accumulated = bool(worker_escalation(conn, row['task_id']).get('first_run_id'))
            route = 'repair-execution' if pause.get('kind') == 'runtime_budget_exhausted' else 'resume-after-repair'
            context = dict(maintenance_recovery=dict(pause_run_id=row['id'], reason=reason,
                resume_route=route, budget_mode='accumulated_escalation' if accumulated else 'per_run'))
            question = ('Execute o reparo desta pausa no contexto administrativo autorizado do Principal, '
                        'fora do worker retido. Diagnostique e corrija a causa; explicar o impedimento não conclui a manutenção. '
                        'Confirme a saída do executor anterior e registre o recibo real. Use repair-card/workspace quando '
                        'a causa for o vínculo; nos demais casos use ' + route + ' dry-run/apply para a pausa ' + str(row['id']) + '. '
                        'Não invente escalonamento nem reinicie saldo: este card usa ' + context['maintenance_recovery']['budget_mode'] + '. '
                        'Não relance o worker para esperar o mesmo reparo. Se o reparo depende de algo fora das suas ferramentas '
                        'autorizadas, não repita a explicação: responda esta decisão uma vez com "dependency":{"owner":"quem '
                        'entrega","need":"o que falta"} no JSON do decide; a pausa espera sem lembrete e volta para você '
                        'reconferir em 1 h (depois 2, 4 e 6 h na mesma pausa); "recheck_hours" de 1 a 24 só quando você sabe '
                        'a hora em que o fato muda. Quem entrega não é você nem a engenharia deste projeto: o que só vocês '
                        'fariam, ou uma autorização que este projeto não pergunta ao dono, não é reparo nem dependência, e o card '
                        'entrega o que cabe e fecha como entrega parcial, com o resto obrigatório no card de continuação. '
                        'Preserve sessão, consumo, evidências e aceites. Causa: ' + reason)
            conn.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) "
                         "VALUES(?,?,?,'impediment',?,?,?,?)",
                         (did,row['task_id'],row['id'],question,delivery._json(context),row['spec_revision'],int(time.time())))
            conn.execute('UPDATE nfos_workflows SET next_action=?,updated_at=? WHERE task_id=?',
                         ('Principal: reparar a pausa e confirmar a retomada. Causa: '+reason, int(time.time()), row['task_id']))
            # The public Project Ops event contract is consumed by both native show and Vigilia.
            kb._append_event(conn,row['task_id'],'blocked',dict(reason=reason,kind='awaiting_principal',
                source='maintenance_pause',pause_run_id=row['id'],decision_id=did),run_id=row['id'])
            kb._append_event(conn,row['task_id'],'nfos_principal_requested',
                dict(decision_id=did,kind='impediment',question=question,maintenance_recovery=True),run_id=row['id'])


def _finish_maintenance_pause(conn, task_id, *, actor, repair_kind):
    """Release the native claim hold only in the successful repair transaction."""
    rows = conn.execute("SELECT id,metadata FROM task_runs WHERE task_id=? "
                        "AND json_type(metadata,'$.maintenance_pause')='object' "
                        "AND json_extract(metadata,'$.maintenance_pause.repaired_at') IS NULL", (task_id,)).fetchall()
    for row in rows:
        metadata = json.loads(row['metadata'])
        metadata['maintenance_pause'].update(repaired_at=time.time(), repaired_by=actor, repair_kind=repair_kind)
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (delivery._json(metadata), row['id']))


def pause_for_repair(conn, task_id, *, expected_run_id, expected_claim, expected_pid,
                     expected_started_at, actor, reason, apply=False, resume_when=None):
    """Principal maintenance pause, using Project Ops and its native exit cleanup.

    resume_when='lab_available': the cause is the lab being down; the pause waits for it natively and ends by
    itself when `concursa-lab status` answers, with no repair for the Principal (LAB_TRANSPORT_WAIT_20261009)."""
    from agent.delegation_context import is_dispatcher_owned_worker_context
    if (os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')
            or not is_dispatcher_owned_worker_context()):
        raise delivery.WorkflowError('Only the Principal maintainer can pause for repair')
    if not actor or not reason or not expected_claim or not expected_pid or expected_started_at is None:
        raise delivery.WorkflowError('Specify actor, reason and the observed run/claim/process identity')
    if resume_when is not None and resume_when not in RESUME_WHEN:
        raise delivery.WorkflowError('resume_when accepts only: ' + ', '.join(RESUME_WHEN))
    identity = dict(run_id=expected_run_id, claim=expected_claim, pid=expected_pid, started_at=expected_started_at)

    def observed():
        task = kb.get_task(conn, task_id)
        run = conn.execute('SELECT * FROM task_runs WHERE task_id=? AND id=?', (task_id, expected_run_id)).fetchone()
        if not task or not run or not delivery.get_workflow(conn, task_id):
            raise delivery.WorkflowError('Card or worker identity changed before maintenance')
        metadata = json.loads(run['metadata'] or '{}')
        saved = metadata.get('maintenance_pause', {})
        if (task.status == 'blocked' and task.current_run_id is None and run['ended_at'] is not None
                and saved.get('identity') == identity):
            return saved
        if (task.status != 'running' or run['ended_at'] is not None
                or (task.current_run_id, task.claim_lock, task.worker_pid, task.worker_started_at)
                != (expected_run_id, expected_claim, expected_pid, expected_started_at)
                or run['worker_pid'] != expected_pid or run['claim_lock'] != expected_claim):
            raise delivery.WorkflowError('Card or worker identity changed before maintenance')
        if kb._pid_alive(expected_pid):
            started_at = kb._process_start_time(expected_pid)
            if started_at is None or abs(started_at - expected_started_at) >= .01:
                raise delivery.WorkflowError('Worker process identity changed before maintenance')
        return None

    saved = observed()
    if saved:
        return dict(saved, paused=True, already_paused=True, run_id=expected_run_id, apply=apply)
    if not apply:
        return dict(identity=identity, actor=actor, reason=reason, paused=False, apply=False, resume_when=resume_when)
    with kb.write_txn(conn):
        saved = observed()
        if saved:
            return dict(saved, paused=True, already_paused=True, run_id=expected_run_id, apply=True)
        now = time.time()
        pause = dict(identity=identity, actor=actor, reason=reason, at=now)
        if resume_when:
            pause['resume_when'] = resume_when
        cleanup = dict(worker_pid=expected_pid, worker_started_at=expected_started_at,
                       status='waiting', grace_seconds=15, not_before=now+15,
                       descendants_json='[]', requested_at=now)
        from hermes_cli.nfos_tool import _descendants
        cleanup['descendants_json'] = json.dumps(_descendants(cleanup, include_group=True))
        # _end_run merges this receipt before clearing the task/run PID pointers.
        kb._end_run(conn, task_id, outcome='reclaimed', status='reclaimed', summary=reason,
                    metadata={'maintenance_pause': pause, 'nfos_cleanup': cleanup})
        conn.execute("UPDATE tasks SET status='blocked',block_kind='awaiting_principal',"
                     "claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,worker_started_at=NULL WHERE id=?", (task_id,))
        kb._append_event(conn, task_id, 'nfos_maintenance_paused', pause, run_id=expected_run_id)
        kb._append_event(conn, task_id, 'blocked', dict(reason=reason, kind='awaiting_principal',
                         source='maintenance_pause', pause_run_id=expected_run_id), run_id=expected_run_id)
    # The existing dispatcher cleans this closed run. Claims and repair_card keep
    # rejecting the old process/descendants until its exit receipt is confirmed.
    return dict(pause, paused=True, already_paused=False, run_id=expected_run_id, apply=True)


def resume_after_repair(conn, task_id, *, pause_run_id, actor, reason, evidence, expected_pause_sha256=None,
                        apply=False):
    """Principal maintenance resume of ONE pause whose cause was repaired outside the card (intake,
    support approval, links), with the same maintainer gate as the pause.

    repair-card, repair-workspace and repair-execution keep closing the pauses they repair; a budget pause
    (kind='runtime_budget_exhausted') stays with repair-execution. This route
    closes only the selected pause (repair_kind='external', with reason and evidence): other pauses,
    decisions and blocks stay, and the card returns to the queue through the runtime sweep once nothing
    else holds it. The preview returns the pause sha256 that apply must present; repeating the same
    resume is idempotent.
    """
    from agent.delegation_context import is_dispatcher_owned_worker_context
    if (os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')
            or not is_dispatcher_owned_worker_context()):
        raise delivery.WorkflowError('Only the Principal maintainer can resume a maintenance pause')
    if (type(pause_run_id) is not int or any(not isinstance(v, str) or not v.strip() for v in (actor, reason))
            or not isinstance(evidence, list) or not evidence
            or any(not isinstance(item, str) or not item.strip() for item in evidence)):
        raise delivery.WorkflowError('Specify the pause run, actor, reason and the evidence that its cause was repaired')
    if apply and not isinstance(expected_pause_sha256, str):
        raise delivery.WorkflowError('Apply requires the pause sha256 returned by the preview')

    def observed():
        row = conn.execute('SELECT metadata FROM task_runs WHERE task_id=? AND id=?', (task_id, pause_run_id)).fetchone()
        metadata = json.loads((row['metadata'] if row else None) or '{}')
        pause = metadata.get('maintenance_pause')
        if not isinstance(pause, dict):
            raise delivery.WorkflowError('The selected run holds no maintenance pause')
        if pause.get('kind') == 'runtime_budget_exhausted':
            raise delivery.WorkflowError('A budget pause resumes through repair-execution, which checks the balance '
                                         'and the resume context')
        if pause.get('repaired_at') is not None:
            if (pause.get('repair_kind'), pause.get('repaired_by'), pause.get('repair_reason')) == ('external', actor, reason):
                return metadata, None  # the same resume again: the card may already be running
            raise delivery.WorkflowError('The selected pause was already closed by another repair')
        _idle(conn, task_id)  # idle card, no live executor, previous worker termination confirmed
        digest = hashlib.sha256(delivery._json(pause).encode()).hexdigest()
        if apply and digest != expected_pause_sha256:
            raise delivery.WorkflowError('The selected pause changed since it was read')
        return metadata, digest

    base = dict(task_id=task_id, pause_run_id=pause_run_id, actor=actor, reason=reason, evidence=evidence, apply=apply)
    metadata, digest = observed()
    if digest is None:
        return dict(base, resumed=True, already_resumed=True)
    if not apply:
        return dict(base, pause_sha256=digest, pause_reason=metadata['maintenance_pause'].get('reason'), resumed=False)
    with kb.write_txn(conn):
        metadata, digest = observed()
        if digest is None:
            return dict(base, resumed=True, already_resumed=True)
        metadata['maintenance_pause'].update(repaired_at=time.time(), repaired_by=actor, repair_kind='external',
                                             repair_reason=reason, repair_evidence=evidence)
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (delivery._json(metadata), pause_run_id))
        kb._append_event(conn, task_id, 'nfos_maintenance_resumed',
                         dict(pause_run_id=pause_run_id, pause_sha256=digest, actor=actor, reason=reason,
                              evidence=evidence), run_id=pause_run_id)
    return dict(base, pause_sha256=digest, resumed=True, already_resumed=False)


def lab_pause_ready(conn, task_id, pause_run_id):
    """A pausa que espera o laboratório pode terminar agora? Só a própria pausa aberta, com o card ocioso e a saída do
    executor anterior confirmada (o mesmo _idle do resume-after-repair). Fora disso a espera continua."""
    row = conn.execute('SELECT metadata FROM task_runs WHERE task_id=? AND id=?', (task_id, pause_run_id)).fetchone()
    pause = json.loads((row['metadata'] if row else None) or '{}').get('maintenance_pause')
    if not isinstance(pause, dict) or pause.get('resume_when') != 'lab_available' or pause.get('repaired_at') is not None:
        return None
    try:
        _idle(conn, task_id)
    except delivery.WorkflowError:
        return None
    return pause


def finish_lab_pause(conn, task_id, pause_run_id, *, evidence):
    """Encerra, na transação do chamador, a pausa que esperava o laboratório (LAB_TRANSPORT_WAIT_20261009). Devolve False
    quando ela não pode terminar agora; o card volta à fila pela varredura quando nada mais o segura."""
    if not lab_pause_ready(conn, task_id, pause_run_id):
        return False
    row = conn.execute('SELECT metadata FROM task_runs WHERE task_id=? AND id=?', (task_id, pause_run_id)).fetchone()
    metadata = json.loads(row['metadata'])
    reason = 'O laboratório voltou a responder (concursa-lab status).'
    metadata['maintenance_pause'].update(repaired_at=time.time(), repaired_by='NFOS automation', repair_kind='lab_available',
                                         repair_reason=reason, repair_evidence=evidence)
    conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (delivery._json(metadata), pause_run_id))
    kb._append_event(conn, task_id, 'nfos_maintenance_resumed',
                     dict(pause_run_id=pause_run_id, actor='NFOS automation', reason=reason, evidence=evidence,
                          repair_kind='lab_available'), run_id=pause_run_id)
    return True


def repair_card(conn, task_id, *, board, delivery_type, expected_delivery_type,
                expected_spec_revision, expected_instruction_revision,
                reason, actor, use_canonical_repo=False, apply=False):
    """Correct routing/classification without replacing historical specifications."""
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise delivery.WorkflowError('Card repair belongs to the maintainer, outside a worker')
    if delivery_type not in {'code', 'report', 'operation'} or not reason or not actor:
        raise delivery.WorkflowError('Specify delivery type, reason and operator')
    from hermes_cli.nfos_runtime import project_config
    from hermes_cli.profiles import profile_exists
    project = project_config(board)
    profile = (project or {}).get('profile')
    if not profile or not profile_exists(profile):
        raise delivery.WorkflowError('The project executor must already exist')
    task = _idle(conn, task_id)
    wf = delivery.get_workflow(conn, task_id)
    if not wf or (task.delivery_type, wf['spec_revision'], task.instruction_revision) != (
            expected_delivery_type, expected_spec_revision, expected_instruction_revision):
        raise delivery.WorkflowError('Card classification or instruction changed since readback')
    path = task.workspace_path
    kind = task.workspace_kind
    if use_canonical_repo:
        repo = Path(project.get('repo_path') or '').expanduser().resolve()
        if delivery_type != 'code' or not project.get('repo_path') or kb._git_toplevel(repo) != repo:
            raise delivery.WorkflowError('Canonical execution requires the configured code repository')
        path, kind = str(repo), 'dir'
    elif delivery_type != 'code':
        if not path or not Path(path).expanduser().exists():
            path = None
        kind = 'dir' if path else 'scratch'
    proposed = dict(delivery_type=delivery_type, requires_repo=delivery_type=='code',
                    assignee=profile, workspace_kind=kind, workspace_path=path,
                    new_spec_required=delivery_type != task.delivery_type,
                    status=task.status, actor=actor, reason=reason)
    if not apply:
        return dict(proposed, apply=False)
    with kb.write_txn(conn):
        current = _idle(conn, task_id)
        current_wf = delivery.get_workflow(conn, task_id)
        if (current.delivery_type, current_wf['spec_revision'], current.instruction_revision,
                current.assignee, current.workspace_path) != (
                expected_delivery_type, expected_spec_revision, expected_instruction_revision,
                task.assignee, task.workspace_path):
            raise delivery.WorkflowError('Card changed during routing repair')
        if not path and kind == 'scratch':
            # Create only the native managed report directory, never a fake repo.
            path = str(kb.resolve_workspace(replace(task,workspace_kind='scratch',workspace_path=None), board=board, conn=conn))
            proposed['workspace_path'] = path
        state = json.loads(current_wf['state_json'])
        state.setdefault('card_repairs', []).append(dict(previous=dict(
            delivery_type=task.delivery_type, requires_repo=task.requires_repo,
            assignee=task.assignee, workspace_kind=task.workspace_kind,
            workspace_path=task.workspace_path), proposed=proposed, at=int(time.time())))
        if proposed['new_spec_required']:
            state['classification_revision_floor'] = current_wf['spec_revision'] + 1
            conn.execute("UPDATE nfos_workflows SET stage='analysis',next_action=? WHERE task_id=?",
                         ('Reuse the preserved TL proposal and save the matching specification revision',task_id))
        if use_canonical_repo:
            state['workspace_policy'] = 'canonical'
        conn.execute('UPDATE tasks SET delivery_type=?,requires_repo=?,assignee=?,workspace_kind=?,workspace_path=? WHERE id=?',
                     (delivery_type,int(delivery_type=='code'),profile,kind,path,task_id))
        conn.execute('UPDATE task_git_delivery SET required=0 WHERE task_id=?',(task_id,))
        conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?',(delivery._json(state),task_id))
        kb._append_event(conn,task_id,'nfos_card_repaired',proposed)
        _finish_maintenance_pause(conn, task_id, actor=actor, repair_kind='card')
    return dict(proposed, apply=True)


def _idle(conn, task_id):
    task = kb.get_task(conn, task_id)
    if not task or task.status not in {'blocked', 'ready', 'todo', 'review'} or task.current_run_id:
        raise delivery.WorkflowError('Workspace repair requires an idle existing card; the Principal can use '
                                     'pause-for-repair, await native worker cleanup, then retry the repair')
    if task.worker_pid and kb._pid_alive(task.worker_pid):
        raise delivery.WorkflowError('Workspace repair cannot interrupt a live executor')
    from hermes_cli.nfos_runtime import previous_runs_termination_pending
    if previous_runs_termination_pending(conn, task_id):
        raise delivery.WorkflowError('Previous worker termination is not confirmed')
    return task


def execution_budget(conn, task):
    """Compute the next native worker's balance without launching a process."""
    from hermes_cli import nfos_principal_review as review
    from hermes_cli.config import load_config_readonly, resolve_turn_limit
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    first = review.worker_escalation(conn, task.id).get('first_run_id')
    if not first:
        return None
    last = conn.execute('SELECT MAX(id) FROM task_runs WHERE task_id=?', (task.id,)).fetchone()[0]
    usage = review.escalation_usage(conn, task.id, last + 1)
    config_path = get_profile_dir(task.assignee or 'default') / 'config.yaml'
    raw = config_path.read_bytes() if config_path.is_file() else b''
    token = set_hermes_home_override(config_path.parent)
    try:
        cfg = load_config_readonly()
    finally:
        reset_hermes_home_override(token)
    limit = (cfg.get('agent') or {}).get('max_turns', cfg.get('max_turns'))
    base = resolve_turn_limit(limit if limit is not None else os.environ.get('HERMES_MAX_ITERATIONS'))
    grants = [g for g in review.iteration_grants(conn, task.id) if g['first_run_id'] == first]
    return dict(config_sha256=hashlib.sha256(raw).hexdigest(), base_iterations=base, prior_usage=usage,
                remaining_iterations=base + sum(g['iterations'] for g in grants) - usage['iterations'],
                remaining_runtime_seconds=task.max_runtime_seconds - usage['seconds'] if task.max_runtime_seconds is not None else None,
                remaining_goal_turns=task.goal_max_turns - usage['turns'] if task.goal_max_turns else None)


def request_execution_budget_review(conn, task, balance):
    """Persist one internal Principal wake per material exhausted state, including pre-spawn."""
    wf = delivery.get_workflow(conn, task.id)
    last = conn.execute('SELECT MAX(id) FROM task_runs WHERE task_id=?', (task.id,)).fetchone()[0]
    identity = dict(run_id=last, instruction_revision=task.instruction_revision, spec_revision=wf['spec_revision'])
    context = dict(execution_budget_identity=identity,
                   balance={k: v for k, v in balance.items() if k != 'config_sha256'})
    decision_id = 'dec_' + hashlib.sha256(delivery._json([task.id, context]).encode()).hexdigest()[:24]
    if delivery.get_decision(conn, decision_id):
        return decision_id
    question = ('Saldo de execução esgotado. Verifique a autorização existente para concessão finita com '
                'grant-budget; após saldo positivo e saída confirmada, use repair-execution dry-run/apply. '
                'Continue não concede orçamento e esta revisão não aceita a entrega.')
    conn.execute('INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) '
                 "VALUES(?,?,?,'impediment',?,?,?,?)",
                 (decision_id, task.id, last, question, delivery._json(context), wf['spec_revision'], int(time.time())))
    kb._append_event(conn, task.id, 'nfos_principal_requested',
                     dict(decision_id=decision_id, kind='impediment', question=question, retained_card=True), run_id=last)
    return decision_id


def repair_execution(conn, task_id, *, board, expected_run_id, expected_instruction_revision,
                     expected_spec_revision, expected_resume_session, actor, reason,
                     pause_run_id=None, apply=False):
    """Release an execution hold only after observing usable budget and native resume context."""
    from agent.delegation_context import is_dispatcher_owned_worker_context
    from hermes_cli import nfos_principal_review as review
    from hermes_cli.profiles import profile_matches_home
    from hermes_constants import get_hermes_home
    if (os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')
            or not is_dispatcher_owned_worker_context()):
        raise delivery.WorkflowError('Execution repair belongs to the Principal maintainer, outside a worker')
    if (any(not isinstance(v, str) or not v.strip() for v in (actor, reason))
            or type(expected_run_id) is not int or expected_run_id <= 0
            or type(expected_instruction_revision) is not int or type(expected_spec_revision) is not int
            or (pause_run_id is not None and type(pause_run_id) is not int)):
        raise delivery.WorkflowError('Specify actor, reason and observed run/instruction/spec identity')
    home = Path(get_hermes_home())

    def observed():
        task = _idle(conn, task_id)
        wf = delivery.get_workflow(conn, task_id)
        last = conn.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1', (task_id,)).fetchone()
        if (not wf or not last or last['id'] != expected_run_id or last['ended_at'] is None
                or task.instruction_revision != expected_instruction_revision or wf['spec_revision'] != expected_spec_revision):
            raise delivery.WorkflowError('Run, instruction or specification changed before execution repair')
        if not profile_matches_home(task.assignee or 'default', home):
            raise delivery.WorkflowError('Run repair under the executor profile')
        db = Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve()
        if db != kb.kanban_db_path(board=board).resolve():
            raise delivery.WorkflowError('Board does not match the current database')
        budget = execution_budget(conn, task)
        if budget is None:
            raise delivery.WorkflowError('Execution budget recovery requires an existing escalation lineage')
        if any(budget[k] is not None and budget[k] <= 0 for k in
               ('remaining_iterations', 'remaining_runtime_seconds', 'remaining_goal_turns')):
            raise delivery.WorkflowError('Execution budget remains exhausted')
        resume, _ = kb._worker_resume_context(replace(task, current_run_id=expected_run_id + 1), str(home), board=board)
        if resume != expected_resume_session:
            raise delivery.WorkflowError('Native resume context changed before execution repair')
        pauses = conn.execute("SELECT id,metadata FROM task_runs WHERE task_id=? "
                              "AND json_type(metadata,'$.maintenance_pause')='object' "
                              "AND json_extract(metadata,'$.maintenance_pause.repaired_at') IS NULL", (task_id,)).fetchall()
        if len(pauses) > 1:
            raise delivery.WorkflowError('Multiple maintenance pauses require separate review')
        pause = pauses[0] if pauses else None
        if pause_run_id is not None and (pause is None or pause['id'] != pause_run_id):
            raise delivery.WorkflowError('Selected maintenance pause changed')
        generic = pause and json.loads(pause['metadata'])['maintenance_pause'].get('kind') != 'runtime_budget_exhausted'
        if generic and (pause_run_id != pause['id'] or last['outcome'] != 'reclaimed'
                        or json.loads(last['metadata'] or '{}').get('worker_session_id') or resume is not None):
            raise delivery.WorkflowError('Generic execution pause requires an explicitly selected unbound reclaimed attempt')
        return dict(task_id=task_id, run_id=expected_run_id, instruction_revision=task.instruction_revision,
                    spec_revision=wf['spec_revision'], pause_run_id=pause['id'] if pause else None,
                    pause_metadata_sha256=hashlib.sha256(pause['metadata'].encode()).hexdigest() if pause else None,
                    **budget, resume_session=resume,
                    resume_kind='native fresh context after unbound reclaimed attempt' if generic else 'native resume selection',
                    worker_args=review.worker_model_args(task, conn), actor=actor, reason=reason)

    proposed = observed()
    if not apply:
        return dict(proposed, apply=False)
    with kb.write_txn(conn):
        current = observed()
        if current != proposed:
            raise delivery.WorkflowError('Execution conditions changed during repair')
        if current['pause_run_id'] is not None:
            row = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (current['pause_run_id'],)).fetchone()
            metadata = json.loads(row['metadata'])
            metadata['maintenance_pause'].update(repaired_at=time.time(), repaired_by=actor, repair_kind='execution')
            conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (delivery._json(metadata), current['pause_run_id']))
        kb.unblock_task(conn, task_id)
        current['status'] = kb.get_task(conn, task_id).status
        identity = {k: current[k] for k in ('run_id', 'instruction_revision', 'spec_revision')}
        for decision in conn.execute("SELECT id,context FROM nfos_decisions WHERE task_id=? AND status='pending'", (task_id,)).fetchall():
            if json.loads(decision['context']).get('execution_budget_identity') == identity:
                answer = 'Execution readiness verified by authorized repair; this is not delivery acceptance'
                conn.execute("UPDATE nfos_decisions SET status='resolved',action='continue',author=?,answer=?,resolved_at=? WHERE id=?",
                             (actor, answer, int(time.time()), decision['id']))
                kb._append_event(conn, task_id, 'nfos_principal_resolved',
                                 dict(decision_id=decision['id'], action='continue', answer=answer), run_id=expected_run_id)
        kb._append_event(conn, task_id, 'nfos_execution_repaired', current, run_id=expected_run_id)
    return dict(current, apply=True)


def repair_workspace(conn, task_id, *, board, repo_path, base_sha, expected_workspace,
                     expected_source_sha, reason, actor, apply=False):
    """Plan/apply only an explicit correction to the configured project repository.

    The old checkout stays intact. Same-repository tracked edits are restored
    exactly; unrelated repository files remain at their original source. Neither
    existing delivery evidence nor approvals nor card status are rewritten.
    """
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'):
        raise delivery.WorkflowError('Workspace repair belongs to the maintainer, outside a worker')
    if not reason or not actor:
        raise delivery.WorkflowError('Record the repair reason and operator')
    from hermes_cli.nfos_runtime import project_config
    project = project_config(board)
    repo = Path(repo_path).expanduser().resolve(strict=True)
    if not project or kb._path_identity(project.get('repo_path')) != kb._path_identity(repo):
        raise delivery.WorkflowError('Destination must be the configured project repository')
    if kb._git_toplevel(repo) != repo or not kb._git_common_dir(repo):
        raise delivery.WorkflowError('Destination is not a Git repository root')
    if len(base_sha) not in (40, 64) or any(c not in '0123456789abcdef' for c in base_sha):
        raise delivery.WorkflowError('Pin the exact existing destination commit')
    if _git(repo, 'rev-parse', '--verify', base_sha + '^{commit}').strip() != base_sha:
        raise delivery.WorkflowError('Destination commit could not be verified')
    identity = dict(board=board, repo_path=str(repo), base_sha=base_sha,
                    expected_workspace=kb._loose_path_identity(expected_workspace),
                    expected_source_sha=expected_source_sha)
    wf = delivery.get_workflow(conn, task_id)
    if not wf:
        raise delivery.WorkflowError('Workspace repair requires an enrolled NFOS card')
    prior = json.loads(wf['state_json']).get('workspace_repair')
    if prior and prior['identity'] != identity:
        raise delivery.WorkflowError('Existing workspace repair has a different identity')
    if prior and prior.get('completed_at'):
        valid, _, error = kb._validate_worktree_ownership(conn, task_id, require_checkout=True)
        if not valid:
            raise delivery.WorkflowError('Repaired workspace changed: ' + error)
        kb._release_worktree_creation_lock(Path(prior['canonical_worktree']), prior)
        return dict(prior, already_applied=True)
    task = _idle(conn, task_id)
    if kb._loose_path_identity(task.workspace_path) != identity['expected_workspace']:
        raise delivery.WorkflowError('Source workspace changed since the operator readback')
    source = Path(task.workspace_path).expanduser().resolve(strict=True)
    source_repo = kb._git_toplevel(source)
    source_sha = _git(source, 'rev-parse', 'HEAD').strip() if source_repo else None
    if source_sha != expected_source_sha:
        raise delivery.WorkflowError('Source commit changed since the operator readback')
    same_repo = source_repo is not None and kb._git_common_dir(source) == kb._git_common_dir(repo)
    if same_repo and source_sha != base_sha:
        raise delivery.WorkflowError('Preserve the existing commit when isolating the same repository')
    previous_delivery = conn.execute('SELECT * FROM task_git_delivery WHERE task_id=?', (task_id,)).fetchone()
    if previous_delivery and previous_delivery['cleanup_state'] != 'not_requested':
        raise delivery.WorkflowError('Resolve the previous cleanup obligation before workspace repair')
    if previous_delivery and previous_delivery['ownership_json']:
        old = json.loads(previous_delivery['ownership_json'])
        _, fingerprint = kb._canonical_delivery_document(old)
        if fingerprint != previous_delivery['ownership_fingerprint'] or old.get('task_id') != task_id:
            raise delivery.WorkflowError('Source ownership evidence is corrupt')
    if not apply:
        return dict(identity, task_id=task_id, status=task.status, same_repository=same_repo,
                    source_preserved=True, apply=False)
    lease, owner = kb._try_acquire_workspace_lease(source, task_id=task_id)
    if lease is None:
        raise delivery.WorkflowError('Source workspace has a live writer: ' + str(owner))
    try:
        with kb.write_txn(conn):
            current = _idle(conn, task_id)
            if current.workspace_path != task.workspace_path or current.branch_name != task.branch_name:
                raise delivery.WorkflowError('Card binding changed before repair')
            state = json.loads(delivery.get_workflow(conn, task_id)['state_json'])
            plan = state.get('workspace_repair')
            if not plan:
                nonce = secrets.token_hex(16)
                target = repo / '.nfos' / 'repairs' / nonce / '.worktrees' / task_id
                branch = f'nfos/{task_id}-repair-{nonce[:12]}'
                plan = dict(identity=identity, task_id=task_id, actor=actor, reason=reason,
                            source=str(source), base_sha=base_sha, repo_root=str(repo),
                            git_common_dir=str(kb._git_common_dir(repo)), canonical_worktree=str(target),
                            branch=branch, creation_nonce=nonce, created_at=int(time.time()),
                            previous_binding=dict(workspace_kind=task.workspace_kind,
                                                  workspace_path=task.workspace_path, branch_name=task.branch_name),
                            previous_git_delivery=dict(previous_delivery) if previous_delivery else None,
                            working_patch=_git(source, 'diff', '--binary', '--no-ext-diff', 'HEAD', '--') if same_repo else '',
                            index_patch=_git(source, 'diff', '--cached', '--binary', '--no-ext-diff', 'HEAD', '--') if same_repo else '',
                            artifacts_location=str(source), same_repository=same_repo)
                state['workspace_repair'] = plan
                conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?', (delivery._json(state), task_id))
                kb._append_event(conn, task_id, 'nfos_workspace_repair_planned',
                                 {k:plan[k] for k in ['identity', 'actor', 'reason', 'canonical_worktree', 'creation_nonce']})
        target = Path(plan['canonical_worktree'])
        if not target.exists() and not target.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            _git(repo, 'worktree', 'add', '--lock', '--reason', 'hermes-nfos-create:' + plan['creation_nonce'],
                 '-b', plan['branch'], str(target), base_sha)
        if (not kb._worktree_creation_lock_matches(target, plan)
                or kb._git_common_dir(target) != kb._git_common_dir(repo)
                or kb._git_current_branch(target) != plan['branch']
                or _git(target, 'rev-parse', 'HEAD').strip() != base_sha):
            raise delivery.WorkflowError('Repair target does not match its durable creation intent')
        # Restore only before publishing this new checkout to the task. A failed
        # final DB commit leaves the same locked target and saved patches retryable.
        _git(target, 'reset', '--hard', base_sha)
        if plan['working_patch']:
            _git(target, 'apply', '--binary', '--whitespace=nowarn', '-', patch=plan['working_patch'])
        if plan['index_patch']:
            _git(target, 'apply', '--cached', '--binary', '--whitespace=nowarn', '-', patch=plan['index_patch'])
        with kb.write_txn(conn):
            current = _idle(conn, task_id)
            if current.workspace_path != task.workspace_path or current.branch_name != task.branch_name:
                raise delivery.WorkflowError('Card binding changed during repair')
            kb._insert_git_delivery_obligation(conn, task_id, int(time.time()))
            conn.execute('UPDATE task_git_delivery SET required=0 WHERE task_id=?', (task_id,))
            conn.execute('UPDATE task_git_delivery SET ownership_json=NULL,ownership_fingerprint=NULL,'
                         'creation_intent_json=NULL,creation_intent_fingerprint=NULL WHERE task_id=?', (task_id,))
            conn.execute("UPDATE tasks SET workspace_kind='worktree' WHERE id=?", (task_id,))
            kb._seal_materialized_worktree_ownership(conn, task_id, repo_root=repo, worktree=target,
                                                    branch=plan['branch'])
            state = json.loads(delivery.get_workflow(conn, task_id)['state_json'])
            state['workspace_repair']['completed_at'] = int(time.time())
            conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?', (delivery._json(state), task_id))
            kb._append_event(conn, task_id, 'nfos_workspace_repaired',
                             dict(source=str(source), workspace=str(target), base_sha=base_sha,
                                  actor=actor, reason=reason, previous_status=task.status,
                                  previous_ownership_fingerprint=(dict(previous_delivery).get('ownership_fingerprint') if previous_delivery else None)))
            _finish_maintenance_pause(conn, task_id, actor=actor, repair_kind='workspace')
        kb._release_worktree_creation_lock(target, plan)
        return dict(state['workspace_repair'], already_applied=False)
    finally:
        kb._release_workspace_lease(lease)
