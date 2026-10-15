"""Efeito que publica o worktree do card declara a candidata que está no HEAD dele (um SHA só)."""
import os
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as d


def git(repo, *args):
    return subprocess.run(['git','-C',str(repo),'-c','user.name=t','-c','user.email=t@example.invalid',*args],
                          check=True,capture_output=True,text=True).stdout.strip()


@pytest.fixture
def card(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    repo=tmp_path/'repo'
    repo.mkdir()
    git(repo,'init','-q','-b','main')
    git(repo,'commit','--allow-empty','-qm','antiga')
    old=git(repo,'rev-parse','HEAD')
    git(repo,'commit','--allow-empty','-qm','candidata')
    head=git(repo,'rev-parse','HEAD')
    with kb.connect_closing() as conn:
        rid=d.receive_request(conn, source={'platform':'telegram','chat_id':'1','thread_id':'2','message_id':'3'},
            text='Fix accepted layout',project={'board':'pilot','profile':'default','delivery_type':'code','repo_path':str(repo)})
        req=d.reserve_request(conn,capacity=2)
        task=d.bootstrap_card(conn,rid,req['claim_token'],pid=os.getpid())
        d.save_spec(conn,task.id,task.current_run_id,
            {'goal':'Readable layout','criteria':[{'id':'AC1','text':'No overlap'}],
             'steps':['Reproduce','Fix','Verify'],'delivery_type':'code'},
            author='Claude TL',evidence={'session':'tl-real-test-fixture'})
        assert d.acquire_project(conn,'pilot',task.id,task.current_run_id,head)
        d.advance(conn,task.id,task.current_run_id,'homolog',next_action='Review candidate',
            state={'homolog_sha':head,'candidate_tree':'c'*40,'homolog_evidence':['hml-result.json']})
        yield conn,task,old,head


def test_pr_for_a_candidate_that_is_not_the_worktree_head_is_refused(card):
    conn,task,old,head=card
    with pytest.raises(d.WorkflowError, match='exact HEAD of the card worktree'):
        d.begin_effect(conn,task.id,task.current_run_id,operation='pr',target='repo:branch',candidate=old)


def test_pr_for_the_worktree_head_is_accepted(card):
    conn,task,old,head=card
    effect=d.begin_effect(conn,task.id,task.current_run_id,operation='pr',target='repo:branch',candidate=head)
    assert effect['execute'] is True


def test_homolog_for_a_candidate_that_is_not_the_worktree_head_is_refused(card):
    conn,task,old,head=card
    with pytest.raises(d.WorkflowError, match='exact HEAD of the card worktree'):
        d.begin_effect(conn,task.id,task.current_run_id,operation='homolog',target='https://hml.example.test',candidate=old)


def test_worktree_without_readable_head_fails_closed(card, tmp_path):
    conn,task,old,head=card
    conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?",(str(tmp_path/'nao-existe'),task.id))
    conn.commit()
    with pytest.raises(d.WorkflowError, match='HEAD cannot be read'):
        d.begin_effect(conn,task.id,task.current_run_id,operation='pr',target='repo:branch',candidate=head)
