"""A premature model final must lead to actual work in the same bounded turn."""
import json
import pytest
from tests.agent.test_empty_tool_name_loop_dampening import agent_env, _text_resp, _tc_resp


@pytest.mark.parametrize('repair_allowed', [True,False])
def test_principal_final_continues_to_execution_without_new_turn_budget(agent_env,tmp_path,monkeypatch,repair_allowed):
    agent, provider = agent_env
    from hermes_cli import kanban_db as kb,nfos_delivery as delivery,nfos_runtime as runtime
    from gateway.wake import current_notify_receipt
    from tools.registry import registry
    from hermes_state import SessionDB
    agent.session_id='principal-fixture'
    agent._session_db=SessionDB(tmp_path/'state.db')
    agent._session_db.create_session(session_id=agent.session_id,source='cli',model='test-model')
    db=tmp_path/'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB',str(db))
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    with kb.connect_closing(db) as conn:
        tid=kb.create_task(conn,title='Repair test fixture',assignee='default',delivery_type='report',requires_repo=False)
        kb.block_task(conn,tid,reason='Fixture needs administrative preparation',kind='transient')
        runtime.adopt_existing_tasks(conn,board='default',project={'profile':'default','delivery_type':'report'})
        did=delivery.pending_decisions(conn)[0]['id']
    receipt_file=tmp_path/'repair-receipt.txt'
    def repair_fixture(args,**kwargs):
        receipt_file.write_text('Actual fixture operation completed',encoding='utf-8')
        with kb.connect_closing(db) as conn:
            delivery.resolve_decision(conn,did,action='continue',answer='Read back fixture receipt',author='Principal')
        return json.dumps({'repaired':True,'receipt':str(receipt_file)})
    schema={'name':'repair_test_fixture','description':'Repair the isolated test fixture',
            'parameters':{'type':'object','properties':{}}}
    registry.register('repair_test_fixture','test_recovery',schema,repair_fixture)
    agent.valid_tool_names.add('repair_test_fixture')
    agent.max_iterations=4
    provider.response_queue.clear();provider.captured_requests.clear()
    provider.response_queue.append(_text_resp('The operation was busy; I will retry later.'))
    if repair_allowed:
        provider.response_queue.extend([_tc_resp('repair_test_fixture'),_text_resp('Repaired and read back.')])
    token=current_notify_receipt.set({'db_path':str(db),'principal_task_id':tid,'delivery_id':'isolated-recovery'})
    try:
        result=agent.run_conversation('Continue the authorized fixture repair',conversation_history=[],task_id='same-turn')
    finally:
        current_notify_receipt.reset(token)
        registry._tools.pop('repair_test_fixture',None)
    with kb.connect_closing(db) as conn:
        if repair_allowed:
            assert receipt_file.read_text()=='Actual fixture operation completed'
            assert delivery.get_decision(conn,did)['status']=='resolved'
            assert kb.get_task(conn,tid).status=='ready'
            assert result['api_calls']==3
        else:
            assert not receipt_file.exists()
            assert delivery.get_decision(conn,did)['status']=='pending'
            assert result['api_calls']==4
    assert agent.max_iterations==4


def test_principal_turn_ends_while_only_the_lab_queue_holds_its_card(agent_env,tmp_path,monkeypatch):
    """PRINCIPAL_TURN_BOUND_20261009: reproduz o turno preso de 09/10 (t_8ed13ed7, espera cs0016-principal-recover1430)."""
    import os
    from pathlib import Path
    agent, provider = agent_env
    from hermes_cli import kanban_db as kb,nfos_delivery as delivery,nfos_principal_review as review,nfos_runtime as runtime
    from gateway.wake import current_notify_receipt
    from hermes_state import SessionDB
    agent.session_id='principal-lab-wait'
    agent._session_db=SessionDB(tmp_path/'state.db')
    agent._session_db.create_session(session_id=agent.session_id,source='cli',model='test-model')
    db=tmp_path/'kanban.db'
    monkeypatch.setenv('HERMES_KANBAN_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(db))
    monkeypatch.delenv('HERMES_KANBAN_TASK',raising=False)
    monkeypatch.setattr(Path,'home',lambda: tmp_path)
    monkeypatch.setattr(review,'settings',lambda: {'principal_validation':False})
    monkeypatch.setattr(runtime,'project_config',lambda board,config=None: {'enabled':True,'board':board,'project_id':'concursa-ai'})
    monkeypatch.setattr(delivery,'_lab_receipt',lambda receipt,timeout=90: {'status':'queued','reason':'capacity'})
    with kb.connect_closing(db) as conn:
        delivery.init_schema(conn)
        rid=delivery.receive_request(conn,source={'platform':'telegram','chat_id':'-10001','thread_id':'41','message_id':'901'},
                                     text='Erro ao enviar o edital.',project={'board':'concursa-ai','profile':'default','delivery_type':'code'},
                                     attachments=[])
        request=delivery.reserve_request(conn,capacity=16)
        task=delivery.bootstrap_card(conn,rid,request['claim_token'],pid=os.getpid())
        wait=delivery.lab_wait(conn,task.id,kb.get_task(conn,task.id).current_run_id,'cs0016-principal-recover1430')
    agent.max_iterations=4
    provider.response_queue.clear();provider.captured_requests.clear()
    provider.response_queue.append(_text_resp('O pedido do laboratório está na fila; o runtime responde a espera.'))
    token=current_notify_receipt.set({'db_path':str(db),'principal_task_id':task.id,'delivery_id':'lab-wait-wake'})
    try:
        result=agent.run_conversation('Revise o card t_8ed13ed7',conversation_history=[],task_id='lab-wait-turn')
    finally:
        current_notify_receipt.reset(token)
    assert result['api_calls']==1, 'a espera do laboratório não segura o turno do Principal'
    with kb.connect_closing(db) as conn:
        assert delivery.get_decision(conn,wait['decision_id'])['status']=='pending'
