"""Read-only extraction for the pre-registered Jev replay v2 (docs/nfos/jev-replay-v2-protocolo.md)."""
import glob
import hashlib
import json
import os
import random
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
SINCE = 1788825600  # 2026-09-08T00:00:00Z
STATE_DB = '/srv/hermes/profiles/hermes-project-factory/state.db'
SKILLS_DIR = '/srv/hermes/profiles/hermes-project-factory/skills'
SEED = 20260923


def ro(path):
    con = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


def load(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def split(rows, key):
    rows.sort(key=lambda r: r[key])
    half = len(rows) // 2
    for i, r in enumerate(rows):
        r['split'] = 'calibration' if i < half else 'test'
    return rows


def failed(content):
    if not isinstance(content, str):
        return False
    data = load(content)
    if isinstance(data, dict):
        if data.get('error'):
            return True
        code = data.get('exit_code')
        return isinstance(code, int) and code != 0
    return bool(re.match(r'\s*(error|traceback|fatal)', content, re.I))


def boards():
    return sorted(glob.glob('/srv/hermes/kanban/boards/*/kanban.db'))


def front1():
    rows = []
    for db in boards():
        board = db.split('/')[-2]
        con = ro(db)
        try:
            found = con.execute(
                "SELECT id, question, context, action, answer, created_at FROM nfos_decisions WHERE kind='impediment' "
                "AND author='Principal' AND status IN ('resolved','human') AND action IN ('continue','changes','human') "
                "AND created_at>=? AND question NOT LIKE '%judge error: RateLimitError%'", (SINCE,)).fetchall()
        except sqlite3.Error:
            found = []
        for r in found:
            ctx = load(r['context']) or {}
            rows.append({'case_id': board + '/' + r['id'], 'board': board, 'at': r['created_at'],
                         'worker_question': (r['question'] or '')[:4000],
                         'requested_block_kind': ctx.get('requested_block_kind') if isinstance(ctx, dict) else None,
                         'principal_action': r['action'], 'principal_answer_len': len(r['answer'] or '')})
        con.close()
    return split(rows, 'at')


def catalog():
    items = {}
    for path in glob.glob(os.path.join(SKILLS_DIR, '**', 'SKILL.md'), recursive=True):
        if '/.archive/' in path:
            continue
        text = open(path, encoding='utf-8', errors='replace').read()
        name = re.search(r'^name:\s*["\']?([^"\'\n]+)', text, re.M)
        desc = re.search(r'^description:\s*["\']?(.+?)["\']?\s*$', text, re.M)
        key = os.path.basename(os.path.dirname(path))
        body = re.sub(r'^---.*?---\s*', '', text, flags=re.S)
        items[key] = {'name': (name.group(1).strip() if name else key), 'description': (desc.group(1).strip() if desc else '')[:300],
                      'full': ((desc.group(1).strip() + '\n\n') if desc else '') + body[:1500]}
    return items


def task_index():
    index = {}
    for db in boards():
        con = ro(db)
        try:
            for r in con.execute("SELECT w.task_id, r.payload FROM nfos_workflows w JOIN nfos_requests r ON r.id=w.request_id"):
                payload = load(r['payload']) or {}
                index[r['task_id']] = {'board': db.split('/')[-2], 'request': (payload.get('text') or '') if isinstance(payload, dict) else ''}
        except sqlite3.Error:
            pass
        con.close()
    return index


def session_calls(con, sid):
    calls, order = {}, []
    for (tc,) in con.execute("SELECT tool_calls FROM messages WHERE session_id=? AND role='assistant' AND tool_calls IS NOT NULL ORDER BY id", (sid,)):
        for item in load(tc) or []:
            if not isinstance(item, dict) or item.get('id') in calls:
                continue
            fn = item.get('function') or {}
            calls[item['id']] = (fn.get('name'), fn.get('arguments') if isinstance(fn.get('arguments'), str) else json.dumps(fn.get('arguments')))
            order.append(item['id'])
    results = {}
    for cid, content in con.execute("SELECT tool_call_id, content FROM messages WHERE session_id=? AND role='tool' ORDER BY id", (sid,)):
        if cid not in results:
            results[cid] = content
    return [(cid,) + calls[cid] + (results.get(cid),) for cid in order]


def fronts_2_3(tasks):
    con = ro(STATE_DB)
    sessions = con.execute("SELECT id, title, started_at FROM sessions WHERE source='kanban' AND started_at>=? ORDER BY started_at", (SINCE,)).fetchall()
    first_session, f3 = {}, []
    for s in sessions:
        m = re.search(r'kanban task (t_[0-9a-f]+)', s['title'] or '')
        seq = session_calls(con, s['id'])
        if m and m.group(1) not in first_session:
            first_session[m.group(1)] = (s['id'], s['started_at'], seq)
        keyed = [(cid, name, args, res, hashlib.sha256((str(name) + '|' + str(args)).encode()).hexdigest()) for cid, name, args, res in seq if res is not None]
        for i, (cid, name, args, res, key) in enumerate(keyed):
            if not failed(res):
                continue
            nxt = next((k for k in keyed[i + 1:i + 31] if k[4] == key), None)
            if nxt:
                f3.append({'case_id': s['id'] + '/' + cid, 'at': s['started_at'], 'tool': name,
                           'arguments': (args or '')[:1500], 'error_output': (res or '')[-2000:],
                           'retry_ok': not failed(nxt[3])})
    con.close()
    f2 = []
    for task_id, (sid, started, seq) in first_session.items():
        info = tasks.get(task_id)
        if not info or not info['request'].strip():
            continue
        loaded = []
        for cid, name, args, res in seq:
            if name != 'skill_view':
                continue
            parsed = load(args) or {}
            skill = parsed.get('name') if isinstance(parsed, dict) else None
            if skill:
                loaded.append(os.path.basename(str(skill).rstrip('/')))
            if len(loaded) >= 10:
                break
        if loaded:
            f2.append({'case_id': task_id, 'board': info['board'], 'at': started, 'request': info['request'][:6000],
                       'labels': sorted(set(loaded))})
    split(f2, 'at')
    split(f3, 'at')
    rng = random.Random(SEED)
    sample = []
    for part in ('calibration', 'test'):
        pool = [r for r in f3 if r['split'] == part]
        sample += rng.sample(pool, min(300, len(pool)))
    return f2, sample, len(f3)


def main():
    f1 = front1()
    cat = catalog()
    tasks = task_index()
    f2, f3, f3_pool = fronts_2_3(tasks)
    for name, rows in (('front1.jsonl', f1), ('front2.jsonl', f2), ('front3.jsonl', f3)):
        with open(name, 'w', encoding='utf-8') as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    json.dump(cat, open('catalog.json', 'w', encoding='utf-8'), ensure_ascii=False)
    count = lambda rows, part: sum(r['split'] == part for r in rows)
    print(json.dumps({
        'front1': {'total': len(f1), 'calibration': count(f1, 'calibration'), 'test': count(f1, 'test'),
                   'actions': {a: sum(r['principal_action'] == a for r in f1) for a in ('continue', 'changes', 'human')}},
        'front2': {'total': len(f2), 'calibration': count(f2, 'calibration'), 'test': count(f2, 'test'),
                   'catalog_skills': len(cat), 'labels_in_catalog': sum(any(l in cat for l in r['labels']) for r in f2)},
        'front3': {'pool': f3_pool, 'sampled': len(f3), 'calibration': count(f3, 'calibration'), 'test': count(f3, 'test'),
                   'retry_ok_rate_sample': round(sum(r['retry_ok'] for r in f3) / max(1, len(f3)), 3)},
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
