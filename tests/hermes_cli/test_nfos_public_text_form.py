"""PUBLIC_TEXT_FORM_20261009: a forma do texto que o NFOS escreve para o solicitante é conferida ao salvar.

Maikol, 09/10/2026 ("1 sim, 2 sim, 3 só time"). A medição de 123 textos públicos em produção mostrou conteúdo bom e forma
ruim: travessão em 14% deles (27% no resumo da entrega final), uma pergunta de 3032 palavras, um resumo de 1583 e sha de 40
caracteres colado em 2 perguntas. A instrução só tratava de conteúdo e nada conferia a forma. Agora a pergunta ao
solicitante, a entrega de dado e o resumo público do relatório voltam a quem os escreveu, com o que corrigir, antes de
serem gravados. Nenhuma recusa pede algo a um humano.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review

DASH = chr(0x2014)
EN_DASH = chr(0x2013)
SHA = "8d2d6039e1" + "0f" * 15
TO = "CS Concursa (solicitante)"
QUESTION = "Pode enviar um vídeo curto da tela com o erro? Conferimos o simulado e a disciplina aparece certa aqui."
DELIVERY = "Corrigimos o conteúdo do seu plano. Conferimos no PDF do edital e as 12 disciplinas batem. A validação em HML passou."


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False, "projects": {"pilot": {"owner_questions": False}}})
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
        yield conn


@pytest.fixture
def report_board(tmp_path, monkeypatch):
    """Board com a configuração padrão: a checagem do relatório não depende do modo do projeto."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
        yield conn


