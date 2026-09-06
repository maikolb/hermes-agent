# REG-2026-09-06-003: worker continuation

Incident: Concursa t_d61b19ea reused session 20260906_043517_a6380b and returned
four identical 672-character answers without tools. Checkpoint phase was
deliverable_composed, next_action finalize_delivery; its sealed hash matched all
four replies. DOV t_9ce8e0e3 also had a composed answer checkpoint.

Event 10124 gave_up was followed by promoted and run 334 claimed in one second.
The protocol breaker counted three violations while consecutive_failures was one.
The next dispatcher tick checked the generic counter, ignoring the durable give-up.

Repair: a worker with a finished answer starts a new turn in its existing session.
Its transcript and workspace survive. Unfinished execution and pending verification
retain checkpoint recovery; ordinary gateway answer delivery retains replay.
gave_up stays blocked until the existing unblock API records a continuation.

Prevention: incident-shaped checkpoint and dispatcher tests failed before the fix.
Final focused verification: seven worker continuation checks plus three breaker
checks passed. The dispatcher check invokes the next actual dispatch cycle after
three clean worker exits, and checks explicit unblock remains supported.

Local verification does not establish target operation. Publication, exact runtime
activation and real incident continuation are pending in this commit.
