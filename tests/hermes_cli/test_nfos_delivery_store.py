"""Real SQLite contract: intake, ownership, specs, review and uncertain effects."""
import json
import os
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from tests.hermes_cli.nfos_owner_question_switch import owner_questions_allowed  # noqa: F401

pytestmark = pytest.mark.usefixtures('owner_questions_allowed')  # NO_OWNER_QUESTIONS_UNIVERSAL_20261010


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path/'kanban.db'))
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path/'kanban.db'


def receive(conn, message='42'):
    return delivery.receive_request(conn,
        source={'platform':'telegram','chat_id':'-10001','thread_id':'8','message_id':message},
        text='Auditar a contagem e entregar o relatório.',
        project={'board':'pilot','profile':'default','delivery_type':'report'},
        attachments=[{'original':'/cache/photo.jpg','mime_type':'image/jpeg'}])


def started(conn):
    rid=receive(conn)
    request=delivery.reserve_request(conn, capacity=2)
    assert request['id']==rid
    task=delivery.bootstrap_card(conn,rid,request['claim_token'],pid=os.getpid())
    return task


def spec(conn, task):
    return delivery.save_spec(conn,task.id,task.current_run_id,
        {'goal':'Contagem verificada','criteria':[{'id':'AC1','text':'Total reconciliado'}],
         'steps':['Consultar','Reconciliar'], 'delivery_type':'report'},
        author='Claude TL', evidence={'session':'tl-001','output':'/evidence/tl.jsonl'})


def test_receive_is_durable_idempotent_and_does_not_create_card(board):
    with kb.connect_closing(board) as conn:
        rid=receive(conn)
        assert receive(conn)==rid
        assert conn.execute('select count(*) from tasks').fetchone()[0]==0
    with kb.connect_closing(board) as conn:
        row=delivery.get_request(conn,rid)
        assert row['status']=='pending'
        assert json.loads(row['payload'])['attachments'][0]['original']=='/cache/photo.jpg'


def test_two_connections_cannot_reserve_same_worker_request(board):
    with kb.connect_closing(board) as conn:
        receive(conn)
    def claim(_):
        with kb.connect_closing(board) as conn:
            return delivery.reserve_request(conn,capacity=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(claim,range(2)))
    assert sum(row is not None for row in results)==1


def test_bootstrap_retransmission_keeps_card_run_and_owner(board):
    with kb.connect_closing(board) as conn:
        rid=receive(conn)
        request=delivery.reserve_request(conn,capacity=2)
        first=delivery.bootstrap_card(conn,rid,request['claim_token'],pid=os.getpid())
        second=delivery.bootstrap_card(conn,rid,request['claim_token'],pid=os.getpid())
        assert (first.id,first.current_run_id)==(second.id,second.current_run_id)
        assert first.worker_pid==os.getpid()
        assert first.created_by=='worker:default'
        with pytest.raises(delivery.OwnershipConflict):
            delivery.bootstrap_card(conn,rid,'different-worker',pid=os.getpid())
        assert conn.execute('select count(*) from tasks').fetchone()[0]==1
        assert conn.execute('select count(*) from task_runs').fetchone()[0]==1


def test_bootstrap_failure_rolls_back_card_event_and_forwarding(board):
    with kb.connect_closing(board) as conn:
        rid=receive(conn)
        request=delivery.reserve_request(conn,capacity=2)
        conn.execute("CREATE TRIGGER fail_bootstrap BEFORE INSERT ON nfos_workflows BEGIN SELECT RAISE(ABORT,'power-loss'); END")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError,match='power-loss'):
            delivery.bootstrap_card(conn,rid,request['claim_token'],pid=os.getpid())
        assert conn.execute('select count(*) from tasks').fetchone()[0]==0
        assert conn.execute('select count(*) from task_events').fetchone()[0]==0
        assert delivery.get_request(conn,rid)['task_id'] is None


