"""Project knowledge routing stays independent of the chosen worker model."""
import os
from hermes_cli import kanban_db as kb, nfos_delivery as d, nfos_principal_review as review


def test_new_worker_gets_explicit_project_scope_and_its_own_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    monkeypatch.setattr(review, 'settings', lambda: {'project_knowledge': {
        'enabled': True, 'graphify_command': '/srv/hermes/bin/graphify',
        'projects': {'concursa-ai': {'memory_project': 'Concursa_ai'}, 'dovcrm': {'memory_project': 'next-crm'}}}})
    with kb.connect_closing() as conn:
        for n,(project,scope) in enumerate([('concursa-ai','Concursa_ai'),('dovcrm','next-crm')]):
            rid=d.receive_request(conn, source={'platform':'telegram','chat_id':'1','thread_id':str(n),'message_id':'1'},
                                  text='Corrigir extração',project={'project_id':project,'profile':'default','delivery_type':'report'})
            claim=d.reserve_request(conn,capacity=3)
            task=d.bootstrap_card(conn,rid,claim['claim_token'],pid=os.getpid())
            checkout=str(tmp_path/project/'worktree with space')
            conn.execute('UPDATE tasks SET workspace_path=?, model_override=?, provider_override=? WHERE id=?',
                         (checkout,'deepseek-v4.1-flash' if n==0 else 'gpt-5.6-luna','opencode-go' if n==0 else 'openai-codex',task.id))
            conn.commit()
            prompt=d.worker_context(conn,task.id)
            assert f'"project":"{scope}"' in prompt
            assert checkout in prompt and '--no-cluster' in prompt
            assert "--out '" + checkout + "' (local code parsing" in prompt
            assert 'memory_query' in prompt and 'memory_read_page' in prompt
            assert 'query_graph' in prompt and 'not a delivery gate' in prompt
            assert 'AOF was disabled' in prompt
            assert ('"project":"next-crm"' in prompt) == (n==1)


def test_optional_knowledge_failure_preserves_case_and_lessons(monkeypatch):
    monkeypatch.setattr(d,'case_context',lambda *a:'CASE')
    monkeypatch.setattr(d,'lessons_context',lambda *a:'LESSONS')
    def unavailable(*args): raise OSError('service unavailable')
    monkeypatch.setattr(d,'project_knowledge_context',unavailable)
    prompt=d.worker_context(None,'task')
    assert prompt.startswith('CASELESSONS') and 'continue with project files' in prompt


def test_disabled_integration_does_not_change_existing_worker_context(monkeypatch):
    monkeypatch.setattr(review,'settings',lambda:{})
    assert d.project_knowledge_context(None,'task')==''
