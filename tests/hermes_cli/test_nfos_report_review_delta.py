import json
import pytest
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, accept, save_report
from hermes_cli import nfos_delivery as d, nfos_principal_review as review


def test_summary_edge_whitespace_preserves_valid_acceptance(task_context):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review'); save_report(conn, task, artifact)
    prior = accept(conn, task, 'final_review', artifact)
    report = json.loads(d._artifact(conn,task.id,'report')['content'])
    report['summary'] = '\n ' + report['summary'] + ' \n'
    d.save_report(conn, task.id, task.current_run_id, report)
    assert review.accepted(conn, task.id, 'final_review')
    latest = conn.execute("SELECT context FROM nfos_decisions WHERE task_id=? AND kind='final_review' ORDER BY rowid DESC LIMIT 1", (task.id,)).fetchone()
    assert json.loads(latest['context'])['report_delta']['previous_decision_id'] == prior


def test_changed_bytes_remain_pending_with_explicit_delta(task_context):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review'); save_report(conn, task, artifact)
    prior = accept(conn, task, 'final_review', artifact)
    artifact.write_text('count=32\n')
    save_report(conn, task, artifact)
    assert not review.accepted(conn, task.id, 'final_review')
    latest = conn.execute("SELECT context FROM nfos_decisions WHERE task_id=? AND kind='final_review' ORDER BY rowid DESC LIMIT 1", (task.id,)).fetchone()
    delta = json.loads(latest['context'])['report_delta']
    assert delta['previous_decision_id'] == prior
    assert delta['changed_artifacts'] == [str(artifact)]
    assert delta['affected_criteria'] == ['C1']


@pytest.mark.parametrize('field', ['summary','criteria','deployed_at'])
def test_substantive_report_fields_do_not_inherit_acceptance(task_context, field):
    conn, task, _, artifact = task_context
    accept(conn, task, 'spec_review'); save_report(conn, task, artifact)
    accept(conn, task, 'final_review', artifact)
    report = json.loads(d._artifact(conn,task.id,'report')['content'])
    if field == 'criteria':
        report[field][0]['observation'] = 'A different claim'
    else:
        report[field] = 'Changed value'
    d.save_report(conn,task.id,task.current_run_id,report)
    assert not review.accepted(conn,task.id,'final_review')
