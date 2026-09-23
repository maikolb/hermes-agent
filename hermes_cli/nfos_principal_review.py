"""Principal acceptance within the existing NFOS decisions and artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from hermes_cli import nfos_delivery as d


QUALITY_POLICY_VERSION = 1
QUALITY_POLICY = """NFOS engineering and evidence policy v1 (Maikol, 23/09/2026).
Correct the cause with the smallest general domain rule. Apply SOLID, DRY,
separation of responsibilities, clear contracts, KISS/YAGNI without speculative
layers. Do not patch lists of customers, projects, people, IDs or words to pass
known examples. Legitimate domain enums are allowed. Regex normalizes documented
syntax; it and string similarity cannot establish semantic identity. Use compatible
structured data or explicit authoritative source evidence; keep ambiguity visible.
Reproduce before changing when possible, otherwise say not reproduced. Validate
positive, negative, conflicting and previously unseen inputs. Green tests do not
certify universal correctness. Code for future runs is not a backfill: independently
verify any required saved-data repair, preserving history/progress, transactions,
idempotence and concurrency appropriate to the operation.
A report explains work. Logs, SQL, tests and rendered reports are technical evidence.
A PNG of a report, HTML reconstruction, mockup or status board is NOT product proof.
A product capture is the REAL platform interface exercising the requested flow,
case, actor and environment. Never fabricate it or alter the DOM to simulate success.
Reuse authorized access, headless execution and protect personal data. Identify
case, target/environment, version, time, scope and observed outcome. A partial/admin
capture cannot prove an entire user flow, historical repair or future generation.
Keep technical proof, visual proof, readiness for retest and human acceptance distinct.
If access or reproduction is missing, record the specific pending result, never fake it.
These rules apply to Principal, workers, delegated work, new sessions and resumes.
Final acceptance always uses the existing Principal review, even in record mode.
No new owner approval: return missing evidence/corrections to this same executor.
Semantic quality and capture authenticity require explicit Principal inspection;
file hashes, extensions, lint and regex do not certify them.
Final assessment retains request_alignment, scope_assessment and criteria, and adds:
quality_review={policy_version:1, engineering:{verdict:general_rule|not_applicable,
rationale:source/diff-grounded explanation, evidence:[inspected artifact refs]},
validation:{reproduction:reproduced|not_reproduced|not_applicable, rationale:why,
cases:{positive:coverage,negative:coverage,conflict:coverage,unseen:coverage},
executions:[native nfos_tool call IDs]}, artifacts:[{ref,sha256,
kind:product_capture|technical|execution|data_repair|source_document|rendered_report,
case,actor,environment,target,version,scope,source,captured_at,observed,
authentic:true,unaltered:true}]}.
Each final criterion includes proof={kind,case,actor,environment,target,version,scope,
coverage:full|partial,data_scope:none|new_flow|historical} describing what the ORIGINAL
request requires. Match inspected artifacts to these fields, not the other way round.
artifact.kind is your independent classification, never merely the worker label.
Product captures require source, timezone-aware captured_at, authentic/unaltered and
observed behavior. Required tests use completed native nfos_tool IDs with real output;
inspect argv/output and exit code for the asserted test. Run commands through the
existing nfos_tool CLI as you work; reuse valid receipts, never rerun only for paperwork.
For general_rule engineering provide all four case-coverage explanations and execution
receipts. not_applicable needs the factual reason no code rule/test is being claimed.
A historical criterion requires data_repair evidence; publishing code alone cannot pass.
Accept only full proof. Keep missing functional proof pending through changes;
verdict observe can record a genuine nonfunctional limitation with partial coverage.
Never promote an observation into PASS or manufacture a human acceptance.
"""


class QualityReviewChanges(d.WorkflowError):
    """A concrete correction returned by the existing decision path, not a human gate."""


def _quality_text(value):
    return isinstance(value, str) and bool(value.strip())


def quality_review(conn, task_id, assessment):
    """Check provenance/coverage; the Principal, not a heuristic, judges semantics.

    Only persisted local bytes and native completed executions back the receipt.
    This deliberately does not claim to detect a Principal lying about an image.
    """
    def require(ok, message, *, reviewer=False):
        if not ok:
            if reviewer:
                raise d.WorkflowError('Principal review incomplete: '+message+'; complete this same decision, without worker or human paperwork')
            raise QualityReviewChanges(message)

    q = assessment.get('quality_review') if isinstance(assessment, dict) else None
    require(isinstance(q, dict) and q.get('policy_version') == QUALITY_POLICY_VERSION,
            'Review engineering and classify the current evidence using quality_review policy v1', reviewer=True)
    report = d._artifact(conn, task_id, 'report')
    checks = json.loads(report['evidence']).get('artifact_checks', [])
    linked = {key: c for c in checks if c.get('status') == 'verified_local'
              for key in (c['ref'], c.get('path')) if key}
    engineering = q.get('engineering')
    require(isinstance(engineering, dict), 'Explain the mechanism/general rule, or why engineering is not applicable', reviewer=True)
    require(engineering.get('verdict') in {'general_rule', 'not_applicable'}
            and _quality_text(engineering.get('rationale')),
            'Fix the mechanism/domain rule; a case-specific patch or unreviewed change cannot be accepted')
    refs = engineering.get('evidence')
    require(isinstance(refs, list) and refs and all(isinstance(r, str) and r in linked for r in refs),
            'Engineering judgment needs inspected source/diff/report evidence', reviewer=True)
    validation = q.get('validation')
    require(isinstance(validation, dict) and validation.get('reproduction') in
            {'reproduced', 'not_reproduced', 'not_applicable'} and _quality_text(validation.get('rationale')),
            'Record actual reproduction, or the specific not-reproduced/not-applicable limitation', reviewer=True)
    calls = validation.get('executions')
    require(isinstance(calls, list) and all(isinstance(c, str) for c in calls)
            and len(calls) == len(set(calls)), 'List distinct native execution IDs, not claimed test results', reviewer=True)
    if engineering['verdict'] == 'general_rule':
        cases = validation.get('cases')
        require(isinstance(cases, dict) and all(_quality_text(cases.get(k)) for k in
                ('positive', 'negative', 'conflict', 'unseen')) and bool(calls),
                'General-rule acceptance needs varied positive/negative/conflict/unseen validation and executed tests')
    executions = []
    for call_id in calls:
        require(conn.execute("SELECT 1 FROM sqlite_master WHERE name='nfos_tool_calls'").fetchone(),
                'Test execution is unproven; use the existing native tool receipt')
        call = conn.execute('SELECT * FROM nfos_tool_calls WHERE id=? AND task_id=?', (call_id, task_id)).fetchone()
        require(call and call['status'] == 'succeeded' and call['returncode'] == 0
                and call['started_at'] is not None and call['finished_at'] is not None,
                'Test execution must be completed successfully on this card, not planned or borrowed')
        chunks = conn.execute('SELECT seq,stream,content FROM nfos_tool_chunks WHERE call_id=? ORDER BY seq', (call_id,)).fetchall()
        require(any(c['content'] for c in chunks), 'Inspect the real command output before claiming tests passed')
        digest = hashlib.sha256()
        for chunk in chunks:
            data = chunk['content']
            digest.update(d._json([chunk['seq'], chunk['stream']]).encode())
            digest.update(data.encode() if isinstance(data, str) else bytes(data))
        executions.append({'id': call_id, 'run_id': call['run_id'], 'argv_sha256': hashlib.sha256(call['argv_json'].encode()).hexdigest(),
                           'cwd': call['cwd'], 'exit': call['returncode'], 'finished_at': call['finished_at'],
                           'output_sha256': digest.hexdigest()})
    artifacts = q.get('artifacts')
    require(isinstance(artifacts, list) and artifacts, 'Classify the evidence actually inspected', reviewer=True)
    reviewed = {}
    fields = ('case', 'actor', 'environment', 'target', 'version', 'scope')
    kinds = {'product_capture', 'technical', 'execution', 'data_repair', 'source_document', 'rendered_report'}
    for item in artifacts:
        require(isinstance(item, dict) and isinstance(item.get('ref'), str) and item['ref'] in linked,
                'Evidence review must reference a saved local report artifact', reviewer=True)
        check = linked[item['ref']]
        require(check['ref'] not in reviewed, 'Classify each artifact only once', reviewer=True)
        require(item.get('sha256') == check['sha256'] and item.get('kind') in kinds
                and all(_quality_text(item.get(k)) for k in fields + ('observed',)),
                'Record artifact hash, actual classification, case, environment, version, scope and observation', reviewer=True)
        if item['kind'] == 'product_capture':
            require(check.get('media_type') == 'image',
                    'Product capture needs actual image bytes plus independent inspection, not an image filename')
            from datetime import datetime
            try:
                captured = datetime.fromisoformat(str(item.get('captured_at', '')).replace('Z', '+00:00'))
                timed = captured.tzinfo is not None and captured.timestamp() <= time.time() + 60
            except (ValueError, OverflowError):
                timed = False
            require(item.get('authentic') is True and item.get('unaltered') is True
                    and timed and _quality_text(item.get('source')),
                    'Inspect the real unaltered platform capture and its source/time; report images and mockups are not product evidence')
        reviewed[check['ref']] = item
    from hermes_cli.nfos_destination import destination
    dest = destination(conn, task_id)
    state = json.loads(d.get_workflow(conn, task_id)['state_json'])
    version = state.get('integrated_sha') or state.get('candidate_sha')
    for row in assessment['criteria']:
        proof = row.get('proof')
        require(isinstance(proof, dict) and proof.get('kind') in kinds
                and all(_quality_text(proof.get(k)) for k in fields)
                and proof.get('coverage') in {'full', 'partial'}
                and proof.get('data_scope') in {'none', 'new_flow', 'historical'},
                f"Criterion {row['id']}: describe the original request's case, target, version, scope and required proof", reviewer=True)
        require(row['verdict'] != 'accept' or proof['coverage'] == 'full',
                f"Criterion {row['id']}: partial evidence cannot prove the whole result")
        if row['verdict'] != 'accept':
            continue  # Honest observations do not become a functional PASS.
        if dest and proof['kind'] in {'product_capture', 'data_repair'}:
            require(proof['environment'] == dest['environment'] and proof['target'] == dest['target'],
                    f"Criterion {row['id']}: evidence does not target the accepted destination")
        if version and proof['kind'] in {'product_capture', 'execution', 'data_repair'}:
            require(proof['version'] == version, f"Criterion {row['id']}: evidence is for another candidate/version")
        matched = []
        for ref in row['evidence']:
            item = reviewed.get(linked[ref]['ref'])
            if item and item['kind'] == proof['kind'] and all(item[k] == proof[k] for k in fields):
                matched.append(item)
        require(matched, f"Criterion {row['id']}: classify and inspect proof of this case/scope/environment; a report is not a product capture")
        require(proof['data_scope'] != 'historical' or proof['kind'] == 'data_repair',
                f"Criterion {row['id']}: code publication is not proof of saved-data repair")
        require(proof['kind'] != 'execution' or bool(executions),
                f"Criterion {row['id']}: test results require a completed native execution")
    return executions


def settings():
    from hermes_cli.config import load_config
    return (load_config().get('kanban') or {}).get('delivery') or {}


def required(conn, task_id, kind=None):
    # Record mode removes orchestration gates, not the Principal's judgment of
    # scope and outcome. Both reviews use the existing decisions on this card.
    workflow = d.get_workflow(conn, task_id)
    if not workflow:
        return False
    if kind == 'final_review':
        return True
    if settings().get('result_review') is True:
        return True
    if settings().get('principal_validation') is False:
        return False
    spec = d.get_spec(conn, task_id)
    if spec and json.loads(spec['content']).get('delivery_destination'):
        return True
    request = d.get_request(conn, workflow['request_id'])
    project = json.loads(request['payload']).get('project', {}) if request else {}
    return settings().get('principal_validation') is True or project.get('principal_validation') is True


def worker_model_args(task, conn=None):
    policy = settings()
    if task.model_override:
        # Explicit project/card pins outrank a global worker default.
        model, provider, effort = task.model_override, task.provider_override, task.reasoning_effort
    else:
        model = policy.get('worker_model') or task.model_override
        provider = policy.get('worker_provider') or task.provider_override
        effort = policy.get('worker_reasoning_effort') or task.reasoning_effort
    if conn is not None:
        escalation = worker_escalation(conn, task.id)
        if escalation and not task.model_override and model in ('gpt-5.6-luna', 'gpt-6-luna') and provider in (None, 'openai-codex'):
            # A retained Luna tier follows the configured generation, keeping its effort.
            model = model if escalation['model'] in ('gpt-5.6-luna', 'gpt-6-luna') else escalation['model']
            provider, effort = 'openai-codex', escalation['reasoning_effort']
    args = ['-m', model] if model else []
    if model and provider:
        args += ['--provider', provider]
    if effort:
        args += ['--reasoning', effort]
    return args


def worker_escalation(conn, task_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='nfos_workflows'").fetchone():
        return {}
    workflow = d.get_workflow(conn, task_id)
    return json.loads(workflow['state_json']).get('worker_escalation', {}) if workflow else {}


def impediment_identity(conn, task_id, context):
    task = d._kb().get_task(conn, task_id)
    spec = d.get_spec(conn, task_id)
    state = json.loads(d.get_workflow(conn, task_id)['state_json'])
    evidence = {}
    refs = context.get('evidence') or []
    refs = [refs] if isinstance(refs, str) else refs if isinstance(refs, list) else []
    for ref in refs:
        if not isinstance(ref, str):
            continue
        try:
            if Path(ref).is_file():
                evidence[ref] = hashlib.sha256(Path(ref).read_bytes()).hexdigest()
        except (OSError, ValueError):
            continue  # Unavailable references remain in context; asking is still possible.
    tool_evidence = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='nfos_tool_calls'").fetchone():
        row = conn.execute('SELECT id,finished_at,status,returncode FROM nfos_tool_calls WHERE task_id=? AND finished_at IS NOT NULL ORDER BY rowid DESC LIMIT 1', (task_id,)).fetchone()
        tool_evidence = dict(row) if row else None
    return dict(instruction_revision=task.instruction_revision,
                spec_revision=spec['revision'] if spec else None,
                candidate={k:state.get(k) for k in ('candidate_sha','candidate_tree','integrated_sha','delivery_readback')},
                evidence=evidence, tool_evidence=tool_evidence)


def _stagnation_evidence(conn, decision, failure):
    """Principal judgment backed by a correction and a subsequent native tool receipt."""
    task_id = decision['task_id']
    task = d._kb().get_task(conn, task_id)
    context = json.loads(decision['context'])
    prior = d.get_decision(conn, failure.get('prior_decision_id'))
    if (not prior or prior['id'] == decision['id'] or prior['task_id'] != task_id
            or prior['status'] != 'resolved' or prior['action'] != 'changes' or prior['author'] != 'Principal'
            or prior['kind'] != decision['kind'] or prior['spec_revision'] != decision['spec_revision']
            or task.current_run_id != decision['run_id']):
        raise d.WorkflowError('Stagnation needs a prior Principal correction in the same scope and current attempt')
    field = 'acceptance_identity' if decision['kind'] == 'spec_review' else 'impediment_identity'
    current = identity(conn, task_id, 'spec_review') if field == 'acceptance_identity' else impediment_identity(conn, task_id, context)
    prior_context = json.loads(prior['context'])
    prior_scope = dict(prior_context.get(field) or {})
    current_scope = dict(current)
    # A new tool result is required after the correction; it changes evidence,
    # not the instruction/spec/candidate scope against which it is judged.
    prior_scope.pop('tool_evidence', None); current_scope.pop('tool_evidence', None)
    if (context.get(field) != current or prior_scope != current_scope
            or decision['kind'] == 'impediment' and prior_context.get('cause') != context.get('cause')):
        raise d.WorkflowError('Stagnation scope or evidence changed; review the current attempt')
    calls = failure.get('tool_call_ids')
    if not isinstance(failure.get('reason'), str) or not failure['reason'].strip() or not isinstance(calls, list) or not calls:
        raise d.WorkflowError('Stagnation needs a diagnosis and inspected native tool output')
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='nfos_tool_calls'").fetchone():
        raise d.WorkflowError('Stagnation needs persisted native tool output')
    for call_id in calls:
        call = conn.execute('SELECT * FROM nfos_tool_calls WHERE id=? AND task_id=? AND run_id=?',
                            (call_id,task_id,decision['run_id'])).fetchone()
        if (not call or call['finished_at'] is None or call['created_at'] < prior['resolved_at']
                or call['status'] not in ('succeeded','failed') or not conn.execute(
                    'SELECT 1 FROM nfos_tool_chunks WHERE call_id=? AND length(content)>0', (call_id,)).fetchone()):
            raise d.WorkflowError('Stagnation needs completed tool output from after the correction')


def escalate_rework(conn, decision, assessment):
    """Principal classifies a current functional rejection, never local test FAILs."""
    if settings().get('worker_escalation') is not True:
        return
    failure = (assessment or {}).get('failure')
    if not isinstance(failure, dict):
        return
    stagnation = (decision['kind'] in ('spec_review','impediment')
                  and failure.get('kind') == 'stagnation' and failure.get('cause') == 'model_reasoning')
    if not stagnation and (decision['kind'] != 'final_review' or failure.get('kind') != 'functional'):
        return
    task_id = decision['task_id']
    reason = failure.get('reason')
    report = None
    if stagnation:
        _stagnation_evidence(conn, decision, failure)
    else:
        current = identity(conn, task_id, 'final_review')
        if json.loads(decision['context']).get('acceptance_identity') != current:
            raise d.WorkflowError('Functional rejection requires the current report and instruction')
        reason = failure.get('reason')
        rows = failure.get('criteria')
        report = d._artifact(conn, task_id, 'report')
        checks = json.loads(report['evidence']).get('artifact_checks', [])
        if not isinstance(reason, str) or not reason.strip() or not isinstance(rows, list) or not rows:
            raise d.WorkflowError('Functional rejection requires a reason, criteria and inspected evidence')
        for row in rows:
            refs = row.get('evidence') if isinstance(row, dict) else None
            linked = [c for c in checks if row.get('id') in c.get('criteria', []) and c.get('status') == 'verified_local'] if isinstance(row, dict) else []
            if not isinstance(refs, list) or not refs or any(not any(ref in (c['ref'], c.get('path')) for c in linked) for ref in refs):
                raise d.WorkflowError('Functional rejection needs criterion-linked report evidence')
            for check in linked:
                actual = d._inspect_local_evidence(check['path'])
                if any(actual[k] != check[k] for k in ('sha256', 'size_bytes')):
                    raise d.WorkflowError('Functional rejection evidence changed; review the current bytes')
    task = d._kb().get_task(conn, task_id)
    args = worker_model_args(task)
    model = args[args.index('-m')+1] if '-m' in args else None
    effort = args[args.index('--reasoning')+1] if '--reasoning' in args else None
    provider = args[args.index('--provider')+1] if '--provider' in args else None
    if task.model_override or model not in ('gpt-5.6-luna', 'gpt-6-luna') or provider not in (None, 'openai-codex'):
        return  # Other model pins and already-higher models remain untouched.
    workflow = d.get_workflow(conn, task_id)
    state = json.loads(workflow['state_json'])
    old = state.get('worker_escalation') or {}
    source_run_id = decision['run_id'] if stagnation else report['run_id']
    # One tier per dispatched attempt, regardless of repeated questions/reports.
    if old and (old.get('status') in {'pending', 'exhausted'} or old.get('source_run_id') == source_run_id
                or report is not None and old.get('report_id') == report['id'] and old.get('report_revision') == report['revision']):
        return
    level = max(int(old.get('level', 0)), 1 if effort == 'max' else 0)
    if old and old.get('run_id') != source_run_id:
        return  # Only a correction actually dispatched on this tier can fail it.
    level = min(2, level + 1)
    exhausted = bool(old and old.get('level') == 2)
    escalation = dict(level=level, model=model if level == 1 else 'gpt-6-astra',
                      reasoning_effort='max' if level == 1 else 'low', reason=reason,
                      decision_id=decision['id'], report_id=report['id'] if report else None, report_revision=report['revision'] if report else None,
                      source_run_id=source_run_id, first_run_id=old.get('first_run_id', source_run_id),
                      status='exhausted' if exhausted else 'pending')
    if exhausted:
        escalation['run_id'] = old['run_id']
    state['worker_escalation'] = escalation
    conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?', (d._json(state), task_id))
    context = json.loads(decision['context']); context['assessment'] = assessment
    conn.execute('UPDATE nfos_decisions SET context=? WHERE id=?', (d._json(context), decision['id']))
    d._event(conn, task_id, task.current_run_id, 'nfos_worker_escalated', escalation)


def escalation_usage(conn, task_id, run_id):
    escalation = worker_escalation(conn, task_id)
    first = escalation.get('first_run_id', run_id)
    rows = conn.execute('SELECT started_at,ended_at,metadata FROM task_runs WHERE task_id=? AND id>=? AND id<?',
                        (task_id, first, run_id)).fetchall()
    return {'seconds': sum(max(0, (r['ended_at'] or r['started_at'])-r['started_at']) for r in rows),
            'iterations': sum(json.loads(r['metadata'] or '{}').get('escalation_usage', {}).get('iterations', 0) for r in rows),
            'turns': sum(json.loads(r['metadata'] or '{}').get('escalation_usage', {}).get('turns', 0) for r in rows)}


def iteration_grants(conn, task_id):
    """Read the append-only grants from the existing Project Ops event ledger."""
    return [json.loads(row['payload']) for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='nfos_iteration_budget_granted' ORDER BY id",
        (task_id,))]


def grant_iteration_budget(conn, task_id, *, grant_id, iterations, actor, reason,
                           expected_run_id, expected_instruction_revision, runtime_seconds=0):
    """Grant a finite allowance to an idle escalation lineage, without resuming it."""
    from agent.delegation_context import is_dispatcher_owned_worker_context
    if (os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')
            or not is_dispatcher_owned_worker_context()):
        raise d.WorkflowError('Budget grants belong to the Principal maintainer, outside a worker')
    if (type(iterations) is not int or iterations <= 0
            or type(runtime_seconds) is not int or runtime_seconds < 0
            or any(not isinstance(v, str) or not v.strip() for v in (grant_id, actor, reason))
            or type(expected_run_id) is not int or expected_run_id <= 0
            or type(expected_instruction_revision) is not int):
        raise d.WorkflowError('Specify a grant id, positive iterations, nonnegative runtime seconds, actor, reason and observed run/instruction')
    request = dict(grant_id=grant_id, iterations=iterations, actor=actor, reason=reason,
                   expected_run_id=expected_run_id, expected_instruction_revision=expected_instruction_revision,
                   runtime_seconds=runtime_seconds)
    from hermes_cli.nfos_workspace_repair import _idle
    with d._kb().write_txn(conn):
        grants = iteration_grants(conn, task_id)
        for saved in grants:
            if saved['grant_id'] == grant_id:
                if any(saved.get(k, 0 if k == 'runtime_seconds' else None) != v for k, v in request.items()):
                    raise d.WorkflowError('Grant id already exists with different parameters')
                return saved
        task = _idle(conn, task_id)
        last = conn.execute('SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1', (task_id,)).fetchone()
        first = worker_escalation(conn, task_id).get('first_run_id')
        if (not last or last['id'] != expected_run_id or task.instruction_revision != expected_instruction_revision
                or not first or not conn.execute('SELECT 1 FROM task_runs WHERE task_id=? AND id=?', (task_id, first)).fetchone()):
            raise d.WorkflowError('Card run, instruction or escalation lineage changed; read the current card')
        usage = escalation_usage(conn, task_id, expected_run_id + 1)
        previous_limit = task.max_runtime_seconds
        if runtime_seconds and (type(previous_limit) is not int or previous_limit <= 0):
            raise d.WorkflowError('Runtime grants require an explicit positive max_runtime_seconds')
        new_limit = previous_limit + runtime_seconds if runtime_seconds else previous_limit
        if runtime_seconds:
            conn.execute('UPDATE tasks SET max_runtime_seconds=? WHERE id=?', (new_limit, task_id))
        receipt = dict(request, task_id=task_id, first_run_id=first, at=int(time.time()),
                       previous_max_runtime_seconds=previous_limit, max_runtime_seconds=new_limit,
                       prior_runtime_seconds=usage['seconds'],
                       remaining_runtime_seconds=max(0, new_limit - usage['seconds']) if new_limit else None,
                       prior_iterations=usage['iterations'],
                       granted_iterations=sum(g['iterations'] for g in grants if g['first_run_id'] == first) + iterations)
        d._event(conn, task_id, expected_run_id, 'nfos_iteration_budget_granted', receipt)
        return receipt


def _owned_worker_run(conn, task_id, run_id, claim):
    """Read the current process owner; callers recheck inside each write transaction."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, is_dispatcher_owned_worker_context
    if (not claim or not is_dispatcher_owned_worker_context()
            or os.environ.get(DELEGATED_CHILD_ENV_MARKER) == '1'):
        return None
    pid = os.getpid()
    row = conn.execute("SELECT r.metadata,t.worker_started_at FROM task_runs r "
                       "JOIN tasks t ON t.current_run_id=r.id "
                       "WHERE t.id=? AND r.id=? AND r.task_id=t.id AND t.claim_lock=? "
                       "AND t.status='running' AND r.ended_at IS NULL "
                       "AND t.worker_pid=? AND r.worker_pid=?", (task_id, int(run_id), claim, pid, pid)).fetchone()
    if row is None or row['worker_started_at'] is None:
        return None
    started_at = d._kb()._process_start_time(pid)
    if started_at is None or abs(started_at - row['worker_started_at']) >= 0.01:
        return None
    return row