def test_implementation_requires_persisted_spec_and_current_run(board):
    with kb.connect_closing(board) as conn:
        task=started(conn)
        with pytest.raises(delivery.WorkflowError,match='spec'):
            delivery.advance(conn,task.id,task.current_run_id,'implement',next_action='Write test')
        spec(conn,task)
        delivery.advance(conn,task.id,task.current_run_id,'implement',next_action='Write test')
        with pytest.raises(delivery.OwnershipConflict):
            delivery.advance(conn,task.id,task.current_run_id+1,'homolog',next_action='Verify')
    with kb.connect_closing(board) as conn:
        wf=delivery.get_workflow(conn,task.id)
        assert wf['stage']=='implement'
        assert wf['next_action']=='Write test'
        assert delivery.get_spec(conn,task.id)['author']=='Claude TL'


def test_pending_principal_decisions_survive_disconnect_and_keep_worker(board):
    with kb.connect_closing(board) as conn:
        task=started(conn);spec(conn,task)
        decision=delivery.ask_principal(conn,task.id,task.current_run_id,
            kind='impediment',question='Qual origem usar?',context={'tried':['Read config']})
    with kb.connect_closing(board) as conn:
        assert delivery.pending_decisions(conn)[0]['id']==decision
        delivery.resolve_decision(conn,decision,action='continue',answer='Use existing source',author='Principal')
        assert kb.get_task(conn,task.id).current_run_id==task.current_run_id
        assert delivery.get_decision(conn,decision)['status']=='resolved'


def test_lost_external_response_requires_destination_read_before_retry(board):
    with kb.connect_closing(board) as conn:
        task=started(conn);spec(conn,task)
        first=delivery.begin_effect(conn,task.id,task.current_run_id,
            operation='repair',target='audit:count',candidate='a'*40)
        assert first['execute'] is True
        again=delivery.begin_effect(conn,task.id,task.current_run_id,
            operation='repair',target='audit:count',candidate='a'*40)
        assert again['execute'] is False
        assert again['reconcile'] is True
        delivery.reconcile_effect(conn,first['id'],found=True,
            evidence={'readback':'Reconciled count 31','url':'https://example.test/audit/80'})
        final=delivery.begin_effect(conn,task.id,task.current_run_id,
            operation='repair',target='audit:count',candidate='a'*40)
        assert final['execute'] is False and final['reconcile'] is False


def test_done_requires_review_and_report_and_reports_need_no_pr(board):
    with kb.connect_closing(board) as conn:
        task=started(conn);spec(conn,task)
        assert kb.complete_task(conn,task.id,result='Completed') is False
        evidence=board.parent/'count.txt'
        evidence.write_text('Total reconciled: 31',encoding='utf-8')
        delivery.save_report(conn,task.id,task.current_run_id,
            {'criteria':[{'id':'AC1','status':'PASS','evidence':[str(evidence)]}],
             'summary':'Total reconciled','artifacts':[str(evidence)]})
        assert kb.complete_task(conn,task.id,result='Completed') is False
        decision=delivery.ask_principal(conn,task.id,task.current_run_id,
            kind='review',question='Review the report',context={})
        delivery.resolve_decision(conn,decision,action='approve',answer='Count and evidence checked',author='Principal')
        assert kb.complete_task(conn,task.id,result='Total reconciled') is False
        from tests.hermes_cli.test_nfos_principal_acceptance import assessment
        reviewed = assessment(evidence)
        reviewed['criteria'][0]['id'] = 'AC1'
        final = delivery.ask_principal(conn,task.id,task.current_run_id,
            kind='final_review',question='Inspect the actual finding',context={})
        delivery.resolve_decision(conn,final,action='continue',answer='Finding independently inspected',
            author='Principal',assessment=reviewed)
        assert kb.complete_task(conn,task.id,result='Total reconciled') is True


