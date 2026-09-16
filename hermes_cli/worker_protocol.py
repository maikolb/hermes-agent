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
1. Reuse the current spec and checkpoint; continue the unfinished step without
   repeating analysis. Plan and execute with the selected worker model.
   Do not launch an automatic Claude TL consultation or a separate planner.
   Preserve explicit model/provider pins, including #deepseek. If specialized
   assistance is necessary, resolve an explicit compatible model/provider pair;
   never send another provider's model through the current provider by default.
2. Persist the versioned spec on the card before implementation. Analysis may
   start before the spec; implementation must wait for the persisted spec.
3. Use the selected model for implement/test/correct cycles with persisted progress. New behavior
   needs a relevant failing test before implementation, then passing regression.
   Reused components need contract/integration checks, not invented RED history.
   Call this Ralph only when a compatible Ralph integration actually ran.
4. For an operation, execute the existing supported mechanism directly and
   verify the requested outcome at its authorized destination. Operations,
   reports and audits without code changes require no PR or deployment.
5. For code changes, run the applicable tests, prepare the PR with spec and
   evidence, and request Principal review. Publish only as required by the
   approved destination: TEST, HML, staging, preview or production are valid.
   Do not add a production promotion or a separate homologation requirement
   beyond that scope. Serialize integration/deployment per project. Before
   retrying an ambiguous external operation, read its actual destination.
6. Verify the result at the requested destination, including the deployed
   version when applicable. Record commands, results and real images proving
   the behavior. Save the spec, applicable PR reference and evidence report
   on the card before kanban_complete.

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
