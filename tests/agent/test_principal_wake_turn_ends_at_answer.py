"""PRINCIPAL_TURN_ENDS_AT_ANSWER_20261013: a card wake's turn ends at the recorded answer.

On 13/10 the Principal's decision wakes paid 97 of 553 calls after the answer was
recorded, most of them one closing text with the whole context. The provider
double counts every call; the decide tool records when the answer landed.
"""
import json

from tests.agent.test_empty_tool_name_loop_dampening import agent_env, _text_resp, _tc_resp  # noqa: F401

DECIDE = 'decide_test_fixture'


def _card(agent, tmp_path, monkeypatch, decisions=1):
    from hermes_cli import kanban_db as kb, nfos_delivery as delivery, nfos_runtime as runtime
    from hermes_state import SessionDB
    agent.session_id = 'principal-answer'
    agent._session_db = SessionDB(tmp_path/'state.db')
    agent._session_db.create_session(session_id=agent.session_id, source='cli', model='test-model')
    db = tmp_path/'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(db))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    with kb.connect_closing(db) as conn:
        tid = kb.create_task(conn, title='Decision fixture', assignee='default', delivery_type='report', requires_repo=False)
        kb.block_task(conn, tid, reason='Fixture needs a Principal decision', kind='transient')
        runtime.adopt_existing_tasks(conn, board='default', project={'profile': 'default', 'delivery_type': 'report'})
        dids = [delivery.pending_decisions(conn)[0]['id']]
        for n in range(1, decisions):
            dids.append(dids[0]+'_%d' % n)
            conn.execute("INSERT INTO nfos_decisions(id,task_id,run_id,kind,question,context,spec_revision,created_at) "
                         "SELECT ?,task_id,run_id,kind,question,'{}',spec_revision,created_at FROM nfos_decisions WHERE id=?",
                         (dids[-1], dids[0]))
        conn.commit()
    return db, tid, dids


def _run(agent, provider, db, tid, dids, responses, *, wake=True, message='[kanban] worker aguardando sua decisão'):
    """Run one turn; return the result and the call count seen at each recorded answer."""
    from hermes_cli import kanban_db as kb, nfos_delivery as delivery
    from gateway.wake import current_notify_receipt
    from tools.registry import registry
    answered = []

    def decide(args, **kwargs):
        with kb.connect_closing(db) as conn:
            delivery.resolve_decision(conn, dids[len(answered)], action='continue', answer='Proceed', author='Principal')
        answered.append(len(provider.captured_requests))
        return json.dumps({'resolved': True})

    registry.register(DECIDE, 'test_principal_answer', {'name': DECIDE, 'description': 'Resolve the fixture decision',
                                                        'parameters': {'type': 'object', 'properties': {}}}, decide)
    agent.valid_tool_names.add(DECIDE)
    agent.max_iterations = 10
    provider.response_queue.clear()
    provider.captured_requests.clear()
    provider.response_queue.extend(responses)
    token = current_notify_receipt.set({'db_path': str(db), 'principal_task_id': tid, 'delivery_id': 'answer-wake'}
                                       if wake else None)
    try:
        result = agent.run_conversation(message, conversation_history=[], task_id='answer-turn')
    finally:
        current_notify_receipt.reset(token)
        registry._tools.pop(DECIDE, None)
    return result, answered


def _pending(db, tid):
    from hermes_cli import kanban_db as kb
    with kb.connect_closing(db) as conn:
        return conn.execute("SELECT COUNT(*) FROM nfos_decisions WHERE task_id=? AND status='pending'", (tid,)).fetchone()[0]


def test_a_wake_turn_ends_once_the_answer_leaves_the_card_without_pending_decisions(agent_env, tmp_path, monkeypatch,
                                                                                     record_property):
    agent, provider = agent_env
    db, tid, dids = _card(agent, tmp_path, monkeypatch)
    result, answered = _run(agent, provider, db, tid, dids, [_tc_resp(DECIDE), _text_resp('Decidido: o worker segue.')])
    after = result['api_calls'] - answered[-1]
    record_property('api_calls', result['api_calls'])
    record_property('calls_after_answer', after)
    assert _pending(db, tid) == 0
    assert after == 0, 'no model call follows the recorded answer of the woken card'
    assert result['api_calls'] == 1
    assert result['final_response'] == '[SILENT]'
    assert result['turn_exit_reason'] == 'nfos_principal_wake_answered'
    tail = result['messages'][-1]
    assert (tail['role'], tail['content']) == ('assistant', '[SILENT]'), 'the transcript closes the turn as silence'


def test_b_wake_turn_continues_while_the_card_keeps_another_pending_decision(agent_env, tmp_path, monkeypatch,
                                                                             record_property):
    agent, provider = agent_env
    db, tid, dids = _card(agent, tmp_path, monkeypatch, decisions=2)
    result, answered = _run(agent, provider, db, tid, dids, [_tc_resp(DECIDE)] + [_text_resp('Sigo com a outra depois.')]*9)
    after = result['api_calls'] - answered[-1]
    record_property('api_calls', result['api_calls'])
    record_property('calls_after_answer', after)
    assert _pending(db, tid) == 1
    # Unchanged: the reply is nudged back to the pending decision until the idle bound releases it.
    from agent import kanban_stop
    assert after == 1+kanban_stop._PRINCIPAL_MAX_IDLE_NUDGES
    assert result['api_calls'] == 2+kanban_stop._PRINCIPAL_MAX_IDLE_NUDGES


def test_b2_wake_turn_continues_to_the_next_decision_and_ends_at_the_last_answer(agent_env, tmp_path, monkeypatch,
                                                                                 record_property):
    agent, provider = agent_env
    db, tid, dids = _card(agent, tmp_path, monkeypatch, decisions=2)
    result, answered = _run(agent, provider, db, tid, dids, [_tc_resp(DECIDE), _tc_resp(DECIDE), _text_resp('Decidido.')])
    record_property('api_calls', result['api_calls'])
    record_property('calls_after_first_answer', result['api_calls'] - answered[0])
    record_property('calls_after_answer', result['api_calls'] - answered[-1])
    assert len(answered) == 2, 'the turn went on to the second pending decision'
    assert result['api_calls'] - answered[0] == 1
    assert result['api_calls'] - answered[-1] == 0
    assert _pending(db, tid) == 0


def test_c_turn_opened_by_a_person_is_unchanged(agent_env, tmp_path, monkeypatch, record_property):
    agent, provider = agent_env
    db, tid, dids = _card(agent, tmp_path, monkeypatch)
    result, answered = _run(agent, provider, db, tid, dids, [_tc_resp(DECIDE), _text_resp('Decidido.')],
                            wake=False, message='Decida o card do fixture')
    after = result['api_calls'] - answered[-1]
    record_property('api_calls', result['api_calls'])
    record_property('calls_after_answer', after)
    assert _pending(db, tid) == 0
    assert after == 1
    assert result['final_response'] == 'Decidido.'


def test_d_wake_reply_without_recording_the_decision_is_unchanged(agent_env, tmp_path, monkeypatch, record_property):
    agent, provider = agent_env
    db, tid, dids = _card(agent, tmp_path, monkeypatch)
    result, answered = _run(agent, provider, db, tid, dids, [_text_resp('Vou verificar o worker e decido depois.')]*10)
    from agent import kanban_stop
    record_property('api_calls', result['api_calls'])
    assert answered == []
    assert _pending(db, tid) == 1
    assert result['api_calls'] == 1+kanban_stop._PRINCIPAL_MAX_IDLE_NUDGES
