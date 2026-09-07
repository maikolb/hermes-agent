"""A comment cannot start a second publication path for an NFOS-owned card."""
import time

import pytest

from hermes_cli import kanban_db as kb, nfos_delivery as d
from tests.hermes_cli.test_nfos_candidate_delivery import delivery, homolog, approved, A
from tests.hermes_cli.test_nfos_recovery_ownership import set_prior_worker


PR='https://github.com/example/pilot/pull/52'
GREEN={'state':'OPEN','mergeable':'MERGEABLE','headRefName':'fix/accepted-layout',
       'headRefOid':A,'statusCheckRollup':[{'conclusion':'SUCCESS'}]}


def capture_legacy_pr_io(monkeypatch, *, green=True):
    calls=[]
    def view(url):
        calls.append(('view',url))
        return dict(GREEN) if green else {**GREEN,'mergeable':'CONFLICTING',
            'statusCheckRollup':[{'conclusion':'FAILURE'}]}
    def merge(url,head_oid=''):
        calls.append(('merge',url,head_oid))
        return True,'--merge'
    monkeypatch.setenv('HERMES_KANBAN_AUTO_MERGE_ACTIVE_PR','on')
    monkeypatch.setattr(kb,'_gh_pr_json',view)
    monkeypatch.setattr(kb,'_gh_pr_merge',merge)
    return calls


def test_nfos_pr_comment_does_not_invoke_legacy_merge_before_worker_resume(delivery,monkeypatch):
    conn,task=delivery
    kb.add_comment(conn,task.id,'worker','Validated candidate PR: '+PR)
    calls=capture_legacy_pr_io(monkeypatch)
    set_prior_worker(conn,task,987654321)
    assert kb.reclaim_task(conn,task.id)
    assert kb.get_task(conn,task.id).status=='ready'
    before=conn.execute('SELECT count(*) FROM task_events WHERE task_id=?',(task.id,)).fetchone()[0]
    assert kb.check_respawn_guard(conn,task.id) is None
    assert calls==[], 'NFOS respawn must not perform a comment-driven PR view or merge'
    assert conn.execute('SELECT count(*) FROM task_events WHERE task_id=?',(task.id,)).fetchone()[0]==before
    assert conn.execute('SELECT count(*) FROM nfos_effects WHERE task_id=?',(task.id,)).fetchone()[0]==0


def test_nfos_review_work_is_not_held_by_legacy_active_pr_guard(delivery,monkeypatch):
    conn,task=delivery
    kb.add_comment(conn,task.id,'worker','PR needs the worker to address review: '+PR)
    calls=capture_legacy_pr_io(monkeypatch,green=False)
    set_prior_worker(conn,task,987654321)
    assert kb.reclaim_task(conn,task.id)
    assert kb.get_task(conn,task.id).status=='ready'
    assert kb.check_respawn_guard(conn,task.id) is None
    assert calls==[]


@pytest.mark.parametrize('reason',['rate_limit_cooldown','blocker_auth','recent_success'])
def test_nfos_retains_other_respawn_guards(delivery,monkeypatch,reason):
    conn,task=delivery
    kb.add_comment(conn,task.id,'worker','Existing PR: '+PR)
    calls=capture_legacy_pr_io(monkeypatch)
    now=int(time.time())+20
    monkeypatch.setattr(kb.time,'time',lambda:now)
    if reason=='blocker_auth':
        conn.execute('UPDATE tasks SET last_failure_error=? WHERE id=?',('Authentication failed: invalid API key',task.id))
    else:
        outcome='rate_limited' if reason=='rate_limit_cooldown' else 'completed'
        # The completion follows creation/requeue events, so it is recent success.
        conn.execute('UPDATE task_runs SET outcome=?,ended_at=? WHERE id=?',(outcome,now-5,task.current_run_id))
        monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS','300')
    conn.commit()
    assert kb.check_respawn_guard(conn,task.id)==reason
    assert calls==[]


@pytest.mark.parametrize('green',[True,False])
def test_unenrolled_card_keeps_legacy_pr_behavior_in_same_nfos_database(delivery,monkeypatch,green):
    conn,nfos_task=delivery
    legacy=kb.create_task(conn,title='Legacy task',assignee='default')
    kb.add_comment(conn,legacy,'worker','Existing PR: '+PR)
    assert d.get_workflow(conn,nfos_task.id) and not d.get_workflow(conn,legacy)
    calls=capture_legacy_pr_io(monkeypatch,green=green)
    assert kb.check_respawn_guard(conn,legacy)==(None if green else 'active_pr')
    expected=[('view',PR),('merge',PR,A)] if green else [('view',PR)]
    assert calls==expected


def test_nfos_merge_still_requires_current_review_then_uses_durable_effect(delivery,monkeypatch):
    conn,task=delivery
    kb.add_comment(conn,task.id,'worker','Candidate PR: '+PR)
    calls=capture_legacy_pr_io(monkeypatch)
    homolog(conn,task)
    assert kb.check_respawn_guard(conn,task.id) is None
    with pytest.raises(d.WorkflowError,match='review'):
        d.begin_effect(conn,task.id,task.current_run_id,operation='merge',target=PR,candidate=A)
    assert conn.execute("SELECT count(*) FROM nfos_effects WHERE operation='merge'").fetchone()[0]==0
    approved(conn,task)
    assert kb.check_respawn_guard(conn,task.id) is None
    effect=d.begin_effect(conn,task.id,task.current_run_id,operation='merge',target=PR,candidate=A)
    assert effect['execute'] and effect['status']=='unknown'
    assert conn.execute("SELECT count(*) FROM nfos_effects WHERE operation='merge'").fetchone()[0]==1
    assert calls==[], 'The worker publication protocol remains the sole external-effect path'