def test_readback_absence_allows_one_retry_and_confirmation_is_final(board):
    with kb.connect_closing(board) as conn:
        task=started(conn);spec(conn,task)
        args=dict(operation='repair',target='audit:count',candidate='b'*40)
        effect=delivery.begin_effect(conn,task.id,task.current_run_id,**args)
        delivery.reconcile_effect(conn,effect['id'],found=False,evidence={'readback':'No matching repair at destination'})
        assert delivery.begin_effect(conn,task.id,task.current_run_id,**args)['execute'] is True
        assert delivery.begin_effect(conn,task.id,task.current_run_id,**args)['execute'] is False
        delivery.reconcile_effect(conn,effect['id'],found=True,evidence={'readback':'Reconciled count 31','url':'https://example.test/audit/2'})
        with pytest.raises(delivery.WorkflowError):
            delivery.reconcile_effect(conn,effect['id'],found=False,evidence={'readback':'stale empty lookup'})


def test_project_homolog_and_publication_have_one_owner(board):
    with kb.connect_closing(board) as conn:
        first=started(conn);spec(conn,first)
        receive(conn,'43')
        request=delivery.reserve_request(conn,capacity=2)
        second=delivery.bootstrap_card(conn,request['id'],request['claim_token'],pid=os.getpid())
        spec(conn,second)
        assert delivery.acquire_project(conn,'pilot',first.id,first.current_run_id,'a'*40)
        assert not delivery.acquire_project(conn,'pilot',second.id,second.current_run_id,'b'*40)
        delivery.release_project(conn,'pilot',first.id,first.current_run_id)
        assert delivery.acquire_project(conn,'pilot',second.id,second.current_run_id,'b'*40)


def test_native_review_tool_routes_to_principal_without_ending_worker(board):
    with kb.connect_closing(board) as conn:
        task=started(conn);spec(conn,task)
        assert kb.request_review(conn,task.id,summary='Review the saved report',expected_run_id=task.current_run_id)
        assert kb.get_task(conn,task.id).current_run_id==task.current_run_id
        assert kb.get_task(conn,task.id).status=='running'
        assert delivery.pending_decisions(conn)[0]['kind']=='review'


def test_enrolled_code_uses_current_workflow_without_legacy_second_reviewer(board):
    with kb.connect_closing(board) as conn:
        rid=delivery.receive_request(conn,source={'platform':'telegram','chat_id':'1','thread_id':'2','message_id':'code'},
            text='Fix accepted behavior',project={'profile':'default','delivery_type':'code','repo_path':str(board.parent)})
        request=delivery.reserve_request(conn,capacity=2)
        task=delivery.bootstrap_card(conn,rid,request['claim_token'],pid=os.getpid())
        assert conn.execute('SELECT required FROM task_git_delivery WHERE task_id=?',(task.id,)).fetchone()[0]==0
    kb.init_db(board)
    with kb.connect_closing(board) as conn:
        assert conn.execute('SELECT required FROM task_git_delivery WHERE task_id=?',(task.id,)).fetchone()[0]==0
        assert kb.complete_task(conn,task.id,result='No evidence yet') is False


def test_native_block_asks_principal_before_releasing_the_worker(board):
    with kb.connect_closing(board) as conn:
        task=started(conn);spec(conn,task)
        assert kb.block_task(conn,task.id,reason='Need endpoint clarification',kind='needs_input',expected_run_id=task.current_run_id)
        current=kb.get_task(conn,task.id)
        assert current.status=='running'
        assert current.current_run_id==task.current_run_id
        decision=delivery.pending_decisions(conn)[0]
        assert decision['kind']=='impediment'
        delivery.resolve_decision(conn,decision['id'],action='continue',answer='Use project HML endpoint',author='Principal')
        assert kb.get_task(conn,task.id).current_run_id==task.current_run_id


