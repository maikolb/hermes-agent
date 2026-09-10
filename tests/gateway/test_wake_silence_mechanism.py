"""WAKE_SILENCE_MECH_20260910: em turno de wake de kanban, só Entregue ou pergunta a humano vão ao grupo."""
from gateway.run import _wake_narration_to_suppress

WAKE = "[kanban] Task t_74beb26e worker aguardando sua decisão. Title: Corrigir em produção..."


def test_narration_on_a_wake_turn_is_suppressed():
    assert _wake_narration_to_suppress(WAKE, "**Resolvi as quatro decisões pendentes como `continue`, no mesmo card.** O bloqueio observado foi HTTP 403.")


def test_delivery_and_human_question_pass_on_a_wake_turn():
    assert not _wake_narration_to_suppress(WAKE, "Entregue: t_74beb26e fechado em produção, readback ok.")
    assert not _wake_narration_to_suppress(WAKE, "**Entregue** t_3824f6dd, 10/10 tenants.")
    assert not _wake_narration_to_suppress(WAKE, "PERGUNTA para Maikol: qual DATABASE_URL o worker deve usar?")
    assert not _wake_narration_to_suppress(WAKE, "Reprovei o candidato. Você quer que eu reabra o card com o escopo antigo?")


def test_non_wake_turns_are_never_touched():
    assert not _wake_narration_to_suppress("[Maikol|996979567] e aí, como está o card?", "Resolvi as decisões pendentes.")
    assert not _wake_narration_to_suppress(None, "Resolvi as decisões pendentes.")
    assert not _wake_narration_to_suppress(WAKE, "")
