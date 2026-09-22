"""Read-only extraction of real Principal verdicts for the pre-registered Jev replay.

Only data that existed before each verdict is kept as input. The Principal's
answer text is never exported as input (only its action, as the reference).
"""
import glob
import json
import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
SINCE = 1789171200  # 2026-09-12T00:00:00Z; the 08-11/09 incident window is excluded.
ALLOWED_PROJECT_KEYS = {'delivery_environment', 'delivery_destination', 'restrictions', 'limits', 'scope'}


def load(value):
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    try:
        return json.loads(text)
    except ValueError:
        return text


def main(out_path):
    cases = []
    for db in sorted(glob.glob('/srv/hermes/kanban/boards/*/kanban.db')):
        board = db.split('/')[-2]
        con = sqlite3.connect('file:' + db + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT d.id, d.task_id, d.kind, d.action, d.created_at, d.spec_revision, d.context,"
                " t.body, t.instruction_revision, w.request_id"
                " FROM nfos_decisions d JOIN tasks t ON t.id=d.task_id"
                " LEFT JOIN nfos_workflows w ON w.task_id=d.task_id"
                " WHERE d.kind IN ('spec_review','final_review') AND d.author='Principal'"
                " AND d.status='resolved' AND d.action IN ('continue','changes') AND d.created_at>=?"
                " ORDER BY d.created_at", (SINCE,)).fetchall()
        except sqlite3.Error:
            con.close()
            continue
        for r in rows:
            ident = (load(r['context']) or {}).get('acceptance_identity') or {}
            req = con.execute('SELECT payload FROM nfos_requests WHERE id=?', (r['request_id'],)).fetchone() if r['request_id'] else None
            payload = load(req['payload']) if req else {}
            payload = payload if isinstance(payload, dict) else {}
            spec_rev = ident.get('spec_revision') if r['kind'] == 'final_review' else (r['spec_revision'] or ident.get('spec_revision'))
            spec = con.execute("SELECT content FROM nfos_artifacts WHERE task_id=? AND kind='spec' AND revision=?",
                               (r['task_id'], spec_rev)).fetchone()
            report = None
            if r['kind'] == 'final_review':
                if ident.get('report_id') is not None:
                    report = con.execute("SELECT content FROM nfos_artifacts WHERE id=? AND task_id=?",
                                         (ident['report_id'], r['task_id'])).fetchone()
                if report is None and ident.get('report_revision') is not None:
                    report = con.execute("SELECT content FROM nfos_artifacts WHERE task_id=? AND kind='report' AND revision=?",
                                         (r['task_id'], ident['report_revision'])).fetchone()
            exact = ident.get('instruction_revision') == r['instruction_revision']
            cases.append({
                'case_id': board + '/' + r['id'], 'board': board, 'task_id': r['task_id'],
                'layer': 'L1' if r['kind'] == 'spec_review' else 'L5',
                'principal_action': r['action'], 'decided_at': r['created_at'],
                'original_request': payload.get('text') or '',
                'attachments': bool(payload.get('attachments')),
                'project_constraints': {k: v for k, v in (payload.get('project') or {}).items() if k in ALLOWED_PROJECT_KEYS},
                'current_instruction': r['body'] if exact else None, 'exact_instruction': exact,
                'spec': load(spec['content']) if spec else None,
                'report': load(report['content']) if report else None,
            })
        con.close()
    with open(out_path, 'w', encoding='utf-8') as fh:
        for case in cases:
            fh.write(json.dumps(case, ensure_ascii=False) + '\n')
    summary = {}
    for c in cases:
        key = c['layer'] + ':' + c['principal_action']
        summary[key] = summary.get(key, 0) + 1
    print(json.dumps({'cases': len(cases), 'by_layer_action': summary,
                      'attachments': sum(c['attachments'] for c in cases),
                      'exact_instruction': sum(c['exact_instruction'] for c in cases),
                      'missing_spec': sum(c['spec'] is None for c in cases),
                      'missing_report_L5': sum(c['layer'] == 'L5' and c['report'] is None for c in cases)}))


if __name__ == '__main__':
    main(sys.argv[1])
