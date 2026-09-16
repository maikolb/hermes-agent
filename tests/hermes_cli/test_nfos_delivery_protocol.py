"""Disabled instructions must not return through the worker prompt."""
from hermes_cli.worker_protocol import dispatcher_worker_protocol
import pytest
from tests.hermes_cli.test_nfos_delivery_environment import board, _code_card, kb, delivery, runtime


def test_worker_protocol_does_not_inject_retired_framework():
    protocol = dispatcher_worker_protocol()
    assert 'AOF' not in protocol
    assert 'Agent Operating Framework' not in protocol


@pytest.mark.parametrize('record_mode,result_review', [(False, False), (True, False), (True, True)])
def test_nfos_worker_saves_own_spec_and_spawn_does_not_require_claude(board, monkeypatch, record_mode, result_review):
    from types import SimpleNamespace
    captured = {}
    monkeypatch.setattr(runtime, '_record_mode', lambda: record_mode)
    monkeypatch.setattr(delivery, '_result_review', lambda: result_review)
    def capture(cmd, **kwargs):
        captured['command'] = cmd
        return SimpleNamespace(pid=4321)
    monkeypatch.setattr(kb.subprocess, 'Popen', capture)
    with kb.connect_closing() as conn:
        task = _code_card(conn, board, 'Executar o pedido com a spec existente.')
        spec = delivery.get_spec(conn, task.id)
        assert spec['author'] == 'worker' and spec['revision'] == 1
    kb._default_spawn(task, str(board))
    prompt = ' '.join(str(part) for part in captured['command'])
    assert 'ask Claude TL' not in prompt
    assert 'Ask Claude TL' not in prompt
    assert 'Have TL verify' not in prompt
    assert 'Plan and execute with the selected worker model' in prompt
    assert '--author worker' in prompt
    assert 'Reuse the current spec and checkpoint' in prompt
    with kb.connect_closing() as conn:
        assert delivery.get_spec(conn, task.id) == spec
