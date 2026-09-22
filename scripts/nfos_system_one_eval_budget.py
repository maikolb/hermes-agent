"""Offline accounting guard for serial, explicitly funded System One evaluations.

No credential storage, provider call, retry, configuration change or activation.
Unknown billing consumes its reservation until independently reconciled.
"""
from decimal import Decimal, InvalidOperation
import json
import re


class BudgetUnavailable(RuntimeError):
    """A bounded operational reason safe to put in a decision receipt."""


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


def _process_alive(pid):
    import os
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == 'nt':
        try:
            import psutil
        except ImportError:
            return True  # Unknown owner: never settle a request that may still be live.
        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reconcile_dead_pending(ledger_path, *, alive=_process_alive):
    """Settle reservations left pending by a process that died mid-call.

    The reserved amount stays billed: a crash or an expiry never proves zero
    cost, and authorize() keeps bounding spend by the account counters. Only
    rows whose owner process is gone are touched; the in-flight lock is freed
    only when every pending row was settled this way.
    """
    import os
    import time
    from pathlib import Path
    path = Path(ledger_path)
    if not path.is_file():
        return 0
    ledger = json.loads(path.read_text(encoding='utf-8'))
    pending = [row for row in ledger.get('requests', []) if row.get('pending')]
    settled = 0
    for row in pending:
        if not alive(row.get('pid')):
            row.update(pending=False, billed=str(amount(row.get('reserved', 0))),
                       billing_state='RECONCILED_CONSERVATIVE_AFTER_CRASH', ended_at=time.time())
            settled += 1
    if settled:
        temporary = path.with_suffix(path.suffix + '.pending')
        temporary.write_text(safe_receipt(ledger), encoding='utf-8')
        os.replace(temporary, path)
        if settled == len(pending):
            path.with_suffix(path.suffix + '.lock').unlink(missing_ok=True)
    return settled


def guarded_call(cfg, payload, secret, send):
    """Opt-in operational evaluator guard used by the native adapter, including CLI children.

    One atomically reserved request at a time. Unknown charges keep their reserve;
    timeout in the caller cannot free the in-flight reservation. No key or source
    payload is stored. An existing ledger is required, never an implicit budget.
    """
    import hashlib
    import os
    from pathlib import Path
    import time
    import urllib.request
    require_paid_endpoint(cfg['endpoint'])
    policy = cfg['evaluation_budget']
    path = Path(policy['ledger_path'])
    if not path.is_absolute() or not path.is_file():
        raise BudgetUnavailable('budget_ledger_missing')
    # Exclusive create is portable and fails closed rather than queuing worker calls.
    lock = path.with_suffix(path.suffix + '.lock')
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise BudgetUnavailable('budget_request_in_flight') from exc
    row = None
    def account():
        values = {}
        for endpoint in ('key', 'credits'):
            req = urllib.request.Request('https://openrouter.ai/api/v1/' + endpoint,
                headers={'Authorization': 'Bearer ' + secret})
            with urllib.request.urlopen(req, timeout=cfg['timeout_seconds']) as response:
                values[endpoint] = json.load(response)['data']
        return {'key_usage': str(values['key']['usage']),
                'total_credits': str(values['credits']['total_credits']),
                'total_usage': str(values['credits']['total_usage'])}
    def save():
        temporary = path.with_suffix(path.suffix + '.pending')
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(safe_receipt(ledger))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    try:
        ledger = json.loads(path.read_text(encoding='utf-8'))
        current = account()
        tariff = ledger['tariff']
        try:
            reserved = reservation(len(payload['questions']), tariff['pricing'], tariff['context_length'])
            authorize(ledger['initial'], current, ledger['requests'], reserved, policy['limit_usd'])
        except ValueError as exc:
            raise BudgetUnavailable('budget_or_tariff_limit') from exc
        row = {'label': 'native-' + str(time.time_ns()), 'endpoint': cfg['endpoint'],
               'pending': True, 'reserved': str(reserved), 'before': current,
               'question_count': len(payload['questions']),
               'payload_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
               'pid': os.getpid(), 'started_at': time.time()}
        ledger['requests'].append(row)
        save()
        response = send(cfg, payload, secret)
        billed, state = charge_or_reserve({'data': response}, reserved)
        row.update(pending=False, billed=str(billed), billing_state=state, ended_at=time.time())
        save()
        return response
    except Exception:
        if row is not None:
            row.update(pending=False, billed=str(reserved), billing_state='RESERVED_UNKNOWN_BILLING', ended_at=time.time())
            save()
        raise
    finally:
        os.close(fd)
        lock.unlink()