@pytest.fixture
def worker():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _started(conn, pid, message_id="11"):
    rid = delivery.receive_request(conn, source={"platform": "telegram", "chat_id": "-10001", "thread_id": "8", "message_id": message_id},
                                   text="Simulado com disciplina errada.",
                                   project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
    request = delivery.reserve_request(conn, capacity=2)
    return delivery.bootstrap_card(conn, rid, request["claim_token"], pid=pid)


def _card(conn, pid, message_id="11"):
    """Card com uma decisão pendente do Principal, como o que chega a ele pelo `decide`."""
    task = _started(conn, pid, message_id)
    decision = delivery.ask_principal(conn, task.id, task.current_run_id, kind="impediment",
                                      question="Falta o print da tela para localizar a disciplina.", context={})
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?", (int(time.time()), task.current_run_id))
    conn.execute("UPDATE tasks SET status='ready', worker_pid=NULL, claim_lock=NULL, current_run_id=NULL WHERE id=?", (task.id,))
    conn.commit()
    return kb.get_task(conn, task.id), decision


def _context(conn, decision):
    return json.loads(delivery.get_decision(conn, decision)["context"])


def _ask(conn, decision, text):
    delivery.resolve_decision(conn, decision, action="human", author="Principal", answer="Pergunta ao solicitante.",
                              public_message={"kind": "question", "text": text, "to": TO})


def _deliver(conn, decision, text):
    delivery.resolve_decision(conn, decision, action="continue", author="Principal", answer="CONTINUE: dado entregue.",
                              public_message={"kind": "delivery", "text": text})


def _working_card(conn, tmp_path):
    """Card em execução com spec salva e a evidência do relatório no disco: o worker pronto para o `save-report`."""
    task = _started(conn, os.getpid())
    delivery.save_spec(conn, task.id, task.current_run_id,
                       {"goal": "Plano corrigido", "criteria": [{"id": "AC1", "text": "Disciplinas conferidas"}],
                        "steps": ["Conferir", "Corrigir"], "delivery_type": "report"},
                       author="Claude TL", evidence={"session": "tl-001", "output": "/evidence/tl.jsonl"})
    evidence = tmp_path / "conferencia.txt"
    evidence.write_text("12 disciplinas conferidas", encoding="utf-8")
    return task, evidence


def _report(evidence, public_summary):
    # O resumo interno leva travessão e sha de propósito: só o campo público é conferido.
    return {"summary": f"Plano corrigido {DASH} candidato {SHA}", "artifacts": [str(evidence)],
            "criteria": [{"id": "AC1", "status": "PASS", "evidence": [str(evidence)]}],
            "public_delivery": {"summary": public_summary, "links": ["https://example.test/plano"]}}


def _reports(conn, task_id):
    return conn.execute("SELECT count(*) FROM nfos_artifacts WHERE task_id=? AND kind='report'", (task_id,)).fetchone()[0]


def test_question_with_a_dash_returns_to_the_principal_and_is_published_once_corrected(board, worker):
    task, decision = _card(board, worker.pid)
    with pytest.raises(delivery.WorkflowError, match="travessão"):
        _ask(board, decision, f"Pode enviar um vídeo curto da tela {DASH} o que mostra o erro?")
    assert delivery.get_decision(board, decision)["status"] == "pending"
    assert "public_message" not in _context(board, decision)
    _ask(board, decision, QUESTION)
    assert delivery.get_decision(board, decision)["status"] == "human"
    assert _context(board, decision)["public_message"]["text"] == QUESTION


@pytest.mark.parametrize("leak", [
    "Autoriza publicar o candidato" + SHA + "?",
    "Autoriza reabrir o card t_ab12cd34?",
    "A decisão dec_0123456789abcdef0123 vale para o seu chamado?",
])
def test_question_carrying_a_commit_hash_or_an_internal_id_is_refused(board, worker, leak):
    task, decision = _card(board, worker.pid)
    with pytest.raises(delivery.WorkflowError, match="identificador interno"):
        _ask(board, decision, leak)
    assert "public_message" not in _context(board, decision)


def test_delivery_text_is_checked_like_the_question(board, worker):
    task, decision = _card(board, worker.pid)
    with pytest.raises(delivery.WorkflowError, match="travessão"):
        _deliver(board, decision, f"Corrigimos o seu plano {EN_DASH} as 12 disciplinas batem com o edital.")
    assert delivery.get_decision(board, decision)["status"] == "pending"
    _deliver(board, decision, DELIVERY)
    published = _context(board, decision)["public_message"]
    assert (published["kind"], published["text"]) == ("delivery", DELIVERY)


def test_question_out_of_proportion_is_refused_with_its_size_and_the_ceiling(board, worker):
    task, decision = _card(board, worker.pid)
    with pytest.raises(delivery.WorkflowError, match="3032 palavras"):
        _ask(board, decision, "Pode confirmar o edital? " + " ".join(["contexto"] * 3028))
    # O limite antigo, de 4000 caracteres, deixava passar uma pergunta de 304 palavras.
    with pytest.raises(delivery.WorkflowError, match="304 palavras e o teto é"):
        _ask(board, decision, "Pode confirmar o edital? " + " ".join(["contexto"] * 300))
    assert delivery.get_decision(board, decision)["status"] == "pending"
    assert "public_message" not in _context(board, decision)


def test_one_refusal_lists_every_correction_and_asks_nothing_from_a_human(board, worker):
    task, decision = _card(board, worker.pid)
    with pytest.raises(delivery.WorkflowError) as refusal:
        _ask(board, decision, f"Autoriza o candidato {SHA} {DASH} " + " ".join(["contexto"] * 300) + "?")
    message = str(refusal.value)
    assert "travessão" in message and "identificador interno" in message and "o teto é" in message
    assert "não é motivo para perguntar a um humano" in message
    assert DASH not in message and EN_DASH not in message


def test_progress_update_keeps_its_internal_route_whatever_its_form(board, worker):
    task, decision = _card(board, worker.pid)
    delivery.resolve_decision(board, decision, action="continue", author="Principal", answer="CONTINUE: seguir com a conferência.",
                              public_message={"kind": "update", "text": f"Conferimos o edital {DASH} candidato {SHA}."})
    assert delivery.get_decision(board, decision)["status"] == "resolved"
    assert "public_message" not in _context(board, decision)


def test_replaying_a_decision_saved_before_the_rule_is_still_a_no_op(board, worker):
    task, decision = _card(board, worker.pid)
    _ask(board, decision, QUESTION)
    older = f"Pode enviar um vídeo curto da tela {DASH} o que mostra o erro?"
    ctx = _context(board, decision)
    ctx["public_message"]["text"] = older
    board.execute("UPDATE nfos_decisions SET context=? WHERE id=?", (json.dumps(ctx), decision))
    board.commit()
    _ask(board, decision, older)
    assert _context(board, decision)["public_message"]["text"] == older


def test_the_principal_cli_gets_the_correction_as_its_json_error(board, worker, tmp_path):
    task, decision = _card(board, worker.pid)
    payload = tmp_path / "decide.json"
    payload.write_text(json.dumps({"answer": "Falta o vídeo da tela.", "human_to": TO,
                                   "human_question": f"Pode enviar um vídeo curto da tela {DASH} o que mostra o erro?"}), encoding="utf-8")
    script = Path(delivery.__file__)
    env = {**os.environ, "PYTHONPATH": str(script.parents[1]), "PYTHONIOENCODING": "utf-8"}
    env.pop("HERMES_KANBAN_TASK", None)
    result = subprocess.run([sys.executable, str(script), "decide", "--decision", decision, "--resolution", "human",
                             "--input", str(payload), "--db", os.environ["HERMES_KANBAN_DB"]],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 1 and "Traceback" not in result.stderr, result.stderr[-1500:]
    error = json.loads(result.stdout.strip().splitlines()[-1])
    assert error["type"] == "WorkflowError" and "travessão" in error["error"]
    assert delivery.get_decision(board, decision)["status"] == "pending"


def test_report_public_summary_returns_to_the_worker_and_is_saved_once_corrected(report_board, tmp_path):
    board = report_board
    task, evidence = _working_card(board, tmp_path)
    with pytest.raises(delivery.WorkflowError, match="travessão"):
        delivery.save_report(board, task.id, task.current_run_id,
                             _report(evidence, f"Corrigimos o seu plano {DASH} as 12 disciplinas batem com o edital."))
    assert _reports(board, task.id) == 0
    delivery.save_report(board, task.id, task.current_run_id, _report(evidence, DELIVERY))
    assert _reports(board, task.id) == 1


def test_report_public_summary_out_of_proportion_is_refused(report_board, tmp_path):
    board = report_board
    task, evidence = _working_card(board, tmp_path)
    with pytest.raises(delivery.WorkflowError, match="1583 palavras"):
        delivery.save_report(board, task.id, task.current_run_id, _report(evidence, " ".join(["resultado"] * 1583) + "."))
    assert _reports(board, task.id) == 0


def test_report_without_a_public_summary_is_saved_as_before(report_board, tmp_path):
    board = report_board
    task, evidence = _working_card(board, tmp_path)
    report = _report(evidence, DELIVERY)
    del report["public_delivery"]
    delivery.save_report(board, task.id, task.current_run_id, report)
    assert _reports(board, task.id) == 1


def test_instructions_carry_the_form_rule_with_the_ceilings_the_check_enforces():
    from hermes_cli import nfos_public_text as form
    from hermes_cli.nfos_runtime import principal_instructions, worker_instructions
    assert str(form.QUESTION_MAX_WORDS) in form.PUBLIC_TEXT_FORM and str(form.RESULT_MAX_WORDS) in form.PUBLIC_TEXT_FORM
    for text in (principal_instructions(), worker_instructions()):
        assert form.PUBLIC_TEXT_FORM in text
    assert "the question itself comes first and alone" in principal_instructions()


@pytest.mark.parametrize("text", [
    QUESTION,
    DELIVERY,
    "Conferimos os dois-pontos do bem-te-vi no guarda-chuva.",
    "Corrigimos três itens:\n- a disciplina de Direito\n- o peso 10-12\n- a data 09/10/2026",
    "O sistema gravou a nota 35250112345678000190550010000012341000012345 no seu cadastro.",
])
def test_usual_public_texts_have_nothing_to_fix(text):
    from hermes_cli import nfos_public_text as form
    assert form.form_problems(text) == []
    assert form.form_problems(text, question=True) == []


def test_the_ceiling_counts_words_and_is_tighter_for_a_question():
    from hermes_cli import nfos_public_text as form
    words = lambda n: " ".join(["palavra"] * n) + "?"
    assert form.form_problems(words(form.QUESTION_MAX_WORDS), question=True) == []
    (over,) = form.form_problems(words(form.QUESTION_MAX_WORDS + 1), question=True)
    assert f"{form.QUESTION_MAX_WORDS + 1} palavras" in over and f"o teto é {form.QUESTION_MAX_WORDS}" in over
    assert form.form_problems(words(form.RESULT_MAX_WORDS)) == []
    (over,) = form.form_problems(words(form.RESULT_MAX_WORDS + 1))
    assert f"o teto é {form.RESULT_MAX_WORDS}" in over
    assert form.refusal("o resumo", words(form.RESULT_MAX_WORDS)) is None
