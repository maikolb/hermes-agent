"""RECORD_MODE_TEXT_20260911: instruções do worker coerentes com o modo registro; premissa 0 por tipo de card; respostas
automáticas neutras quanto à rota."""
import pytest

from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_runtime as runtime


def test_worker_instructions_in_record_mode_have_no_conducted_protocol(monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    text = runtime.worker_instructions()
    assert text.startswith("OWNER PREMISES")
    assert "record mode" in text
    for old in ("spec_review", "final_review", "preparation review", "acquire-project --wait 900",
                "Homologation precedes Principal publication review", "ask --kind homologation", "Wait for the Principal's decision"):
        assert old not in text, old
    assert "NFOS closeout policy applies to every project" in text
    assert "partial_delivery=true" in text


def test_worker_instructions_outside_record_mode_keep_the_conducted_protocol(monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": True})
    text = runtime.worker_instructions()
    assert text.startswith("Exact workflow CLI prefix")
    assert "kind=spec_review" in text
    assert "NFOS closeout policy applies to every project" in text


def test_premise_zero_distinguishes_operation_and_report():
    zero = [p for p in runtime.owner_premises() if p.startswith("0.")][0]
    assert "for a code request" in zero and "operation or report request read only the target" in zero


def test_auto_answers_do_not_force_main_or_skip_staging():
    assert "PR em main" not in delivery._CODE_ROUTE_PREPARATION
    assert "só se o corpo do card pedir" not in delivery._CODE_ROUTE_PREPARATION
    answer = delivery._auto_continue_answer("impediment", "The canonical staging slot is occupied by another task")
    assert "acquire-project` só registra" in answer or "só registra quem publica" in answer