def worker_checkpoint(agent, turn_id=None):
    """Called only between completed tool batches, before another model call."""
    task_id, run_id = os.environ.get('HERMES_KANBAN_TASK'), os.environ.get('HERMES_KANBAN_RUN_ID')
    if not task_id or not run_id:
        return False
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, is_dispatcher_owned_worker_context
    if (not is_dispatcher_owned_worker_context()
            or os.environ.get(DELEGATED_CHILD_ENV_MARKER) == '1'):
        return False
    claim = os.environ.get('HERMES_KANBAN_CLAIM_LOCK')
    with d._kb().connect_closing() as conn:
        if _owned_worker_run(conn, task_id, run_id, claim) is None:
            return False
        task = d._kb().get_task(conn, task_id)
        pending = worker_escalation(conn, task_id)
        if not task or task.current_run_id != int(run_id):
            return False
        if task.claim_lock != os.environ.get('HERMES_KANBAN_CLAIM_LOCK'):
            return False
        execution = {'model': agent.model, 'provider': getattr(agent, 'provider', None),
                     'reasoning_effort': (agent.reasoning_config or {}).get('effort'),
                     'session_id': getattr(agent, 'session_id', None), 'source': 'agent_pre_request'}
        with d._kb().write_txn(conn):
            row = _owned_worker_run(conn, task_id, run_id, claim)
            if row is None:
                return False
            metadata = json.loads(row['metadata'] or '{}')
            previous = metadata.get('worker_execution', {})
            if any(previous.get(k) != v for k, v in execution.items()):
                execution['recorded_at'] = int(time.time())
                metadata['worker_execution'] = execution
                conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (d._json(metadata), int(run_id)))
                d._event(conn, task_id, int(run_id), 'nfos_worker_execution', execution)
        if not pending and settings().get('worker_escalation') is not True:
            return False
        budget = getattr(agent, 'iteration_budget', None)
        if budget is not None:
            prior = escalation_usage(conn, task_id, int(run_id))
            if not getattr(agent, '_nfos_budget_loaded', False):
                first = pending.get('first_run_id', int(run_id))
                grants = [g for g in iteration_grants(conn, task_id) if g['first_run_id'] == first]
                allowance = dict(base_iterations=budget.max_total, prior_iterations=prior['iterations'],
                                 granted_iterations=sum(g['iterations'] for g in grants),
                                 grant_ids=[g['grant_id'] for g in grants], first_run_id=first)
                budget.max_total = max(0, budget.max_total + allowance['granted_iterations'] - prior['iterations'])
                allowance['effective_iterations'] = budget.max_total
                agent._nfos_iteration_allowance = allowance
                agent._nfos_budget_loaded = True
            with d._kb().write_txn(conn):
                row = _owned_worker_run(conn, task_id, run_id, claim)
                if row is None:
                    return False
                metadata = json.loads(row['metadata'] or '{}')
                metadata['iteration_budget'] = agent._nfos_iteration_allowance
                usage = metadata.setdefault('escalation_usage', {'turns': 0})
                usage['iterations'] = budget.used
                if turn_id and usage.get('turn_id') != turn_id and not (
                        pending.get('status') == 'pending' and pending.get('source_run_id') == int(run_id)):
                    usage.update(turn_id=turn_id, turns=usage['turns']+1)
                metadata['escalation_usage'] = usage
                conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (d._json(metadata), int(run_id)))
        if pending.get('status') != 'pending':
            return False
        if pending.get('source_run_id') != int(run_id):
            with d._kb().write_txn(conn):
                if _owned_worker_run(conn, task_id, run_id, claim) is None:
                    return False
                confirm_worker_dispatch(conn, task_id, int(run_id), agent.model,
                                        (agent.reasoning_config or {}).get('effort'))
            return False
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='nfos_tool_calls'").fetchone() and conn.execute(
                "SELECT 1 FROM nfos_tool_calls WHERE task_id=? AND run_id=? AND status IN ('intent','running','stopping')",
                (task_id, int(run_id))).fetchone():
            return False
        # Keep the claim/run live until this process has flushed its session and exited.
        agent._nfos_escalation_yield = True
        return True


