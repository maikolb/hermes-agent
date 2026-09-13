"""CLIENT_CHAT_20260913: marcador de silêncio não vaza (bordas), wake alinhado com o filtro, chat do cliente sem publicação
passiva (recibo e wake seguem), título público sem carimbo nem telefone, barra honesta."""
import asyncio
import json

import pytest

from gateway import kanban_watchers as kw
from gateway.response_filters import is_intentional_silence_response, strip_silence_markers
from gateway.run import _wake_narration_to_suppress
from gateway.stream_consumer import _strip_tail_silence_marker
from hermes_cli import kanban_db as kb
from hermes_cli.nfos_delivery import public_request_title, request_card_title

WAKE = "[kanban] Task t_1 completed.\nTitle: x"


def test_strip_markers_only_at_the_edges_and_after_the_silence_decision():
    assert is_intentional_silence_response("[SILENT]") is True
    assert is_intentional_silence_response("Entregue: x\n\n[SILENT]") is False
    assert strip_silence_markers("Entregue: cargo corrigido.\n\n[SILENT]") == "Entregue: cargo corrigido."
    assert strip_silence_markers("Entregue: cargo corrigido. [SILENT]") == "Entregue: cargo corrigido."
    assert strip_silence_markers("[SILENT] Entregue: cargo corrigido.") == "Entregue: cargo corrigido."
    assert strip_silence_markers("[SILENT]\n[SILENT]\nPronto: x.\nNO_REPLY\n\n[silent]") == "Pronto: x."
    assert strip_silence_markers("Pronto: x. [SILENT] [SILENT]") == "Pronto: x."
    assert strip_silence_markers("O worker ficou silent por 3 min e voltou.") == "O worker ficou silent por 3 min e voltou."
    assert strip_silence_markers("A frase [SILENT] no meio fica.") == "A frase [SILENT] no meio fica."
    assert strip_silence_markers("SILENTLY o worker parou e voltou") == "SILENTLY o worker parou e voltou"
    assert strip_silence_markers("estado final: NOT_SILENT") == "estado final: NOT_SILENT"
    assert _strip_tail_silence_marker("estado final: NOT_SILENT") == "estado final: NOT_SILENT"
    assert strip_silence_markers("[SILENT]\n\n  \n") == ""
    assert strip_silence_markers("") == "" and strip_silence_markers(None) is None
    assert _strip_tail_silence_marker("Entregue: y.\n\n[SILENT]") == "Entregue: y."
    assert _strip_tail_silence_marker("sem marcador") == "sem marcador"


def test_wake_rule_and_narration_filter_are_aligned():
    assert "termine a resposta com exatamente [SILENT]" not in kw.WAKE_GROUP_RULE
    assert "NÃO acrescente [SILENT]" in kw.WAKE_GROUP_RULE and "Pronto" in kw.WAKE_GROUP_RULE
    for ok in ("Pronto: cargo corrigido.", "Parcial: metade.", "Entregue: x", "PERGUNTA para Ana: qual?", "Aviso: vai atrasar."):
        assert not _wake_narration_to_suppress(WAKE, ok)
    assert _wake_narration_to_suppress(WAKE, "Resolvi as decisões pendentes e o card segue.")


