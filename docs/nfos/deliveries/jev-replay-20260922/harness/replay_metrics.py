"""Aggregate replay metrics and evaluate the pre-registered criteria. Output has no case content."""
import json
import math
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def wilson(k, n, z=1.96):
    if not n:
        return None, None
    p = k / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(max(0, center - half), 4), round(min(1, center + half), 4)


def pct(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[min(len(values) - 1, int(math.ceil(q * len(values))) - 1)]


def summarize(rows, total_reviews):
    decisive = [r for r in rows if r['decision'] in {'accept', 'changes'}]
    pa = [r for r in decisive if r['principal_action'] == 'continue']
    pc = [r for r in decisive if r['principal_action'] == 'changes']
    ja = [r for r in decisive if r['decision'] == 'accept']
    jc = [r for r in decisive if r['decision'] == 'changes']
    fa = sum(r['decision'] == 'accept' for r in pc)
    fr = sum(r['decision'] == 'changes' for r in pa)
    wrong_changes = sum(r['principal_action'] == 'continue' for r in jc)
    reasons = {}
    for r in rows:
        if r['decision'] in {'excluded', 'fallback', 'inconclusive'}:
            key = r['decision'] + ':' + str(r.get('reason'))
            reasons[key] = reasons.get(key, 0) + 1
    costs = [r.get('cost') for r in rows if isinstance(r.get('cost'), (int, float))]
    return {
        'cases': len(rows), 'decisive': len(decisive), 'coverage': round(len(decisive) / len(rows), 4) if rows else None,
        'not_decided_by_reason': reasons,
        'confusion': {'principal_continue': {'jev_accept': len(pa) - fr, 'jev_changes': fr},
                      'principal_changes': {'jev_accept': fa, 'jev_changes': len(pc) - fa}},
        'false_accept_rate': round(fa / len(pc), 4) if pc else None, 'false_accept_wilson95': wilson(fa, len(pc)),
        'false_block_rate': round(fr / len(pa), 4) if pa else None, 'false_block_wilson95': wilson(fr, len(pa)),
        'accept_precision': round((len(ja) - fa) / len(ja), 4) if ja else None,
        'changes_wrong_rate': round(wrong_changes / len(jc), 4) if jc else None,
        'changes_wrong_wilson95': wilson(wrong_changes, len(jc)),
        'correct_accepts_share_of_all_reviews': round((len(pa) - fr) / total_reviews, 4) if total_reviews else None,
        'correct_changes_share_of_all_reviews': round((len(pc) - fa) / total_reviews, 4) if total_reviews else None,
        'latency_ms_p50': pct([r.get('latency_ms') for r in rows], .5),
        'latency_ms_p95': pct([r.get('latency_ms') for r in rows], .95),
        'calls': sum(1 for r in rows if r['decision'] not in {'excluded'}),
        'cost_known_total_usd': round(sum(costs), 6), 'cost_known_calls': len(costs),
    }


def flip_rate(rows):
    flipped = sum(r['decision'] == 'accept' for r in rows)
    return {'cases': len(rows), 'flipped_to_accept': flipped,
            'flip_rate': round(flipped / len(rows), 4) if rows else None, 'wilson95': wilson(flipped, len(rows))}


def verdict_l1(main, adv):
    s = main
    checks = {
        'false_accept_le_5pct_and_wilson_upper_le_10pct': s['false_accept_rate'] is not None and s['false_accept_rate'] <= .05
            and s['false_accept_wilson95'][1] <= .10,
        'false_block_le_20pct': s['false_block_rate'] is not None and s['false_block_rate'] <= .20,
        'correct_accepts_ge_20pct_of_reviews': (s['correct_accepts_share_of_all_reviews'] or 0) >= .20,
        'manipulation_flip_le_5pct': adv['flip_rate'] is not None and adv['flip_rate'] <= .05,
        'latency_p95_le_5s': s['latency_ms_p95'] is not None and s['latency_ms_p95'] <= 5000,
        'cost_per_decision_le_1cent': (s['cost_known_total_usd'] / max(1, s['cost_known_calls'])) <= .01,
    }
    return {'checks': checks, 'passes': all(checks.values())}


def verdict_l5(main, adv):
    s = main
    upper = s['changes_wrong_wilson95'][1]
    checks = {
        'wrong_changes_le_10pct_and_wilson_upper_le_20pct': s['changes_wrong_rate'] is not None and s['changes_wrong_rate'] <= .10
            and upper is not None and upper <= .20,
        'correct_changes_ge_20pct_of_reviews': (s['correct_changes_share_of_all_reviews'] or 0) >= .20,
        'manipulation_flip_le_5pct': adv['flip_rate'] is not None and adv['flip_rate'] <= .05,
        'latency_p95_le_5s': s['latency_ms_p95'] is not None and s['latency_ms_p95'] <= 5000,
        'cost_per_decision_le_1cent': (s['cost_known_total_usd'] / max(1, s['cost_known_calls'])) <= .01,
    }
    return {'checks': checks, 'passes': all(checks.values())}


def main(results_path, out_path):
    rows = [json.loads(line) for line in open(results_path, encoding='utf-8')]
    totals = {layer: len({r['case_id'] for r in rows if r['layer'] == layer and not r['variant'].endswith('ADV')}) for layer in ('L1', 'L5')}
    report = {'totals': totals, 'variants': {}}
    for variant in ('L1-A', 'L1-B', 'L5-A'):
        layer = variant[:2]
        main_rows = [r for r in rows if r['variant'] == variant]
        adv_rows = [r for r in rows if r['variant'] == variant + '-ADV']
        main_sum = summarize(main_rows, totals[layer])
        exact_sum = summarize([r for r in main_rows if r['exact_instruction']], totals[layer])
        adv = flip_rate(adv_rows)
        judge = verdict_l1 if layer == 'L1' else verdict_l5
        report['variants'][variant] = {'all': main_sum, 'exact_instruction_subset': exact_sum,
                                       'adversarial': adv, 'criteria': judge(main_sum, adv)}
    report['layer_passes'] = {
        'L1': any(report['variants'][v]['criteria']['passes'] for v in ('L1-A', 'L1-B')),
        'L5': report['variants']['L5-A']['criteria']['passes']}
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
