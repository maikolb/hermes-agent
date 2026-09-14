"""HUMAN_LAST_RESORT_20260914: no chat do cliente, PERGUNTA só vai ao grupo quando é para quem faz pedidos naquele quadro; pergunta ao
dono, à manutenção ou a destinatário desconhecido fica no card, e a classificação falha fechada."""
from types import SimpleNamespace

import pytest

from gateway import kanban_watchers as kw
from gateway import run as gr

WAKE = ("[kanban] Task t_b464e591 blocked; needs attention.\nTitle: Registrar a validade do plano\n"
        "Assignee: @hermes-project-factory\nBoard: infotributos-board\n\nCheck the result or decide the next step.")


def _source(chat_id="-1004309874643", thread_id="3234"):
    return SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id=chat_id, thread_id=thread_id)


@pytest.fixture
def client_chat(monkeypatch):
    recorded = []
    monkeypatch.setattr(kw, "_client_source_for_board",
                        lambda board: ({"platform": "telegram", "chat_id": "-1004309874643", "thread_id": "3234"}, True))
    monkeypatch.setattr(kw, "_record_client_suppression", lambda board, task_id, kind, sub: recorded.append((board, task_id, kind)))
    monkeypatch.setattr(gr, "_client_chat_askable_names", lambda board: {"jhonatan"})
    return recorded


def test_question_to_the_requester_goes_out_and_the_rest_stays_on_the_card(client_chat):
    suppressed = [
        "**PERGUNTA para Maikol:** pode configurar o probe_env do Infotributos?",
        "- PERGUNTA para Maikol: pode configurar?",
        "1. PERGUNTA para Maikol: pode configurar?",
        "2) **PERGUNTA para Maikol**: pode configurar?",
        "❓ PERGUNTA ao Maikol: pode configurar?",
        "PERGUNTA para Fulano: quem é?",
        "Pronto: validade registrada.\nPERGUNTA para Maikol: posso fechar?",
    ]
    for text in suppressed:
        assert gr._wake_owner_question_in_client_chat(WAKE, text, _source()) is True, text
    assert client_chat[0] == ("infotributos-board", "t_b464e591", "owner_question")
    assert gr._wake_owner_question_in_client_chat(WAKE, "PERGUNTA para Jhonatan: pode religar a VPS do TEST?", _source()) is False
    assert gr._wake_owner_question_in_client_chat(WAKE, "Pronto: validade registrada no TEST.", _source()) is False


def test_operator_chat_and_non_wake_turns_are_untouched(client_chat):
    assert gr._wake_owner_question_in_client_chat(WAKE, "PERGUNTA para Maikol: autoriza?", _source(chat_id="996979567", thread_id=None)) is False
    assert gr._wake_owner_question_in_client_chat("[Jhonatan|7550030839] @omaikol", "PERGUNTA para Maikol: x?", _source()) is False
    assert client_chat == []


def test_classification_failure_in_the_client_chat_suppresses(client_chat, monkeypatch):
    def boom(board):
        raise RuntimeError("banco indisponível")

    monkeypatch.setattr(gr, "_client_chat_askable_names", boom)
    assert gr._wake_owner_question_in_client_chat(WAKE, "PERGUNTA para Jhonatan: pode religar?", _source()) is True


def test_askable_names_come_from_requests_without_internal_names(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import nfos_delivery as delivery
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "names.db"))
    kb.init_db()
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
        for n, author in enumerate(("[Jhonatan|7550030839]", "[Maikol|996979567]")):
            delivery.receive_request(conn, source={"platform": "telegram", "chat_id": "-100", "thread_id": "3234", "message_id": str(n)},
                                     text=f"{author} registrar a validade do plano",
                                     project={"board": "infotributos-board", "profile": "default", "delivery_type": "report"}, attachments=[])
    assert gr._client_chat_askable_names(None) == {"jhonatan"}


def test_wake_rule_says_where_questions_go():
    assert "nunca vai ao grupo do cliente" in kw.WAKE_GROUP_RULE
    assert "quem faz pedidos neste grupo" in kw.WAKE_GROUP_RULE
    assert "NÃO acrescente [SILENT]" in kw.WAKE_GROUP_RULE