def test_public_title_removes_forward_noise_and_keeps_useful_numbers():
    raw = "mais uma demanda: [11/09, 20:33] +55 38 9152-9909: https://admin.concursaai.com/notice-uploads/91ee6231-d8f3-42e0-8ea4-f11420490011?x=1 [11/09, 20:33] +55 38 9152-9909: Auxiliar de Regulação Médica"
    title = public_request_title(raw)
    assert "9152" not in title and "[11/09" not in title and "mais uma demanda" not in title
    assert title.startswith("admin.concursaai.com/notice-uploads/91ee6231-d8f…") and title.endswith("Auxiliar de Regulação Médica")
    assert public_request_title("[Ana|7550030839]\nCargo 301 sem disciplinas") == "Cargo 301 sem disciplinas"
    assert public_request_title("Erro na versão 2.14.3 do edital 45/2026, protocolo 20261234567890, valor R$ 1.234,56") == "Erro na versão 2.14.3 do edital 45/2026, protocolo 20261234567890, valor R$ 1.234,56"
    assert public_request_title("Ligar para (38) 99152-9909 ou 38 9152 9909 ou +5538991529909") == "Ligar para ou ou"
    assert public_request_title("Título limpo") == "Título limpo"
    assert public_request_title(public_request_title(raw)) == title
    assert public_request_title("[11/09, 20:33] +55 38 9152-9909:") == ""
    assert request_card_title("[Maikol|996979567]\nmais uma demanda: [11/09, 20:33] +55 38 9152-9909: Auxiliar de Regulação Médica").startswith("Auxiliar de Regulação Médica")


def test_progress_texts_are_public_and_closing_is_honest():
    bar = kw._progress_render("t_10a026a6", 4, "testar", 38, 91, 250, "M", "abrir PR", title="[Ana|1]\nCargo 301 sem disciplinas +55 38 9152-9909")
    assert bar.startswith("▰▰▰▰▱▱▱ 4/7 testar · Cargo 301 sem disciplinas · 38 min") and "t_10a026a6" not in bar and "tools" not in bar and "9152" not in bar
    assert bar.endswith("próximo: abrir PR")
    assert kw._progress_render("t_x", 7, "parcial", 57, 64, 80, "P", done=True, title="Cargo", outcome="parcial").startswith("▰▰▰▰▰▰▰ 7/7 parcial · Cargo")
    text = kw._progress_recebido("t_x", "mais uma demanda: [11/09, 20:33] +55 38 9152-9909: Cargo 301", "M", "corpo\nCritério de aceite: filtro funciona\n")
    assert text == "Recebido: Cargo 301\nPronto quando: filtro funciona"


def test_client_chat_rule_from_project_source(monkeypatch):
    src = {"platform": "telegram", "chat_id": "-100", "thread_id": "41"}
    monkeypatch.setattr(kw, "_client_source_for_board", lambda board: (src, True))
    assert kw._is_client_chat("concursa-ai", {"platform": "telegram", "chat_id": "-100", "thread_id": "41"}) is True
    assert kw._is_client_chat("concursa-ai", {"platform": "Telegram", "chat_id": -100, "thread_id": 41}) is True
    assert kw._is_client_chat("concursa-ai", {"platform": "telegram", "chat_id": "996979567", "thread_id": None}) is False
    monkeypatch.setattr(kw, "_client_source_for_board", lambda board: (None, True))
    assert kw._is_client_chat("concursa-ai", {"platform": "telegram", "chat_id": "996979567", "thread_id": None}) is True
    monkeypatch.setattr(kw, "_client_source_for_board", lambda board: (None, False))
    assert kw._is_client_chat("project-factory", {"platform": "telegram", "chat_id": "-100", "thread_id": "41"}) is False


def test_client_source_distinguishes_missing_project_from_query_failure(monkeypatch):
    import hermes_cli.nfos_runtime as runtime
    monkeypatch.setattr(runtime, "project_config", lambda board, config=None: None)
    assert kw._client_source_for_board("project-factory") == (None, False)
    monkeypatch.setattr(runtime, "project_config", lambda board, config=None: {"enabled": True, "board": board, "source": {"platform": "telegram", "chat_id": "-100", "thread_id": "41"}})
    assert kw._client_source_for_board("concursa-ai") == ({"platform": "telegram", "chat_id": "-100", "thread_id": "41"}, True)
    monkeypatch.setattr(runtime, "project_config", lambda board, config=None: {"enabled": True, "board": board})
    assert kw._client_source_for_board("concursa-ai") == (None, True)

    def boom(board, config=None):
        raise RuntimeError("config indisponível")
    monkeypatch.setattr(runtime, "project_config", boom)
    assert kw._client_source_for_board("concursa-ai") == (None, True)
    assert kw._is_client_chat("concursa-ai", {"platform": "telegram", "chat_id": "996979567", "thread_id": None}) is True


