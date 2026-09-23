"""Pre-registered metrics for Jev replay v2. Thresholds come from the calibration half only. Aggregates only."""
import collections
import json
import math
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def wilson(k, n, z=1.96):
    if not n:
        return [None, None]
    p = k / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0, center - half), 4), round(min(1, center + half), 4)]


def pct(values, q):
    values = sorted(v for v in values if isinstance(v, (int, float)))
    return values[min(len(values) - 1, int(math.ceil(q * len(values))) - 1)] if values else None


def auto_continue(row, t):
    a = row.get('answers') or {}
    if not a:
        return False
    rt = a.get('request_type', {})
    return (rt.get('choice') in {'technical_problem', 'status_or_progress'} and (rt.get('confidence') or 0) >= t
            and (a.get('can_continue_alone', {}).get('noul') or 0) >= t
            and (a.get('needs_business_owner', {}).get('noul') if a.get('needs_business_owner') else 1) <= .2)


def front1(rows):
    cal = [r for r in rows if r['split'] == 'calibration']
    test = [r for r in rows if r['split'] == 'test']
    grid = [round(.50 + .05 * i, 2) for i in range(10)]
    table, chosen = [], None
    for t in grid:
        auto = [r for r in cal if auto_continue(r, t)]
        err = sum(r['principal_action'] != 'continue' for r in auto)
        table.append({'T': t, 'auto': len(auto), 'errors': err, 'error_rate': round(err / len(auto), 4) if auto else None})
        if chosen is None and auto and err / len(auto) <= .05:
            chosen = t
    result = {'calibration_cases': len(cal), 'test_cases': len(test), 'calibration_grid': table, 'chosen_T': chosen,
              'calibration_base_rate_continue': round(sum(r['principal_action'] == 'continue' for r in cal) / max(1, len(cal)), 4),
              'fallbacks_test': sum(1 for r in test if not r.get('answers')),
              'jev_ms_p50': pct([r.get('jev_ms') for r in rows], .5), 'jev_ms_p95': pct([r.get('jev_ms') for r in rows], .95)}
    if chosen is None:
        result.update(passes=False, reason='no threshold reaches <=5% error on calibration')
        return result
    auto = [r for r in test if auto_continue(r, chosen)]
    err = sum(r['principal_action'] != 'continue' for r in auto)
    correct = [r for r in auto if r['principal_action'] == 'continue']
    lost = sum(r['principal_answer_len'] > 200 for r in correct)
    rate = err / len(auto) if auto else None
    upper = wilson(err, len(auto))[1]
    coverage = len(auto) / len(test) if test else 0
    checks = {'error_le_5pct': rate is not None and rate <= .05, 'wilson_upper_le_10pct': upper is not None and upper <= .10,
              'coverage_ge_20pct': coverage >= .20, 'jev_p95_le_1s': (result['jev_ms_p95'] or 1e9) <= 1000}
    result.update(test_auto=len(auto), test_errors=err, test_error_rate=round(rate, 4) if rate is not None else None,
                  test_error_wilson95=wilson(err, len(auto)), test_coverage=round(coverage, 4),
                  secondary_correct_with_specific_principal_instruction=f'{lost}/{len(correct)}',
                  test_errors_by_action=dict(collections.Counter(r['principal_action'] for r in auto if r['principal_action'] != 'continue')),
                  checks=checks, passes=all(checks.values()))
    return result


def front2(rows):
    cal = [r for r in rows if r['split'] == 'calibration']
    test = [r for r in rows if r['split'] == 'test']
    freq = collections.Counter(l for r in cal for l in r['labels'])
    baseline_skill = freq.most_common(1)[0][0] if freq else None
    baseline_precision = sum(baseline_skill in r['labels'] for r in test) / max(1, len(test))
    suggested = [r for r in test if r.get('suggestion')]
    hits = sum(r['suggestion'] in r['labels'] for r in suggested)
    precision = hits / len(suggested) if suggested else None
    coverage = len(suggested) / max(1, len(test))
    ms = [m for r in rows for m in (r.get('jev_ms') or []) if isinstance(m, (int, float))]
    checks = {'precision_ge_60pct': precision is not None and precision >= .60,
              'beats_baseline_by_15pts': precision is not None and precision - baseline_precision >= .15,
              'coverage_ge_30pct': coverage >= .30}
    return {'calibration_cases': len(cal), 'test_cases': len(test), 'baseline_skill_rank1_share_calibration': freq.most_common(3),
            'baseline_precision_test': round(baseline_precision, 4),
            'test_suggested': len(suggested), 'test_hits': hits, 'test_precision': round(precision, 4) if precision is not None else None,
            'test_precision_wilson95': wilson(hits, len(suggested)), 'test_coverage': round(coverage, 4),
            'no_suggestion_reasons_test': dict(collections.Counter(r.get('reason') for r in test if not r.get('suggestion'))),
            'avg_labels_per_card': round(sum(len(r['labels']) for r in rows) / max(1, len(rows)), 2),
            'jev_ms_p50': pct(ms, .5), 'jev_ms_p95': pct(ms, .95), 'checks': checks, 'passes': all(checks.values())}


def main(path, out_path):
    rows = [json.loads(line) for line in open(path, encoding='utf-8')]
    report = {'front1_impediment': front1([r for r in rows if r['front'] == 'front1']),
              'front2_skill_suggestion': front2([r for r in rows if r['front'] == 'front2']),
              'front3': 'cancelled before paid calls (protocol amendment 2c3d0e84)',
              'cost_known_usd': round(sum(r.get('cost') or 0 for r in rows), 6), 'rows': len(rows)}
    json.dump(report, open(out_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
