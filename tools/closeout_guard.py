"""Shared closeout-quality guard: reject status-as-closeout.

28/08 Central_DEC: a principal-turn mirror card closed ``done`` in 40s with
result "**Em execução.**" — a status line, not a closeout — and the operator
read the board as stalled. The #60 length guard cannot catch this class
(filler and status lines clear 40 chars easily), so completion paths share
this single opening-phrase check: a ``done`` record must state what WAS
done, never what is still happening.
"""

from __future__ import annotations

import os
import re
import unicodedata


def closeout_rewrite_enabled() -> bool:
    """One escape for the whole guard family (reviewer round 2).

    ``HERMES_KANBAN_REQUIRE_CLOSEOUT=off`` must disable BOTH the tool
    rejection and the mirrors' honest-note rewrites — an operator who
    turned the guard off would otherwise still get altered results.
    """
    return os.environ.get(
        "HERMES_KANBAN_REQUIRE_CLOSEOUT", "on"
    ).strip().lower() not in ("off", "0", "false")

# Openers that declare in-progress/future work. Matched against the
# normalized OPENING of the closeout only — a legitimate closeout that
# mentions "aguardando" mid-text after stating its delivery still passes.
# English entries are real status PHRASES, not bare verb prefixes:
# reviewer round 2 showed "Starting point isolated..." and "Will not
# recur: invariant test added." being rejected by "starting"/"will ".
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
    "starting to ",
    "starting on ",
    "starting work",
    "still running",
    "waiting for ",
    "waiting on ",
    "will start",
    "will now ",
    "will then ",
    "will continue",
    "about to ",
)

# Label prefixes that wrap a status line without changing its meaning:
# "Status: **Em execução.**" must match the same as the bare line.
_LABEL_RE = re.compile(r"^(status|estado|situacao|situação)\s*:\s*", re.I)

_NOISE_RE = re.compile(
    r"^[\s*_`#>•\-\N{WHITE HEAVY CHECK MARK}✔️☑️"
    r"\N{HOURGLASS WITH FLOWING SAND}\N{HOURGLASS}"
    r"\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}"
    r"\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}"
    r"\N{VARIATION SELECTOR-16}]+"
)


def looks_like_status_not_closeout(text: str) -> bool:
    """True when *text* OPENS by declaring work still in progress.

    Normalization (NFKC + noise class) strips markdown emphasis,
    list/quote markers, check-mark and spinner/hourglass emoji so
    "**Em execução.**", "✅ Aguardando..." and "⏳ Em andamento" match.
    Empty text is not a status line (the length guard owns that case).
    """
    head = (text or "").strip()
    if not head:
        return False
    head = unicodedata.normalize("NFKC", head)
    head = _NOISE_RE.sub("", head).casefold().lstrip()
    label = _LABEL_RE.match(head)
    if label:
        head = _NOISE_RE.sub("", head[label.end():]).lstrip()
    return head.startswith(_STATUS_OPENERS)