def record_worker_iteration(agent):
    """Persist consumed allowance before a tool can complete this run."""
    task_id, run_id = os.environ.get('HERMES_KANBAN_TASK'), os.environ.get('HERMES_KANBAN_RUN_ID')
    claim = os.environ.get('HERMES_KANBAN_CLAIM_LOCK')
    budget = getattr(agent, 'iteration_budget', None)
    if not task_id or not run_id or not claim or budget is None:
        return
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, is_dispatcher_owned_worker_context
    if (not is_dispatcher_owned_worker_context()
            or os.environ.get(DELEGATED_CHILD_ENV_MARKER) == '1'):
        return
    with d._kb().connect_closing() as conn:
        if not worker_escalation(conn, task_id) and settings().get('worker_escalation') is not True:
            return
        with d._kb().write_txn(conn):
            row = _owned_worker_run(conn, task_id, run_id, claim)
            if row is None:
                return
            metadata = json.loads(row['metadata'] or '{}')
            metadata.setdefault('escalation_usage', {})['iterations'] = budget.used
            conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (d._json(metadata), int(run_id)))


def reclaim_escalation(conn, task_id):
    """Dispatcher calls only after confirming the old process is dead."""
    task = d._kb().get_task(conn, task_id)
    pending = worker_escalation(conn, task_id)
    if not task or pending.get('status') != 'pending' or pending.get('source_run_id') != task.current_run_id:
        return False
    d._kb()._end_run(conn, task_id, outcome='reclaimed', status='reclaimed',
                    metadata={'worker_escalation': pending, 'retry_status': 'ready'})
    conn.execute("UPDATE tasks SET status='ready',claim_lock=NULL,claim_expires=NULL,worker_pid=NULL WHERE id=?", (task_id,))
    d._event(conn, task_id, pending['source_run_id'], 'nfos_worker_checkpoint', pending)
    return True


