"""BLOCK_LESS_20260910: premissas do owner (autônomo, bloquear o mínimo, entregar primeiro)."""
import pytest

from agent.turn_checkpoint import CheckpointConflictError, TurnCheckpointStore
from hermes_cli import nfos_delivery as d
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_runtime as runtime


def test_profile_flag_false_disables_validation_even_with_delivery_destination(monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    monkeypatch.setattr(review.d, "get_workflow", lambda conn, task_id: {"request_id": "r1"})
    monkeypatch.setattr(review.d, "get_spec", lambda conn, task_id: {"content": '{"delivery_destination": {"environment": "homolog"}}'})
    assert review.required(None, "t_x") is False


def test_profile_flag_true_keeps_validation(monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": True})
    monkeypatch.setattr(review.d, "get_workflow", lambda conn, task_id: {"request_id": "r1"})
    monkeypatch.setattr(review.d, "get_spec", lambda conn, task_id: {"content": '{"delivery_destination": {"environment": "homolog"}}'})
    assert review.required(None, "t_x") is True


@pytest.mark.parametrize("question", [
    "HML slot remains occupied by Products task t_35bd4a15/run672. May this card acquire the shared HML slot?",
    "The official project slot readback is inconsistent: Products run672 has a release receipt",
    "Resolve the partial HML delivery and stale lease for t_9ce8e0e3 before another publication attempt.",
    "What is the next authorized action for t_9ce8e0e3?",
    "RateLimitError repeated on run 5; usage limit reached for the provider",
])
def test_known_impediments_get_automatic_continue(question):
    assert d._auto_continue_answer("impediment", question).startswith("CONTINUE (automático")


def test_unknown_impediment_and_reviews_still_go_to_the_principal():
    assert d._auto_continue_answer("impediment", "Which of the two PDFs is the official source for cargo 327?") is None
    assert d._auto_continue_answer("review", "slot occupied") is None
    assert d._auto_continue_answer("spec_review", "slot occupied") is None


def test_homologation_is_automatic():
    assert d._auto_continue_answer("homologation", "Accept exact candidate e13c87d for HML publication?").startswith("CONTINUE")


def test_human_requires_question_and_recipient():
    assert d._human_question_valid("Qual PDF é a fonte oficial do cargo 327?", "Maikol")
    assert not d._human_question_valid("Pausa técnica para reclassificação", "Maikol")
    assert not d._human_question_valid("Qual PDF?", "")
    assert not d._human_question_valid(None, None)


def test_worker_prompt_starts_with_premises_when_validation_is_off(monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    text = runtime.worker_instructions()
    assert text.startswith("OWNER PREMISES")
    assert "never ask spec_review or final_review" in text
    assert "acquire-project --wait 900" in text


def test_worker_prompt_has_no_premises_when_validation_is_on(monkeypatch):
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": True})
    assert runtime.worker_instructions().startswith("Exact workflow CLI prefix")


def _messages(*contents):
    rows = [{"role": "system", "content": "system"}]
    for index, content in enumerate(contents):
        rows.append({"role": "user" if index % 2 == 0 else "assistant", "content": content})
    return rows


def test_transition_retries_after_revision_conflict(tmp_path, monkeypatch):
    store = TurnCheckpointStore(tmp_path / "checkpoints")
    store.start_turn("session-1", "turn-1", "run", _messages("run"))
    real_write = store._write
    calls = {"n": 0}

    def flaky_write(state, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CheckpointConflictError("stale checkpoint write rejected: revision changed")
        return real_write(state, **kwargs)

    monkeypatch.setattr(store, "_write", flaky_write)
    state = store.transition("session-1", phase="tool_attempting", next_action="await_tool")
    assert state["phase"] == "tool_attempting"
    assert state["revision"] == 2
    assert calls["n"] == 2


def test_transition_gives_up_after_three_conflicts(tmp_path, monkeypatch):
    store = TurnCheckpointStore(tmp_path / "checkpoints")
    store.start_turn("session-1", "turn-1", "run", _messages("run"))

    def always_conflict(state, **kwargs):
        raise CheckpointConflictError("stale checkpoint write rejected: revision changed")

    monkeypatch.setattr(store, "_write", always_conflict)
    with pytest.raises(CheckpointConflictError):
        store.transition("session-1", phase="tool_attempting")