def test_principal_human_decision_is_durable_until_worker_stops_and_answer_arrives(board,monkeypatch):
    with kb.connect_closing(board) as conn:
        task=started(conn);spec(conn,task)
        decision=delivery.ask_principal(conn,task.id,task.current_run_id,kind='impediment',question='Which business rule?',context={'saved':'checkpoint'})
        delivery.resolve_decision(conn,decision,action='human',answer='Maikol must choose A or B',author='Principal')
        assert kb.block_task(conn,task.id,reason='Maikol must choose A or B',kind='needs_input',expected_run_id=task.current_run_id)
        assert kb.get_task(conn,task.id).status=='blocked'
        assert delivery.get_decision(conn,decision)['status']=='human'
        monkeypatch.setattr(delivery,'_run_process_alive',lambda *args:False)
        from hermes_cli import nfos_runtime
        monkeypatch.setattr(nfos_runtime,'run_termination_pending',lambda *args:False)
        delivery.resume_after_answer(conn,task.id,answer='Use A',source={'platform':'telegram','message_id':'55'})
        assert kb.get_task(conn,task.id).status=='ready'
        assert delivery.get_decision(conn,decision)['status']=='resolved'
        assert delivery.get_spec(conn,task.id)['revision']==1


def test_bootstrap_claim_is_recognized_as_host_local_by_recovery(board):
    with kb.connect_closing(board) as conn:
        task=started(conn)
        assert task.claim_lock.startswith(kb._claimer_id().split(':',1)[0]+':')


def test_portal_reply_waits_for_principal_and_retries_without_duplicate(board, monkeypatch):
    from hermes_cli import nfos_runtime
    with kb.connect_closing(board) as conn:
        task = started(conn); spec(conn, task)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind='impediment', question='Which record?', context={})
        delivery.resolve_decision(conn, decision, action='human', answer='Internal diagnostic detail', author='Principal',
                                  public_message={'kind':'question','to':'solicitante','text':'Qual registro foi afetado?'})
        assert kb.block_task(conn, task.id, reason='Human question', kind='needs_input', expected_run_id=task.current_run_id)
        monkeypatch.setattr(delivery, '_run_process_alive', lambda *args:False)
        monkeypatch.setattr(nfos_runtime, 'run_termination_pending', lambda *args:False)
        monkeypatch.setattr(nfos_runtime, 'previous_runs_termination_pending', lambda *args:False)
        source = {'platform':'portal','actor':'Lucas','ticket':'DV-0011','message_id':'portal-note-7'}
        assert not delivery.resume_after_answer(conn, task.id, answer='Registro 2', source=source)
        assert kb.get_task(conn, task.id).status == 'blocked'
        assert delivery.get_decision(conn, decision)['status'] == 'pending'
        assert not kb.claim_task(conn, task.id, claimer='default')
        delivery.resolve_decision(conn, decision, action='continue', answer='Use the supplied record, retaining evidence.', author='Principal')
        assert kb.get_task(conn, task.id).status == 'ready'
        assert delivery.get_decision(conn, decision)['author'] == 'Principal'
        assert json.loads(delivery.get_decision(conn, decision)['context'])['human_reply']['author'] == 'Lucas'
        assert not delivery.resume_after_answer(conn, task.id, answer='Registro 2', source=source)
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='nfos_human_answer_received'", (task.id,)).fetchone()[0] == 1
        assert kb.get_task(conn, task.id).status == 'ready'


