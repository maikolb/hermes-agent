"""Label availability check for fronts 2 and 3 (counts only, no content, no Jev calls)."""
import hashlib
import json
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
DB = '/srv/hermes/profiles/hermes-project-factory/state.db'
SINCE = 1788825600  # 2026-09-08


def failed(content):
    if not isinstance(content, str):
        return False
    try:
        data = json.loads(content)
    except ValueError:
        return bool(re.match(r'\s*(error|traceback|fatal)', content, re.I))
    if isinstance(data, dict):
        if data.get('error'):
            return True
        code = data.get('exit_code')
        return isinstance(code, int) and code != 0
    return False


con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
sessions = con.execute("SELECT id, title FROM sessions WHERE source='kanban' AND started_at>=? ORDER BY started_at", (SINCE,)).fetchall()
skill_sessions = skill_calls = total_calls = failures = retried = retried_ok = 0
per_tool = {}
for sid, title in sessions:
    calls = {}
    order = []
    for (tc,) in con.execute("SELECT tool_calls FROM messages WHERE session_id=? AND role='assistant' AND tool_calls IS NOT NULL ORDER BY id", (sid,)):
        try:
            items = json.loads(tc)
        except (TypeError, ValueError):
            continue
        for item in items or []:
            fn = (item or {}).get('function') or {}
            key = hashlib.sha256((str(fn.get('name')) + '|' + str(fn.get('arguments'))).encode()).hexdigest()
            calls[item.get('id')] = (fn.get('name'), key)
            order.append(item.get('id'))
    results = {}
    for cid, content in con.execute("SELECT tool_call_id, content FROM messages WHERE session_id=? AND role='tool' ORDER BY id", (sid,)):
        results[cid] = failed(content)
    names = [calls[c][0] for c in order if c in calls]
    total_calls += len(names)
    n_skill = sum(1 for n in names if n == 'skill_view')
    skill_calls += n_skill
    skill_sessions += n_skill > 0
    seq = [(c, calls[c][0], calls[c][1], results.get(c)) for c in order if c in calls and c in results]
    for i, (cid, name, key, bad) in enumerate(seq):
        if not bad:
            continue
        failures += 1
        per_tool[name] = per_tool.get(name, 0) + 1
        nxt = next((s for s in seq[i + 1:i + 30] if s[2] == key), None)
        if nxt:
            retried += 1
            retried_ok += not nxt[3]
print(json.dumps({'kanban_sessions_since_0809': len(sessions), 'tool_calls': total_calls,
                  'sessions_with_skill_view': skill_sessions, 'skill_view_calls': skill_calls,
                  'tool_failures': failures, 'failures_retried_identically': retried,
                  'identical_retry_succeeded': retried_ok,
                  'failures_by_tool_top': sorted(per_tool.items(), key=lambda x: -x[1])[:8]}, ensure_ascii=False))