def confirm_worker_dispatch(conn, task_id, run_id, model, reasoning):
    """The initialized worker confirms the selected model, not merely Popen success."""
    pending = worker_escalation(conn, task_id)
    if pending.get('status') != 'pending' or run_id == pending.get('source_run_id'):
        return
    task = d._kb().get_task(conn, task_id)
    args = worker_model_args(task, conn)
    expected_model = args[args.index('-m')+1] if '-m' in args else None
    expected_effort = args[args.index('--reasoning')+1] if '--reasoning' in args else None
    if expected_model != pending['model'] or (expected_effort and expected_effort != pending['reasoning_effort']):
        if model != expected_model or (expected_effort and reasoning != expected_effort):
            raise d.WorkflowError('Worker dispatch did not apply the effective model policy')
        pending.update(model=model, reasoning_effort=reasoning,
                       reason='Effective model policy superseded the pending escalation')
    if model != pending['model'] or reasoning != pending['reasoning_effort']:
        raise d.WorkflowError('Worker dispatch did not apply the pending model and reasoning')
    workflow = d.get_workflow(conn, task_id); state = json.loads(workflow['state_json'])
    pending.update(status='applied', run_id=run_id)
    state['worker_escalation'] = pending
    conn.execute('UPDATE nfos_workflows SET state_json=? WHERE task_id=?', (d._json(state), task_id))
    d._event(conn, task_id, run_id, 'nfos_worker_escalated', pending)


