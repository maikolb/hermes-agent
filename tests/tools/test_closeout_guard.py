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


def test_mirror_final_result_replaces_status_line():
    out = _compose_final_result("**Em execução.**", 1)
    assert "anunciando continuação" in out
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
