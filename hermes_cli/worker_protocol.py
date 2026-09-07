"""Delivery instructions for the board's full-capability workers."""
from __future__ import annotations

__all__ = ["dispatcher_worker_protocol"]


def dispatcher_worker_protocol() -> str:
    """The card supplies scope; the Principal manages decisions and delivery review."""
    return """## Worker delivery workflow
Read the current card, original request, attachments, prior work and instructions.
Analyze text, images, audio and video relevant to the request. Preserve originals.
Report additional independent tasks to the Principal instead of expanding scope.

For a card enrolled in the NFOS delivery workflow:
1. Ask Claude TL to turn the request into verifiable criteria and direct steps.
   Use Codex when Claude is unavailable and record the actual fallback reason.
2. Persist the versioned spec on the card before implementation. Analysis may
   start before the spec; implementation must wait for the persisted spec.
3. Use Codex implement/test/correct cycles with persisted progress. New behavior
   needs a relevant failing test before implementation, then passing regression.
   Reused components need contract/integration checks, not invented RED history.
   Call this Ralph only when a compatible Ralph integration actually ran.
4. Validate the candidate in the project's homolog environment. Record its SHA,
   commands, results and real images demonstrating the requested behavior.
5. Create or update the PR with spec and evidence, then request Principal review.
   The Principal can approve merge and deployment within the authorized scope.
   Serialize homolog/integration/deployment per project. Before retrying an
   ambiguous external operation, read its destination to find the actual result.
6. Confirm the deployed version and functional production readback. Save the
   spec, PR reference and evidence report on the card before kanban_complete.
Reports and audits without code changes do not require a PR or deployment.

Persist progress during work: session, stage, next action, files, test results,
evidence and external references. Keep uncommitted work recoverable. A resumed
worker must execute the unfinished step, not replay its previous final answer.
Send impediments to the Principal's persisted review queue with what happened,
what was tried and the concrete next decision. If the Principal resolves the
impediment, continue this execution. When human input is required, persist the
state and exit with the card awaiting that answer; do not consume a worker slot.
Never mark a task done solely because the process exited successfully. Complete
only after applicable criteria, review and delivery have been verified. The
kanban_complete result must describe the outcome, evidence and any limitations.
Do not add approvals, credentials, unrelated work or project-wide restrictions.
"""
