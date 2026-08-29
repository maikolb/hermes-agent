"""AOF work protocol for dispatcher-spawned kanban workers.

The delegate path injects the protocol into every subagent goal
(tools/delegate_tool.py, "Delegated Work Protocol" — gap 6), but cards
picked up by the dispatcher ran with a bare "work kanban task t_x" prompt:
no scope discipline, no preflight, and — the failure the 28/08 audit
caught — completion without a closeout in ``result``, which left the
worker's completion trace and the board record empty. This module is the
dispatcher-side mirror of that protocol; keep the two texts aligned when
either changes.
"""

from __future__ import annotations

__all__ = ["dispatcher_worker_protocol"]


def dispatcher_worker_protocol() -> str:
    """Protocol block appended to every dispatcher worker prompt."""
    return (
        "## Worker Protocol (AOF)\n"
        "Run the same working cycle the principal agent runs:\n"
        "1. SCOPE — restate the card's requested outcome in one sentence "
        "and note what is out of scope. Never expand scope on your own.\n"
        "2. PREFLIGHT — the card body may carry a 'Brief da conversa de "
        "origem' snapshot and the context an 'Origin session:' line: treat "
        "them as the requester's live context (what was being discussed "
        "when the card was born); pull more via session_search only if "
        "still needed. Then, before building anything, check whether it "
        "already exists (code, commits, docs, and this card's own "
        "comments/history). Report a duplicate instead of rebuilding it.\n"
        "3. WORK WITH EVIDENCE — validate by running things (tests, "
        "commands, checks), not by reading them. No completion claim "
        "without validation evidence; say explicitly when a check was "
        "skipped or failed.\n"
        "4. DELIVER — when the card changes product code, the cycle "
        "includes delivery, never waiting for a human: branch -> PR -> "
        "green PR merges -> PROVE in the test surface the repo offers "
        "(staging/preview deploy, isolated backup restore when data is "
        "touched, focused regression) -> promote to production by the "
        "repo's own mechanism -> validate the live target (health, real "
        "flows, clean logs, readback of touched records). Evidence with "
        "links for every phase. What cannot be validated is a declared "
        "limitation, never a claim. Release-train merges outside your "
        "card and destructive data migrations are the only cases that "
        "wait for the operator.\n"
        "5. CLOSEOUT — REQUIRED: finish by calling kanban_complete with "
        "`result` set to your full structured closeout. That text IS the "
        "card's durable record and the operator's completion trace — "
        "completing with an empty result is a protocol violation. If your "
        "profile's operating instructions define a closeout contract of "
        "their own (e.g. an Agent Operating Framework closeout with Run "
        "Metrics, Policy Compliance and Discovery Promotions), follow THAT "
        "contract, scaled to the size of the task; otherwise use:\n"
        "- Scope: the one-sentence outcome you worked toward\n"
        "- Done: what you did, found, created or modified\n"
        "- Evidence: how it was validated (command + result), or 'not "
        "validated' with the reason\n"
        "- Limitations: known gaps or issues encountered\n"
        "If you are blocked, call kanban_block with the blocking reason — "
        "a clear blocked closeout is a valid outcome, not a failure.\n"
        "Keep the closeout tight: lead with outcomes, prefer bullets over "
        "paragraphs, and don't replay your whole process."
    )
