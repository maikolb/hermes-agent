"""Shared closeout-quality guard: reject status-as-closeout.

28/08 Central_DEC: a principal-turn mirror card closed ``done`` in 40s with
result "**Em execução.**" — a status line, not a closeout — and the operator
read the board as stalled. The #60 length guard cannot catch this class
(filler and status lines clear 40 chars easily), so completion paths share
this single opening-phrase check: a ``done`` record must state what WAS
done, never what is still happening.
"""

from __future__ import annotations

import re

# Openers that declare in-progress/future work. Matched against the
# normalized OPENING of the closeout only — a legitimate closeout that
# mentions "aguardando" mid-text after stating its delivery still passes.
_STATUS_OPENERS = (
    "em execucao",
    "em execução",
    "executando",
    "em andamento",
    "em progresso",
    "aguardando",
    "iniciando",
    "vou ",
    "irei ",
    "trabalhando",
    "in progress",
    "working on",
    "starting",
    "waiting",
    "will ",
)

_NOISE_RE = re.compile(r"^[\s*_`#>•\-\N{WHITE HEAVY CHECK MARK}✔️☑️]+")


def looks_like_status_not_closeout(text: str) -> bool:
    """True when *text* OPENS by declaring work still in progress.

    Normalization strips markdown emphasis, list/quote markers and
    check-mark emoji so "**Em execução.**" and "✅ Aguardando..." match.
    Empty text is not a status line (the length guard owns that case).
    """
    head = (text or "").strip()
    if not head:
        return False
    head = _NOISE_RE.sub("", head).casefold().lstrip()
    return head.startswith(_STATUS_OPENERS)