def test_legacy_portal_reply_requires_actual_principal_review(board, monkeypatch):
    from hermes_cli import nfos_runtime
    monkeypatch.setattr(delivery, '_run_process_alive', lambda *args:False)
    monkeypatch.setattr(nfos_runtime, 'run_termination_pending', lambda *args:False)
    monkeypatch.setattr(nfos_runtime, 'previous_runs_termination_pending', lambda *args:False)
    with kb.connect_closing(board) as conn:
        task = started(conn); spec(conn, task)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind='impediment', question='Which record?', context={})
        delivery.resolve_decision(conn, decision, action='human', answer='Ask the requester', author='Principal',
                                  public_message={'kind':'question','to':'solicitante','text':'Qual registro?'})
        delivery.reconcile_human_answers(conn)
        # Historical runtime stored the human's answer as the resolved decision.
        context = json.loads(delivery.get_decision(conn, decision)['context'])
        context['human_reply'] = {'answer':'Registro dois','author':'Lucas','received_at':12,
                                  'source':{'platform':'portal','actor':'Lucas','ticket':'DV-0011','message_id':'legacy-note-7'}}
        with kb.write_txn(conn):
            conn.execute("UPDATE nfos_decisions SET status='resolved',action='continue',author='Lucas',answer='Registro dois',resolved_at=12,context=? WHERE id=?",
                         (json.dumps(context), decision))
        delivery.reconcile_human_answers(conn)
        assert kb.get_task(conn, task.id).status == 'blocked'
        saved = delivery.get_decision(conn, decision)
        assert saved['status'] == 'pending' and saved['author'] is None
        assert json.loads(saved['context'])['legacy_human_resolution']['author'] == 'Lucas'
        delivery.reconcile_human_answers(conn)
        assert len(delivery.pending_decisions(conn)) == 1
        delivery.resolve_decision(conn, decision, action='continue', answer='Analyze record two', author='Principal')
        assert kb.get_task(conn, task.id).status == 'ready'


def test_delivery_public_message_releases_requester_without_ending_the_card(board):
    """STUDENT_DELIVERY_20261009: entrega ao solicitante é mensagem pública própria; o card segue para a causa."""
    with kb.connect_closing(board) as conn:
        task = started(conn); spec(conn, task)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind='impediment',
                                          question='Dado do aluno corrigido e conferido; entregar?', context={})
        with pytest.raises(delivery.WorkflowError):
            delivery.resolve_decision(conn, decision, action='human', answer='Ask', author='Principal',
                                      public_message={'kind': 'delivery', 'text': 'Seu plano foi corrigido.'})
        with pytest.raises(delivery.WorkflowError):
            delivery.resolve_decision(conn, decision, action='continue', answer='Deliver', author='Principal',
                                      public_message={'kind': 'release', 'text': 'Seu plano foi corrigido.'})
        delivery.resolve_decision(conn, decision, action='continue', answer='Entregar e seguir para a causa', author='Principal',
                                  public_message={'kind': 'delivery', 'text': 'Seu plano foi corrigido.', 'to': 'solicitante',
                                                  'internal': 'não passa'})
        saved = delivery.get_decision(conn, decision)
        public = json.loads(saved['context'])['public_message']
        assert saved['status'] == 'resolved' and saved['action'] == 'continue'
        assert {k: public[k] for k in ('kind', 'text')} == {'kind': 'delivery', 'text': 'Seu plano foi corrigido.'}
        assert 'internal' not in public and public['created_at'] > 0
        assert kb.get_task(conn, task.id).status != 'done'


def test_portal_followup_question_needs_a_new_reply(board, monkeypatch):
    from hermes_cli import nfos_runtime
    monkeypatch.setattr(delivery, '_run_process_alive', lambda *args:False)
    monkeypatch.setattr(nfos_runtime, 'run_termination_pending', lambda *args:False)
    monkeypatch.setattr(nfos_runtime, 'previous_runs_termination_pending', lambda *args:False)
    with kb.connect_closing(board) as conn:
        task = started(conn); spec(conn, task)
        decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind='impediment', question='Identify record', context={})
        delivery.resolve_decision(conn, decision, action='human', answer='Ask the requester', author='Principal',
                                  public_message={'kind':'question','to':'solicitante','text':'Qual registro?'})
        source = {'platform':'portal','actor':'Lucas','message_id':'first'}
        delivery.resume_after_answer(conn, task.id, answer='Registro dois', source=source)
        delivery.resolve_decision(conn, decision, action='human', answer='Need the preceding action', author='Principal',
                                  public_message={'kind':'question','to':'solicitante','text':'Qual foi a ação anterior?'})
        delivery.reconcile_human_answers(conn)
        saved = delivery.get_decision(conn, decision)
        assert saved['status'] == 'human' and kb.get_task(conn, task.id).status == 'blocked'
        context = json.loads(saved['context'])
        assert 'human_reply' not in context and context['human_reply_history'][0]['answer'] == 'Registro dois'
        assert context['public_message_history'][0]['text'] == 'Qual registro?'
        delivery.resume_after_answer(conn, task.id, answer='Cliquei em salvar', source={**source,'message_id':'second'})
        assert delivery.get_decision(conn, decision)['status'] == 'pending'
        delivery.resolve_decision(conn, decision, action='continue', answer='Reproduce save on record two', author='Principal')
        assert kb.get_task(conn, task.id).status == 'ready'


