"""Resolve the Claude Code model/effort for one request without inference.

The request is read from stdin so it never appears in the process command
line. Output is bounded routing metadata and intentionally excludes the text.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent.smart_model_routing import resolve_claude_delegation_route


def main() -> int:
    message = sys.stdin.read(32_001)
    if len(message) > 32_000:
        print(json.dumps({"error": "request_too_large"}, sort_keys=True))
        return 2
    route = resolve_claude_delegation_route(message)
    print(json.dumps(route, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