def identity(conn, task_id, kind):
    spec = d.get_spec(conn, task_id)
    if not spec:
        raise d.WorkflowError('Principal acceptance requires the persisted spec')
    task = d._kb().get_task(conn, task_id)
    result = d._legacy_spec_identity(task, spec)
    result['spec_revision'] = spec['revision']
    if kind == 'final_review':
        result['quality_policy_version'] = QUALITY_POLICY_VERSION
        report = d._artifact(conn, task_id, 'report')
        if not report:
            raise d.WorkflowError('Final review requires the saved report')
        result.update(report_id=report['id'], report_revision=report['revision'],
                      report_sha256=hashlib.sha256(report['content'].encode()).hexdigest(),
                      evidence_sha256=hashlib.sha256(report['evidence'].encode()).hexdigest())
        state = json.loads(d.get_workflow(conn, task_id)['state_json'])
        result['candidate'] = {k: state.get(k) for k in (
            'candidate_sha', 'candidate_tree', 'homolog_sha', 'homolog_evidence',
            'integrated_sha', 'artifact', 'production_readback', 'homologation_decision')}
        from hermes_cli.nfos_destination import destination
        scope = destination(conn, task_id)
        if scope:
            result['delivery_destination'] = scope
            result['delivery_readback'] = state.get('delivery_readback')
            result['destination_effects'] = [dict(row) for row in conn.execute(
                "SELECT id,status,evidence FROM nfos_effects WHERE task_id=? AND operation=? AND target=? ORDER BY id",
                (task_id, scope['verification_operation'], scope['target']))]
    return result