def test_portal_reopening_retains_attempt_and_requires_new_approval(board):
    with kb.connect_closing(board) as conn:
        task = started(conn); spec(conn, task)
        workflow = delivery.get_workflow(conn, task.id)
        request = delivery.get_request(conn, workflow['request_id'])
        payload = json.loads(request['payload'])
        payload['origin'] = {'portal':{'chamado':'DV-0011'}}
        payload['support_approval'] = {'required':True,'approved_at':1,'approved_by':'Maikol'}
        with kb.write_txn(conn):
            conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?', (json.dumps(payload), request['id']))
            conn.execute("UPDATE tasks SET status='done',completed_at=12,worker_pid=NULL,current_run_id=NULL WHERE id=?", (task.id,))
        source = {'platform':'portal','actor':'Lucas','message_id':'portal-note-8','ticket':'DV-0011'}
        assert not delivery.reopen_support_task(conn, task.id, text='Ainda falha no registro 2', source=source)['duplicate']
        reopened = kb.get_task(conn, task.id)
        assert reopened.status == 'todo' and reopened.current_run_id is None
        assert reopened.instruction_revision > task.instruction_revision
        assert 'Ainda falha' in reopened.body
        assert delivery.task_support_approval_pending(conn, task.id)
        assert not kb.claim_task(conn, task.id, claimer='default')
        assert delivery.get_spec(conn, task.id)['revision'] == 1
        assert delivery.reopen_support_task(conn, task.id, text='Ainda falha no registro 2', source=source)['duplicate']
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('closure_source,allowed', [('balcao:dov:DV-0011:resolve', True),
                                                  ('balcao:dov:DV-0011:cancel', False),
                                                  ('balcao:dov:DV-9999:resolve', False)])
def test_operator_resolution_reopens_only_the_current_ticket_receipt(board, monkeypatch, closure_source, allowed):
    from hermes_cli import nfos_runtime
    monkeypatch.setattr(nfos_runtime, 'previous_runs_termination_pending', lambda *args: False)
    with kb.connect_closing(board) as conn:
        task = started(conn)
        request = delivery.get_request(conn, delivery.get_workflow(conn, task.id)['request_id'])
        payload = json.loads(request['payload'])
        payload['origin'] = {'portal': {'chamado': 'DV-0011'}}
        payload['support_approval'] = {'required': True, 'approved_at': 1, 'approved_by': 'Maikol'}
        with kb.write_txn(conn):
            conn.execute('UPDATE nfos_requests SET payload=? WHERE id=?', (json.dumps(payload), request['id']))
        metadata = {'disposition': 'cancelled_by_owner', 'functional_delivery': False,
                    'reason': 'Resolvido no atendimento', 'author': 'Lucas', 'source': closure_source,
                    'authorization_message': 'Encerrar atendimento automático',
                    'expected_instruction_revision': task.instruction_revision}
        assert kb.complete_task(conn, task.id, metadata=metadata)
        source = {'platform': 'portal', 'actor': 'Lucas', 'message_id': 'portal-note-resolution', 'ticket': 'DV-0011'}
        if not allowed:
            with pytest.raises(delivery.WorkflowError, match='cancelled ticket'):
                delivery.reopen_support_task(conn, task.id, text='Ainda ocorre', source=source)
            assert kb.get_task(conn, task.id).status == 'done'
            return
        assert not delivery.reopen_support_task(conn, task.id, text='Ainda ocorre', source=source)['duplicate']
        reopened = kb.get_task(conn, task.id)
        assert reopened.status == 'todo' and reopened.title == task.title
        assert delivery.task_support_approval_pending(conn, task.id)
        assert not kb.claim_task(conn, task.id)
        assert conn.execute("SELECT count(*) FROM task_events WHERE kind='administrative_cancelled'").fetchone()[0] == 1
        assert 'administrative_closure' not in json.loads(delivery.get_workflow(conn, task.id)['state_json'])
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done',completed_at=456 WHERE id=?", (task.id,))
        with pytest.raises(ValueError, match='already closed'):
            kb.complete_task(conn, task.id, metadata=metadata)


