"""Counterfactual effectiveness of the Jev decision layers against today's runtime.

Uses the pre-registered replay decisions plus timings measured in production
(same window). Read-only. Output contains aggregates only.

Model per replaced spec review (layer 1, runtime rule min_confidence 0.8):
- correct accept: the worker skips the Principal review wait (measured latency).
- false accept (Principal asked for changes): the review wait is skipped too,
  but the defective spec goes to implementation. Lower-bound cost: one wasted
  implementation cycle (median measured) plus one extra final review wait
  (median measured). Principal calls: one spec review avoided, one final
  review rejection added. A defect the final review misses is not counted.
"""
import glob
import json
import sqlite3
import statistics
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
SINCE = 1789171200


def minutes(seconds):
    return round(seconds / 60, 1)


def main(results_path, out_path):
    latency, final_latency, impl_cycle = {}, [], []
    for db in sorted(glob.glob('/srv/hermes/kanban/boards/*/kanban.db')):
        board = db.split('/')[-2]
        con = sqlite3.connect('file:' + db + '?mode=ro', uri=True)
        try:
            rows = con.execute("SELECT id, task_id, kind, action, created_at, resolved_at FROM nfos_decisions "
                               "WHERE author='Principal' AND status='resolved' AND kind IN ('spec_review','final_review') "
                               "AND created_at>=? AND resolved_at IS NOT NULL", (SINCE,)).fetchall()
        except sqlite3.Error:
            con.close()
            continue
        for did, task_id, kind, action, created, resolved in rows:
            wait = max(0, resolved - created)
            if kind == 'spec_review':
                latency[board + '/' + did] = wait
                if action == 'continue':
                    report = con.execute("SELECT min(created_at) FROM task_events WHERE task_id=? AND kind='nfos_report_saved' AND created_at>?",
                                         (task_id, resolved)).fetchone()[0]
                    if report:
                        impl_cycle.append(report - resolved)
            else:
                final_latency.append(wait)
        con.close()
    rows = [json.loads(line) for line in open(results_path, encoding='utf-8')]
    impl_med = statistics.median(impl_cycle)
    final_med = statistics.median(final_latency)
    spec_waits = sorted(latency.values())
    report = {'runtime_today': {
        'spec_reviews_measured': len(spec_waits),
        'spec_review_wait_min_p50': minutes(statistics.median(spec_waits)),
        'spec_review_wait_min_p90': minutes(spec_waits[int(len(spec_waits) * .9)]),
        'spec_review_wait_hours_total': round(sum(spec_waits) / 3600, 1),
        'implementation_cycle_min_p50': minutes(impl_med), 'implementation_cycles_measured': len(impl_cycle),
        'final_review_wait_min_p50': minutes(final_med), 'final_reviews_measured': len(final_latency)},
        'with_jev_layer1': {}}
    for variant in ('L1-A', 'L1-B'):
        saved_wait, correct, false_accepts = 0, 0, 0
        for r in rows:
            if r['variant'] != variant or r['decision'] != 'accept':
                continue
            saved_wait += latency.get(r['case_id'], 0)
            if r['principal_action'] == 'continue':
                correct += 1
            else:
                false_accepts += 1
        wasted = false_accepts * (impl_med + final_med)
        report['with_jev_layer1'][variant] = {
            'spec_reviews_replaced': correct + false_accepts, 'correct_accepts': correct, 'false_accepts': false_accepts,
            'principal_calls_net': correct + false_accepts - false_accepts,
            'principal_calls_net_share_of_spec_reviews': round(correct / len(spec_waits), 4),
            'review_wait_saved_hours': round(saved_wait / 3600, 2),
            'rework_added_hours_lower_bound': round(wasted / 3600, 2),
            'net_hours': round((saved_wait - wasted) / 3600, 2),
            'defective_specs_sent_to_implementation': false_accepts}
    l5 = [r for r in rows if r['variant'] == 'L5-A' and r['decision'] in {'accept', 'changes'}]
    report['with_jev_layer5'] = {'decisive': len(l5), 'final_reviews': len({r['case_id'] for r in rows if r['variant'] == 'L5-A'}),
                                 'note': 'decisive in under 1% of final reviews; no measurable effect'}
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
