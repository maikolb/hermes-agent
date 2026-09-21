"""Offline accounting guard for serial, explicitly funded System One evaluations.

No credential storage, provider call, retry, configuration change or activation.
Unknown billing consumes its reservation until independently reconciled.
"""
from decimal import Decimal, InvalidOperation
import json
import re


def amount(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid billing amount') from exc
    if not result.is_finite() or result < 0:
        raise ValueError('Invalid billing amount')
    return result


def require_paid_endpoint(endpoint):
    if endpoint != 'https://openrouter.ai/api/v1/systemone':
        raise ValueError('Paid evaluator only permits the verified OpenRouter SystemOne endpoint')


def reservation(question_count, pricing, context_length):
    if not isinstance(question_count, int) or isinstance(question_count, bool) or not 1 <= question_count <= 32:
        raise ValueError('Unsupported question count')
    if context_length != 32000 or amount(pricing['prompt']) != Decimal('0.000000042') or amount(pricing['completion']) != 0:
        raise ValueError('Tariff changed; verify before spending')
    # Reserve the maximum per-question context, twice, plus a rounding margin.
    # Summed billed tokens are not the per-question context window.
    return Decimal(context_length) * question_count * amount(pricing['prompt']) * 2 + Decimal('.001')


def authorize(initial, current, rows, reserve, budget='2.88'):
    if any(row.get('pending') for row in rows):
        raise ValueError('Unsettled request; no concurrent paid call')
    key_delta = amount(current['key_usage']) - amount(initial['key_usage'])
    account_delta = amount(current['total_usage']) - amount(initial['total_usage'])
    if min(key_delta, account_delta) < 0:
        raise ValueError('Usage counter changed backwards; reconcile')
    billed = sum((amount(row.get('billed', 0)) for row in rows), Decimal(0))
    spent = max(key_delta, account_delta, billed)
    remaining = amount(current['total_credits']) - amount(current['total_usage'])
    if spent + amount(reserve) > amount(budget) or amount(reserve) > remaining:
        raise ValueError('Insufficient reserved headroom')
    return spent


def charge_or_reserve(response, reserved):
    cost = (response.get('data', {}).get('usage') or {}).get('cost')
    if cost is None:
        return amount(reserved), 'RESERVED_UNKNOWN_BILLING'
    cost = amount(cost)
    if cost > amount(reserved):
        raise ValueError('Provider billing exceeded reservation; stop')
    return cost, 'RESPONSE_BILLED'


def safe_receipt(value):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if re.search(r'sk-or-v1-[0-9a-f]{64}|Bearer\s+\S+', text, re.I):
        raise ValueError('Credential-like content cannot enter evidence')
    return text