def test_worker_recovers_explicit_existing_card_without_recreating_history(board):
    with kb.connect_closing(board) as conn:
        tid=kb.create_task(conn,title='Existing authorized report',body='Original request',assignee='default',
            created_by='Maikol',delivery_type='report',requires_repo=False)
        kb.add_comment(conn,tid,author='Maikol',body='Preserve these results')
        rid=delivery.receive_request(conn,source={'platform':'telegram','chat_id':'1','thread_id':'2','message_id':'55'},
            text='Resume this same delivery',project={'board':'pilot','profile':'default','delivery_type':'report','existing_task_id':tid})
        request=delivery.reserve_request(conn,capacity=2)
        task=delivery.bootstrap_card(conn,rid,request['claim_token'],pid=os.getpid())
        assert task.id==tid
        assert task.created_by=='Maikol'
        assert task.body.startswith('Original request')
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==1
        assert conn.execute('SELECT body FROM task_comments WHERE task_id=?',(tid,)).fetchone()[0]=='Preserve these results'


def test_native_wait_returns_the_principal_answer_without_another_model_turn(board):
    with kb.connect_closing(board) as conn:
        task=started(conn);spec(conn,task)
        decision=delivery.ask_principal(conn,task.id,task.current_run_id,kind='impediment',question='Resolve endpoint',context={})
        waiting=threading.Event()
        def wait():
            with kb.connect_closing(board) as reader:
                waiting.set()
                return delivery.wait_decision(reader,decision,timeout=3)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(wait)
            assert waiting.wait(3)
            delivery.resolve_decision(conn,decision,action='continue',answer='Use HML',author='Principal')
            assert future.result(timeout=5)['answer']=='Use HML'


def test_existing_report_card_can_materialize_its_real_code_workspace(board):
    repo=board.parent/'repo';repo.mkdir()
    def git(*args):
        return subprocess.run(['git','-C',str(repo),*args],stdin=subprocess.DEVNULL,check=True,capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    git('init','--initial-branch=main')
    git('-c','user.name=NFOS test','-c','user.email=nfos@example.test','commit','--allow-empty','-m','Fixture baseline')
    with kb.connect_closing(board) as conn:
        tid=kb.create_task(conn,title='Existing visual defect',assignee='default',workspace_kind='scratch',requires_repo=False,delivery_type='report')
        assert conn.execute('SELECT 1 FROM task_git_delivery WHERE task_id=?',(tid,)).fetchone() is None
        rid=delivery.receive_request(conn,source={'platform':'telegram','chat_id':'1','thread_id':'2','message_id':'adopt-code'},
            text='Deliver this existing correction',project={'profile':'default','delivery_type':'code','repo_path':str(repo),'existing_task_id':tid})
        request=delivery.reserve_request(conn,capacity=2)
        task=delivery.bootstrap_card(conn,rid,request['claim_token'],pid=os.getpid())
        workspace,branch=kb._resolve_worktree_workspace(task,board='pilot',conn=conn)
        assert workspace.is_dir()
        assert kb._validate_worktree_ownership(conn,tid,require_checkout=True)[0]
        assert conn.execute('SELECT required FROM task_git_delivery WHERE task_id=?',(tid,)).fetchone()[0]==0
        assert kb.get_task(conn,tid).branch_name==branch