def accepted(conn, task_id, kind):
    if not required(conn, task_id, kind):
        return True
    try:
        current = identity(conn, task_id, kind)
    except d.WorkflowError:
        return False
    # A newer rejection/pending review revokes the former acceptance.
    row = conn.execute('SELECT * FROM nfos_decisions WHERE task_id=? AND kind=? ORDER BY rowid DESC LIMIT 1',
                       (task_id, kind)).fetchone()
    historical = bool(kind == 'final_review' and row
                      and d._kb().get_task(conn, task_id).status == 'done'
                      and 'quality_policy_version' not in json.loads(row['context']).get('acceptance_identity', {}))
    if historical:
        current.pop('quality_policy_version', None)  # Preserve terminal history; reopened work must use v1.
    if kind == 'final_review' and not historical and row and row['status'] == 'resolved' and row['action'] == 'continue':
        assessment = json.loads(row['context']).get('assessment') or {}
        try:
            if quality_review(conn, task_id, assessment) != assessment.get('quality_review', {}).get('execution_receipts'):
                return False
        except (d.WorkflowError, KeyError, TypeError, ValueError):
            return False
    return bool(row and row['status'] == 'resolved' and row['action'] == 'continue'
                and row['author'] == 'Principal'
                and json.loads(row['context']).get('acceptance_identity') == current
                and json.loads(row['context']).get('assessment'))


