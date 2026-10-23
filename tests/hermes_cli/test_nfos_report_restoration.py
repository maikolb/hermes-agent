"""RESTORATION_INHERIT_20261017: a porta de reparo do Concursa grava, no ``op --mode aplicar``, o relatório com o bloco
``restoration`` (a prova da releitura em produção). O relatório final do worker (o do ``public_delivery``) é uma revisão nova:
sem o bloco, o fato ``restoration_proven`` do fechamento (que lê só a última revisão) cairia e o worker teria de copiar a prova à
mão. Então ``save_report`` herda o ``restoration`` da revisão anterior quando a nova não traz o seu; quem traz o próprio bloco
manda, e sem revisão anterior nada é inventado."""
import json

import pytest

from hermes_cli import nfos_delivery as d, nfos_principal_review as review
from tests.hermes_cli.test_nfos_principal_acceptance import task_context, accept  # noqa: F401
from tests.hermes_cli.nfos_owner_question_switch import owner_questions_allowed  # noqa: F401

pytestmark = pytest.mark.usefixtures('owner_questions_allowed')

RESTORATION = {'query': ['select position_id, status from study_plans where id = :plan'],
               'before': {'move_plan_position:p1': {'position_id': 'cargo-antigo'}},
               'after': {'move_plan_position:p1': {'position_id': 'cargo-novo'}}, 'pending': [], 'evidence_gaps': [], 'readback': []}


def report(artifact, **extra):
    return {'summary': 'Reparo aplicado pela porta', 'criteria': [{'id': 'C1', 'status': 'PASS', 'evidence': ['readback']}],
            'artifacts': [{'id': 'readback', 'path': str(artifact)}], **extra}


def prepare(ctx, monkeypatch):
    conn, task, spec, artifact = ctx
    monkeypatch.setattr(review, 'settings', lambda: {'principal_validation': False, 'result_review': True})
    accept(conn, task, 'spec_review')
    return conn, task, artifact


def latest(conn, task_id):
    row = d._artifact(conn, task_id, 'report')
    return row['revision'], json.loads(row['content']), json.loads(row['evidence'])


def test_the_final_report_inherits_the_restoration_the_repair_port_recorded(task_context, monkeypatch):
    conn, task, artifact = prepare(task_context, monkeypatch)
    d.save_report(conn, task.id, task.current_run_id, report(artifact, restoration=RESTORATION))
    d.save_report(conn, task.id, task.current_run_id, report(artifact, summary='Entrega ao aluno registrada'))
    revision, content, evidence = latest(conn, task.id)
    assert revision == 2 and content['summary'] == 'Entrega ao aluno registrada'
    assert content['restoration'] == RESTORATION
    # a herança entra antes do hash: o conteúdo salvo é o que a evidência assina
    assert evidence['report_sha256'] == d.hashlib.sha256(d._artifact(conn, task.id, 'report')['content'].encode()).hexdigest()


def test_a_support_reopening_ends_the_inheritance(task_context, monkeypatch):
    """ALTO da revisora: a prova vale para o episódio atual (EPISODE_FACTS_20261010, enforce.card_facts lê ``support_reopened``).
    Depois da reabertura pelo suporte, o relatório sem prova não herda a da rodada anterior; a prova nova volta a valer."""
    conn, task, artifact = prepare(task_context, monkeypatch)
    clock = iter(range(1_800_000_000, 1_800_000_100))  # cada gravação num segundo próprio: a reabertura fica entre r1 e r2
    monkeypatch.setattr(d.time, 'time', lambda: next(clock))
    d.save_report(conn, task.id, task.current_run_id, report(artifact, restoration=RESTORATION))
    conn.execute("INSERT INTO task_events(task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'support_reopened', '{}', ?)",
                 (task.id, task.current_run_id, next(clock)))
    conn.commit()
    d.save_report(conn, task.id, task.current_run_id, report(artifact, summary='Rodada nova sem prova'))
    assert 'restoration' not in latest(conn, task.id)[1]
    again = dict(RESTORATION, after={'move_plan_position:p1': {'position_id': 'cargo-novo-2'}})
    d.save_report(conn, task.id, task.current_run_id, report(artifact, restoration=again))
    d.save_report(conn, task.id, task.current_run_id, report(artifact, summary='Entrega da rodada nova'))
    revision, content, _ = latest(conn, task.id)
    assert revision == 4 and content['restoration'] == again  # a prova do episódio atual herda


def test_a_report_that_brings_its_own_restoration_keeps_it(task_context, monkeypatch):
    conn, task, artifact = prepare(task_context, monkeypatch)
    d.save_report(conn, task.id, task.current_run_id, report(artifact, restoration=RESTORATION))
    partial = dict(RESTORATION, pending=['rebuild_schedule'])
    d.save_report(conn, task.id, task.current_run_id, report(artifact, restoration=partial))
    assert latest(conn, task.id)[1]['restoration'] == partial


def test_without_an_earlier_restoration_nothing_is_invented(task_context, monkeypatch):
    conn, task, artifact = prepare(task_context, monkeypatch)
    d.save_report(conn, task.id, task.current_run_id, report(artifact))
    d.save_report(conn, task.id, task.current_run_id, report(artifact, summary='Segunda revisão sem prova'))
    revision, content, _ = latest(conn, task.id)
    assert revision == 2 and 'restoration' not in content
