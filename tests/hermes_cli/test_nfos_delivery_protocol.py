"""Disabled instructions must not return through the worker prompt."""
from hermes_cli.worker_protocol import dispatcher_worker_protocol


def test_worker_protocol_does_not_inject_retired_framework():
    protocol = dispatcher_worker_protocol()
    assert 'AOF' not in protocol
    assert 'Agent Operating Framework' not in protocol