def require_spec(conn, task_id):
    if not accepted(conn, task_id, 'spec_review'):
        raise d.WorkflowError('Principal spec acceptance is pending; ask kind=spec_review and wait before implementation')


def complete_accepted(conn, decision_id):
    """Finalize the accepted run through the normal closure fences, not another model turn."""
    from hermes_cli import kanban_db as kb
    decision = d.get_decision(conn, decision_id)
    if (not decision or decision['kind'] != 'final_review'
            or decision['status'] != 'resolved' or decision['action'] != 'continue'
            or decision['author'] != 'Principal'):
        return False
    task = kb.get_task(conn, decision['task_id'])
    if (not task or task.status == 'done' or task.current_run_id is None
            or task.current_run_id != decision['run_id']):
        return False
    if not accepted(conn, task.id, 'final_review'):
        old_identity = json.loads(decision['context']).get('acceptance_identity', {})
        if task.status == 'running' and old_identity.get('quality_policy_version') != QUALITY_POLICY_VERSION:
            d.ask_principal(conn, task.id, task.current_run_id, kind='final_review',
                question='Review the retained result under the current quality policy; reuse evidence and do not repeat delivery',
                context={'quality_upgrade_of': decision_id})
        return False
    report = d._artifact(conn, task.id, 'report')
    content = json.loads(report['content'])
    summary = str(content.get('summary') or '').strip()
    if not summary:
        return False
    # complete_task rechecks current evidence, destination, human decisions,
    # Git receipt and run ownership. It also preserves native cleanup/history.
    return kb.complete_task(conn, task.id, expected_run_id=task.current_run_id,
        result=summary, summary=summary, metadata={
            'completion_actor': 'Principal dispatcher',
            'principal_decision_id': decision_id,
            'report_revision': report['revision'],
        })


