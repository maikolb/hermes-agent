"""HUMAN_LAST_RESORT_20260914: pergunta ao dono ou à manutenção escrita num wake de kanban nunca vai ao chat do cliente."""
from types import SimpleNamespace

from gateway import kanban_watchers as kw
from gateway.run import _wake_owner_question_in_client_chat

WAKE = ("[kanban] Task t_b464e591 blocked; needs attention.\nTitle: Registrar a validade do plano\n"
        "Assignee: @hermes-project-factory\nBoard: infotributos-board\n\nCheck the result or decide the next step.")


def _source(chat_id="-1004309874643", thread_id="3234"):
    return SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id=chat_id, thread_id=thread_id)


def test_owner_question_stays_out_of_the_client_chat(monkeypatch):
    recorded = []
    monkeypatch.setattr(kw, "_client_source_for_board",
                        lambda board: ({"platform": "telegram", "chat_id": "-1004309874643", "thread_id": "3234"}, True))
    monkeypatch.setattr(kw, "_record_client_suppression", lambda board, task_id, kind, sub: recorded.append((board, task_id, kind)))
    assert _wake_owner_question_in_client_chat(WAKE, "**PERGUNTA para Maikol:** pode configurar o probe_env do Infotributos?", _source()) is True
    assert recorded == [("infotributos-board", "t_b464e591", "owner_question")]
    assert _wake_owner_question_in_client_chat(WAKE, "PERGUNTA para Jhonatan: qual plano conta como pago?", _source()) is False
    assert _wake_owner_question_in_client_chat(WAKE, "Pronto: validade registrada no TEST.", _source()) is False
    assert _wake_owner_question_in_client_chat(WAKE, "PERGUNTA para Maikol: autoriza?", _source(chat_id="996979567", thread_id=None)) is False
    assert _wake_owner_question_in_client_chat("[Jhonatan|7550030839] @omaikol", "PERGUNTA para Maikol: x?", _source()) is False
    assert len(recorded) == 1


def test_wake_rule_says_where_owner_questions_go():
    assert "nunca vai ao grupo do cliente" in kw.WAKE_GROUP_RULE
    assert "NÃO acrescente [SILENT]" in kw.WAKE_GROUP_RULE
