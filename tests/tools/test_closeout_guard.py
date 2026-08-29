"""Status-as-closeout guard (28/08 Central_DEC: card done in 40s with
result "**Em execução.**" — the board lied and the operator read a stall)."""

from __future__ import annotations

from tools.closeout_guard import looks_like_status_not_closeout
from tools.principal_turn_mirror import _compose_final_result


def test_status_openers_match_through_markdown_and_emoji():
    assert looks_like_status_not_closeout("**Em execução.**")
    assert looks_like_status_not_closeout("✅ Aguardando o deploy do cliente")
    assert looks_like_status_not_closeout("Em andamento: subindo o repo")
    assert looks_like_status_not_closeout("- vou criar o contrato em seguida")
    assert looks_like_status_not_closeout("Working on the private repo now")
    # Spinner/hourglass noise + NFKC (reviewer: "⏳ Em execução" escaped)
    assert looks_like_status_not_closeout("⏳ Em execução")
    assert looks_like_status_not_closeout("🔄 Aguardando resposta da API")
    assert looks_like_status_not_closeout("Ｅm execução")  # fullwidth E, NFKC


def test_delivered_closeouts_pass():
    assert not looks_like_status_not_closeout(
        "Done: repo criado e push feito. Evidence: CI verde. "
        "Limitations: aguardando aprovação do deploy."
    )
    assert not looks_like_status_not_closeout(
        "Scope: provisionar. Done: workspace+board+repo. Evidence: gh repo view ok."
    )
    assert not looks_like_status_not_closeout("")
    assert not looks_like_status_not_closeout(None)


def test_english_noun_openers_are_not_status():
    """Reviewer round 2: bare prefixes 'starting'/'will ' rejected
    legitimate delivered closeouts — phrases only."""
    assert not looks_like_status_not_closeout(
        "Starting point isolated; parser fixed and tests pass."
    )
    assert not looks_like_status_not_closeout(
        "Will not recur: invariant test added."
    )
    assert not looks_like_status_not_closeout(
        "Waiting time reduced from 5s to 200ms after the cache fix."
    )
    assert looks_like_status_not_closeout("Starting to build the repo now")
    assert looks_like_status_not_closeout("Will start the deploy next")
    assert looks_like_status_not_closeout("Waiting for the client's approval")


def test_status_label_prefix_is_transparent():
    """Reviewer round 2: 'Status: **Em execução.**' escaped the guard."""
    assert looks_like_status_not_closeout("Status: **Em execução.**")
    assert looks_like_status_not_closeout("Estado: aguardando o deploy")
    assert not looks_like_status_not_closeout(
        "Status: entregue — PR #12 mergeada, CI verde."
    )


def test_mirror_final_result_replaces_status_line(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_REQUIRE_CLOSEOUT", raising=False)
    out = _compose_final_result("**Em execução.**", 1)
    assert "SEM closeout" in out
    assert "Em execução" in out  # snippet preserved, quoted
    assert not out.startswith("**Em execução")


def test_mirror_final_result_keeps_real_closeout():
    closeout = "Bugs confirmados e hotfix aplicado; PR #300 mergeada."
    out = _compose_final_result(closeout, 7)
    assert out.startswith(closeout)
    assert "~7 min" in out
    assert _compose_final_result("", 3) == (
        "Turno principal concluído em ~3 min."
    )


def test_mirror_rewrite_makes_no_unverified_claims(monkeypatch):
    """Reviewer round 2: the note must state only what the mirror knows —
    no assertion that continuation cards exist."""
    monkeypatch.delenv("HERMES_KANBAN_REQUIRE_CLOSEOUT", raising=False)
    out = _compose_final_result("**Em execução.**", 2)
    assert "SEM closeout" in out
    assert "não verificada" in out
    assert "cards" not in out.casefold().replace("closeout", "")


def test_mirror_rewrite_respects_escape_hatch(monkeypatch):
    """Reviewer round 2: one escape for the whole family — off means the
    mirrors publish verbatim too."""
    monkeypatch.setenv("HERMES_KANBAN_REQUIRE_CLOSEOUT", "off")
    out = _compose_final_result("**Em execução.**", 2)
    assert out.startswith("**Em execução.**")