def complete_accepted_deliveries(conn):
    """Existing dispatch tick owns this work; Principal admission never waits on Git/hooks."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='nfos_decisions'").fetchone():
        return []
    rows = conn.execute("""
        SELECT d.id, d.task_id FROM nfos_decisions d JOIN tasks t ON t.id=d.task_id
        WHERE d.kind='final_review' AND d.status='resolved' AND d.action='continue'
          AND d.author='Principal' AND t.status IN ('running','blocked','review')
          AND t.current_run_id=d.run_id
          AND NOT EXISTS (SELECT 1 FROM nfos_decisions newer
              WHERE newer.task_id=d.task_id AND newer.kind='final_review' AND newer.rowid>d.rowid)
        ORDER BY t.priority DESC, d.resolved_at ASC, d.rowid ASC
    """).fetchall()
    completed = []
    for row in rows:
        try:
            if complete_accepted(conn, row['id']):
                completed.append(row['task_id'])
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Accepted delivery completion deferred for %s', row['task_id'])
    return completed


def final_assessment(conn, task_id):
    """Only the current, independently accepted result may authorize closure."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nfos_decisions'").fetchone():
        return {}
    row = conn.execute("SELECT * FROM nfos_decisions WHERE task_id=? AND kind='final_review' ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
    if not row or row['status'] != 'resolved' or row['action'] != 'continue' or row['author'] != 'Principal':
        return {}
    context=json.loads(row['context'])
    return context.get('assessment', {}) if accepted(conn, task_id, 'final_review') else {}


def _unresolved_functional_failures(spec, report):
    """An open functional blocker cannot become a documentary observation."""
    delivery = report.get('delivery') if isinstance(report.get('delivery'), dict) else {}
    blockers = [*(report.get('blockers') or []), *(delivery.get('blockers') or [])]
    unresolved = any(not isinstance(b, dict) or b.get('status', 'unresolved') not in {'resolved', 'closed'}
                     for b in blockers)
    if not unresolved and report.get('functional_delivery') is not False and delivery.get('functional_delivery') is not False:
        return set()
    mandatory = {c['id'] for c in json.loads(spec['content'])['criteria'] if c.get('mandatory')}
    return {cid for cid, row in d._report_results(spec, report).items()
            if cid in mandatory and row['status'] == 'FAIL'}


def observed_criteria(conn, task_id):
    observed = {row['id'] for row in final_assessment(conn, task_id).get('criteria', [])
                if row.get('verdict') == 'observe'}
    if observed:
        report = d._artifact(conn, task_id, 'report')
        observed -= _unresolved_functional_failures(d.get_spec(conn, task_id), json.loads(report['content']))
    return observed


def closeout_packet(conn, task_id):
    """Project Ops persists this reviewed resolution and its actual image files."""
    assessment = final_assessment(conn, task_id)
    report = d._artifact(conn, task_id, 'report')
    if not assessment or not report:
        return None
    content = json.loads(report['content'])
    observations = [r['id'] + ': ' + r['observation'] for r in assessment['criteria'] if r.get('verdict') == 'observe']
    checks = json.loads(report['evidence']).get('artifact_checks', [])
    classified = {key: item for item in assessment.get('quality_review', {}).get('artifacts', [])
                  for check in checks if item['ref'] in (check['ref'], check.get('path'))
                  for key in (check['ref'], check.get('path')) if key}
    fields = ('kind', 'case', 'actor', 'environment', 'target', 'version', 'scope')
    visual = {ref for row in assessment['criteria']
              if row['verdict'] == 'accept' and row.get('proof', {}).get('kind') == 'product_capture'
              and row['proof'].get('coverage') == 'full' for ref in row['evidence']
              if ref in classified and all(classified[ref].get(k) == row['proof'][k] for k in fields)}
    images = [c['path'] for c in checks
              if c.get('status') == 'verified_local' and (c['ref'] in visual or c.get('path') in visual)]
    return {'resolution': assessment.get('resolution') or content.get('summary') or assessment['request_alignment'],
            'observations': observations, 'images': list(dict.fromkeys(images)),
            'learning': assessment.get('learning') or assessment.get('resolution') or assessment['scope_assessment'],
            'fully_proven': not observations}


def persist_closeout_learning(conn, task_id, packet):
    """Use Hermes' native memory store; failure is recorded, never a human gate."""
    from tools.memory_tool import MemoryStore
    from hermes_cli.config import load_config
    task = d._kb().get_task(conn, task_id)
    wf = d.get_workflow(conn, task_id)
    request = d.get_request(conn, wf['request_id'])
    project = json.loads(request['payload']).get('project', {}) if request else {}
    board = project.get('board') or task.project_id or 'project'
    entry = f"[{board}] {packet['learning']}"
    if packet['observations']:
        entry += ' Limitations: ' + '; '.join(packet['observations'])
    entry += f' (NFOS {task_id})'
    try:
        config = (load_config() or {}).get('memory') or {}
        store = MemoryStore(memory_char_limit=config.get('memory_char_limit', 2200))
        store.load_from_disk()
        result = store.add('memory', entry)
    except Exception as exc:
        result = {'success': False, 'error': str(exc)}
    with d._kb().write_txn(conn, allow_nested=True):
        d._event(conn, task_id, task.current_run_id, 'nfos_learning_saved' if result.get('success') else 'nfos_learning_pending',
                 {'native_memory': True, 'entry': entry,
                  'result': {key: result[key] for key in ('success', 'error') if key in result}})
    return result


def assess(conn, decision, assessment):
    """Validate coverage and current bytes, never infer semantic truth from prose."""
    if os.environ.get('HERMES_KANBAN_TASK'):
        raise d.WorkflowError('Only the Principal coordinator can accept a worker delivery')
    kind = decision['kind']
    task_id = decision['task_id']
    context = json.loads(decision['context'])
    if context.get('acceptance_identity') != identity(conn, task_id, kind):
        raise d.WorkflowError('Spec, instruction, candidate or report changed; request a current review')
    d._require_current_instruction_spec(conn, task_id)
    if not isinstance(assessment, dict) or not all(
            isinstance(assessment.get(k), str) and assessment[k].strip()
            for k in ('request_alignment', 'scope_assessment')):
        raise d.WorkflowError('Principal assessment needs request_alignment and scope_assessment')
    spec = d.get_spec(conn, task_id)
    expected = {c['id'] for c in json.loads(spec['content'])['criteria']}
    rows = assessment.get('criteria')
    if (not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows)
            or len(rows) != len(expected) or {r.get('id') for r in rows} != expected):
        raise d.WorkflowError('Principal must assess every spec criterion exactly once')
    allowed = {'accept', 'observe'} if kind == 'final_review' else {'accept'}
    if any(r.get('verdict') not in allowed or not isinstance(r.get('observation'), str)
           or not r['observation'].strip() for r in rows):
        raise d.WorkflowError('Each accepted criterion needs an explicit verdict and observation; otherwise request changes')
    if kind == 'final_review':
        require_spec(conn, task_id)
        report = d._artifact(conn, task_id, 'report')
        content = json.loads(report['content'])
        results = d._report_results(spec, content)
        observed = {r['id'] for r in rows if r['verdict'] == 'observe'}
        functional_failures = _unresolved_functional_failures(spec, content) & observed
        if functional_failures:
            raise d.WorkflowError(
                'Unresolved functional criteria cannot close as observations: ' + ', '.join(sorted(functional_failures))
                + '. Continue the unmet requirement in this same card. If a real scope transfer is necessary, '
                'use kanban_create continuation_of=<this card>, not a child dependent on its completion. '
                'A continuation does not prove the missing result; no additional owner approval is needed.')
        if observed and not str(assessment.get('resolution') or '').strip():
            raise d.WorkflowError('Closure with observations needs the Principal resolution; do not ask the owner to decide closure')
        if any(r['status'] != 'PASS' and cid not in observed for cid, r in results.items()):
            raise d.WorkflowError('Unproven criteria cannot receive final acceptance')
        checks = json.loads(report['evidence']).get('artifact_checks', [])
        for row in rows:
            refs = row.get('evidence')
            linked = [c for c in checks if row['id'] in c['criteria'] and c.get('status') == 'verified_local']
            # Principal names the precise inspected artifact, not just "looks good".
            if not isinstance(refs, list) or not refs or any(
                    not any(ref in (c['ref'], c.get('path')) for c in linked) for ref in refs):
                raise d.WorkflowError('Each final criterion needs inspected local report evidence or a saved external readback')
        for check in checks:
            if check.get('status') == 'verified_local':
                actual = d._inspect_local_evidence(check['path'])
                if any(actual[k] != check[k] for k in ('sha256', 'size_bytes')):
                    raise d.WorkflowError('Report evidence changed; save and review the current evidence')
    assessment = json.loads(d._json(assessment))
    if kind == 'final_review':
        try:
            executions = quality_review(conn, task_id, assessment)
        except d.WorkflowError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise d.WorkflowError('Malformed Principal quality review; complete the same decision without worker or human paperwork') from exc
        assessment['quality_review']['execution_receipts'] = executions
    return assessment