def test_progress_outcome_only_claims_delivery_with_a_report(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "outcome.db"))
    monkeypatch.setattr(kb, "_resolve_executable_assignee", lambda name: name)
    kb.init_db()
    from hermes_cli import nfos_delivery as delivery
    conn = kb.connect()
    try:
        delivery.init_schema(conn)
        tid = kb.create_task(conn, title="x", assignee="worker")
        assert kw._progress_outcome(None, tid) == "concluído"  # sem relatório: não afirma entrega

        def report(content):
            conn.execute("DELETE FROM nfos_artifacts WHERE task_id=?", (tid,))
            conn.execute("INSERT INTO nfos_artifacts(task_id,run_id,kind,revision,content,author,evidence,created_at) VALUES(?,1,'report',1,?,'w','{}',1)", (tid, content))
            conn.commit()
        report("{isso não é json")
        assert kw._progress_outcome(None, tid) == "concluído"
        report(json.dumps({"summary": "ok", "criteria": [{"id": "C1", "status": "PASS"}]}))
        assert kw._progress_outcome(None, tid) == "entregue"
        report(json.dumps({"summary": "ok", "criteria": [{"id": "C1", "status": "PASS"}, {"id": "C2", "status": "NOT_RUN"}]}))
        assert kw._progress_outcome(None, tid) == "parcial"
        report(json.dumps({"summary": "ok", "partial_delivery": True, "criteria": [{"id": "C1", "status": "PASS"}]}))
        assert kw._progress_outcome(None, tid) == "parcial"
        report(json.dumps({"summary": "cancelado", "disposition": "cancelled_by_owner"}))
        assert kw._progress_outcome(None, tid) == "encerrado"
        report(json.dumps({"summary": "sem critérios"}))
        assert kw._progress_outcome(None, tid) == "concluído"
    finally:
        conn.close()


def _notifier_setup(tmp_path, monkeypatch, name):
    from tests.gateway.test_kanban_notifier import RecordingAdapter, _make_runner, _run_one_notifier_tick
    db_path = tmp_path / f"{name}.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(kb, "_resolve_executable_assignee", lambda name: name)  # sem perfil real na máquina de teste
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Cargo 301 sem disciplinas", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="origin-chat")
        assert kb.complete_task(conn, tid, summary="Tempo total: n/a\nFuso: America/Sao_Paulo\nCloseout: abc123")
    finally:
        conn.close()
    adapter = RecordingAdapter()
    return tid, adapter, lambda: asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))


def test_completed_in_client_chat_publishes_nothing_but_keeps_receipt_and_trace(tmp_path, monkeypatch):
    tid, adapter, tick = _notifier_setup(tmp_path, monkeypatch, "client")
    monkeypatch.setattr(kw, "_client_source_for_board", lambda board: ({"platform": "telegram", "chat_id": "origin-chat", "thread_id": None}, True))
    tick()
    tick()  # segundo tick: recibo já processado, nada se repete
    assert adapter.sent == []
    conn = kb.connect()
    try:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
        assert kinds.count("client_publication_suppressed") == 1
    finally:
        conn.close()


def test_completed_in_operator_chat_still_publishes_the_trace(tmp_path, monkeypatch):
    tid, adapter, tick = _notifier_setup(tmp_path, monkeypatch, "operator")
    monkeypatch.setattr(kw, "_client_source_for_board", lambda board: ({"platform": "telegram", "chat_id": "-100", "thread_id": "41"}, True))
    tick()
    assert len(adapter.sent) == 1 and "Worker concluído" in adapter.sent[0]["text"]
