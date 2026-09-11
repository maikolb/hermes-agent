# REG-2026-09-05-001: preserve user intent through compaction

An ongoing delivery was repeatedly interrupted by status questions and conflicting instructions. Runtime-delivered steering is stored at the end of tool results, so the user-role-only verbatim collector omitted it. Legacy compaction did not retain the deterministic user section at all. The reference prefix also asserted that historical requests had already been addressed.

The repair retains a bounded newest-first user section in legacy mode, includes the existing terminal steering marker, and preserves cancellation chronology. Old head instructions already covered by an earlier summary remain summarizable evidence but are excluded from the renewed verbatim section. Ordinary tool output and embedded marker examples are not promoted to user records.

Completion guidance now distinguishes status questions from cancellation, preserves the requested delivery boundary and selected model, checks that delegated instructions permit completion, and directs bounded background work through the existing completion notification surface.

Validation: the isolated legacy-mode replay changes from 1/4 to 4/4 checks without model calls. Focused and adjacent regression files cover repeat compaction after restart, summary-prefix compatibility, multimodal tool results, cancellation, bounded retention and background notification routing. This proves data retention and routing behavior, not universal model compliance. No memory store is reset and no production release is activated by these changes.
