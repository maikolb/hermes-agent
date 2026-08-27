"""Retention policy: human messages the compressor must never consume.

TARGET_ARCHITECTURE gap 1 (27/08/2026): batch compaction summarized away
recent HUMAN messages — including attachments that had not been processed
yet — so a later mention saw only the summary and the agent "forgot" what
the operators had just said (DOVCRM 17:56 incident: three observed media
messages archived mid-turn; Ceogame flow degraded the same way).

Policy, deliberately simple and predictable: the last ``keep_last`` REAL
human messages in the transcript are inviolable. Position does not matter;
if they fall inside the compressible middle they are carried verbatim into
the compacted transcript (prefixed to the protected tail, original order).
System-generated user rows (system notes, compaction markers) are not
human messages and get no protection from this policy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

_SYNTHETIC_USER_PREFIXES = (
    "[System note:",
    "[CONTEXT COMPACTION",
    "[Note:",
)


def _content_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def is_real_human_message(message: Any) -> bool:
    """A user-role message actually written (or sent) by a person.

    Includes observed messages and media placeholders ("[Autor] [audio ...
    saved at ...]"); excludes synthetic user rows the gateway/compressor
    fabricates (system notes, compaction markers), which start with a known
    prefix.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    text = _content_text(message).strip()
    if not text:
        return False
    return not any(text.startswith(prefix) for prefix in _SYNTHETIC_USER_PREFIXES)


def select_retained_middle_humans(
    messages: List[Dict[str, Any]],
    compress_start: int,
    compress_end: int,
    keep_last: int,
) -> List[Tuple[int, Dict[str, Any]]]:
    """(index, message) pairs the compressor must rescue from the middle.

    The last ``keep_last`` real human messages of the WHOLE transcript are
    protected; this returns the subset that falls inside
    ``[compress_start, compress_end)`` (the summarization window), oldest
    first. Head and tail members of the protected set are already safe by
    position and are not returned.
    """
    if keep_last <= 0:
        return []
    protected: List[int] = []
    for index in range(len(messages) - 1, -1, -1):
        if is_real_human_message(messages[index]):
            protected.append(index)
            if len(protected) >= keep_last:
                break
    rescued = [
        index
        for index in protected
        if compress_start <= index < compress_end
    ]
    rescued.sort()
    return [(index, messages[index]) for index in rescued]
