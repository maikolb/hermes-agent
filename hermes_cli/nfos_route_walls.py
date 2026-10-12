"""A provider route closed by a long usage window (ROUTE_WALL_20261012).

A provider refuses a call for two different things. A request-rate throttle clears in seconds or a few minutes
and the client's own retry outlasts it. A usage window (weekly, monthly, five hours) does not: on 2026-10-11 one
worker spent 6 h 46 min repeating its turn on the opencode-go weekly limit, and after the executor learned to
leave at the wall (exit code 75) each launch still slept through two capped Retry-After waits holding a slot,
and the respawn guard launched it again on the same wall.

The provider says which one it is in structured fields of its answer, and only those are read here: the name of
the limit (``metadata.limitName``), the reset time (``resets_at`` / ``reset_at`` / ``resets_in_seconds`` /
``retry_after`` of the body, the ``Retry-After`` header). No message text is interpreted.

* ``quota_window`` is the verdict. A reset further away than the longest wait the client does for one attempt
  (``LONG_WINDOW_FLOOR_SECONDS``) cannot be outlasted inside the turn: the capped sleep ends before the window
  does. A named limit with no reset time is a window whose end is unknown. Anything else stays what it was.
* ``on_quota_error`` is the conversation loop's single question: leave the turn on this attempt? A dispatcher-owned
  kanban worker also closes its route here and sends the one notice of this wall through the event the runtime
  already uses for a model switch (``model_fallback``).
* A closed route is one file under the shared kanban root, so every board and every profile of the host sees it.
  The dispatcher asks ``closed`` before it claims a card: while the route is closed the card is not launched,
  holds no slot, counts no failure and stands in the wait registry (``nfos_waits``, reason ``provider_quota``).
* The route reopens at the reset time the provider gave. With none, and whenever a reset time passes with the
  wall still up, one launch probes it: a worker on the wall leaves on its first refused call. The spacing is the
  respawn guard's own (the rate-limit cooldown, doubling up to one hour), kept once for the route instead of
  once per card. A worker the provider answers reopens the route (``answered``).

Nothing here selects another provider or model: a closed route makes its cards wait.
"""
import hashlib
import json
import logging
import os
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# The cap the conversation loop puts on one Retry-After wait. A reset beyond it is a window, not a throttle.
LONG_WINDOW_FLOOR_SECONDS = 600
# Probe spacing ceiling, the respawn guard's own (QUOTA_BACKOFF_20260910).
PROBE_CEILING_SECONDS = 3600
# A probe that neither met the wall again nor got an answer in this time gave no verdict: another card may probe.
PROBE_LEASE_SECONDS = 900
# Wait registry: how often a held card's wait is rechecked and when it stops counting as a healthy wait.
WAIT_RECHECK_SECONDS = 300
WAIT_DEADLINE_SECONDS = 35 * 86400

ROUTE_ENV = 'HERMES_KANBAN_ROUTE'
_ABSOLUTE_FIELDS = ('resets_at', 'reset_at')
_RELATIVE_FIELDS = ('resets_in_seconds', 'retry_after')


# ── What the provider said ──────────────────────────────────────────────

def _payloads(error):
    """The JSON the provider answered and its ``error`` object, from the exception and its causes.

    The OpenAI SDK keeps only the inner ``error`` object in ``.body``; the whole answer, where opencode-go puts
    ``metadata``, is in ``.response``."""
    found, current = [], error
    for _ in range(5):
        response = getattr(current, 'response', None)
        if callable(getattr(response, 'json', None)):
            try:
                found.append(response.json())
            except Exception:  # noqa: BLE001 - a body that is not JSON carries no field to read
                pass
        found.append(getattr(current, 'body', None))
        cause = getattr(current, '__cause__', None) or getattr(current, '__context__', None)
        if cause is None or cause is current:
            break
        current = cause
    out = []
    for body in found:
        if isinstance(body, dict):
            out.append(body)
            if isinstance(body.get('error'), dict):
                out.append(body['error'])
    return out


def _headers(error):
    current = error
    for _ in range(5):
        headers = getattr(getattr(current, 'response', None), 'headers', None)
        if headers and hasattr(headers, 'get'):
            return headers
        cause = getattr(current, '__cause__', None) or getattr(current, '__context__', None)
        if cause is None or cause is current:
            break
        current = cause
    return {}


def _absolute(value):
    """Epoch seconds from epoch seconds, epoch milliseconds or an ISO-8601 string."""
    if isinstance(value, bool) or value in (None, ''):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        try:
            from datetime import datetime
            return datetime.fromisoformat(str(value).strip().replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    if number <= 0:
        return None
    return number / 1000.0 if number > 1_000_000_000_000 else number


def _relative(value, now):
    if isinstance(value, bool) or value in (None, ''):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return now + seconds if seconds > 0 else None


def _reset_at(error, payloads, now):
    for payload in payloads:
        for field in _ABSOLUTE_FIELDS:
            found = _absolute(payload.get(field))
            if found is not None:
                return found
        for field in _RELATIVE_FIELDS:
            found = _relative(payload.get(field), now)
            if found is not None:
                return found
    headers = _headers(error)
    raw = headers.get('retry-after') or headers.get('Retry-After')
    if raw in (None, ''):
        return None
    found = _relative(raw, now)
    if found is not None:
        return found
    try:  # RFC 9110: Retry-After is delay seconds or an HTTP date
        return parsedate_to_datetime(str(raw)).timestamp()
    except (TypeError, ValueError):
        return None


def _limit_name(payloads):
    for payload in payloads:
        metadata = payload.get('metadata')
        name = metadata.get('limitName') if isinstance(metadata, dict) else None
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _error_kind(payloads):
    for payload in payloads:
        for field in ('code', 'type'):
            kind = payload.get(field)
            if isinstance(kind, str) and kind.strip() and kind.strip() != 'error':
                return kind.strip()
    return None


def quota_window(error, *, now=None):
    """``{'limit', 'reset_at'}`` when the provider tied this refusal to a long usage window, else ``None``.

    ``reset_at`` is epoch seconds, or ``None`` when the provider named the limit and gave no reset time."""
    now = time.time() if now is None else float(now)
    payloads = _payloads(error)
    name = _limit_name(payloads)
    reset_at = _reset_at(error, payloads, now)
    if reset_at is None:
        if not name:
            return None
    elif reset_at - now <= LONG_WINDOW_FLOOR_SECONDS:
        return None
    return {'limit': name or _error_kind(payloads) or 'retry-after', 'reset_at': reset_at}


# ── The closed routes of this host ──────────────────────────────────────

def _key(provider, model):
    return str(provider or '').strip().lower(), str(model or '').strip().lower()


def _dir():
    from hermes_cli.kanban_db import kanban_home
    return kanban_home() / 'kanban' / 'route_walls'


def _path(provider, model):
    provider, model = _key(provider, model)
    return _dir() / (hashlib.sha256(f'{provider}\n{model}'.encode()).hexdigest()[:24] + '.json')


def read(provider, model):
    """The wall closing this route, or ``None``."""
    try:
        record = json.loads(_path(provider, model).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def walls():
    """Every closed route of this host."""
    out = []
    try:
        files = sorted(_dir().glob('*.json'))
    except OSError:
        return out
    for file in files:
        try:
            record = json.loads(file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def any_closed():
    """Cheap gate for the dispatcher: is any route of this host closed at all?"""
    try:
        with os.scandir(_dir()) as entries:
            return any(entry.name.endswith('.json') for entry in entries)
    except OSError:
        return False


def _staged(record):
    path = _path(record['provider'], record['model'])
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f'{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp')
    staged.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    return staged, path


def _write(record):
    staged, path = _staged(record)
    os.replace(staged, path)


def _create(record):
    """Write the wall only if the route has none. One caller wins: that one sends the notice."""
    staged, path = _staged(record)
    try:
        os.link(staged, path)  # atomic, and readers never see a half-written wall
        return True
    except FileExistsError:
        return False
    except (OSError, NotImplementedError, AttributeError):
        try:
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), 'w', encoding='utf-8') as out:
                json.dump(record, out, ensure_ascii=False)
            return True
        except FileExistsError:
            return False
    finally:
        try:
            staged.unlink()
        except OSError:
            pass


def _probe_spacing(probes):
    """The respawn guard's schedule (cooldown doubling up to one hour), once for the route."""
    try:
        from hermes_cli.kanban_db import _resolve_rate_limit_cooldown_seconds
        first = int(_resolve_rate_limit_cooldown_seconds())
    except Exception:  # noqa: BLE001
        first = 0
    first = first if first > 0 else 300
    return min(PROBE_CEILING_SECONDS, first * (2 ** min(int(probes), 12)))


def open_at(wall):
    """When the route admits its next launch: the reset time the provider gave, never sooner than the probe
    spacing allows (a reset time that passed with the wall still up was not the end of the window)."""
    return max(float(wall.get('reset_at') or 0), float(wall.get('next_probe_at') or 0))


def close(provider, model, *, limit, reset_at=None, task_id=None, board=None, now=None):
    """A worker met a long usage window on this route. Returns ``(wall, opened)``; ``opened`` is true only for
    the call that closed an open route.

    On a route already closed nothing changes unless the caller is the probe: its refusal is the verdict of the
    probe, so the next one is scheduled and the provider's current reset time replaces the old one."""
    provider, model = _key(provider, model)
    if not provider or not model:
        return None, False
    now = time.time() if now is None else float(now)
    wall = {'provider': provider, 'model': model, 'limit': str(limit), 'reset_at': reset_at, 'closed_at': now,
            'walled_at': now, 'probes': 0, 'next_probe_at': now + _probe_spacing(0), 'probe': None,
            'closed_by': {'task_id': task_id, 'board': board}}
    if _create(wall):
        return wall, True
    existing = read(provider, model)
    if existing is None:
        return None, False
    if task_id and (existing.get('probe') or {}).get('task_id') == task_id:
        probes = int(existing.get('probes') or 0) + 1
        existing.update(probes=probes, probe=None, walled_at=now, limit=str(limit), reset_at=reset_at,
                        next_probe_at=now + _probe_spacing(probes))
        _write(existing)
    return existing, False


def reopen(provider, model):
    try:
        _path(provider, model).unlink()
        return True
    except OSError:
        return False


def closed(provider, model, *, now=None):
    """The wall holding launches on this route right now, or ``None``: the route is open, or its reopening is
    due and no probe is in flight, so the next launch is the probe (``take_probe``)."""
    wall = read(provider, model)
    if wall is None:
        return None
    now = time.time() if now is None else float(now)
    probe = wall.get('probe') or {}
    if probe and now - float(probe.get('at') or 0) < PROBE_LEASE_SECONDS:
        return wall
    return wall if now < open_at(wall) else None


def take_probe(provider, model, *, task_id, board=None, now=None):
    """The dispatcher claimed ``task_id`` on a closed route whose reopening is due: that launch is the probe."""
    wall = read(provider, model)
    if wall is None:
        return None
    wall['probe'] = {'task_id': task_id, 'board': board, 'at': time.time() if now is None else float(now)}
    _write(wall)
    return wall


# ── The worker's side ───────────────────────────────────────────────────

def _owns_card():
    """This execution is the dispatcher's own worker of a card (not a delegated child, not an in-process cron)."""
    if not os.environ.get('HERMES_KANBAN_TASK', '').strip():
        return False
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, is_dispatcher_owned_worker_context
    return is_dispatcher_owned_worker_context() and os.environ.get(DELEGATED_CHILD_ENV_MARKER) != '1'


def worker_route(agent):
    """The route this worker is on: the one the dispatcher launched it with while it runs its primary model
    (the dispatcher's own spelling, so both sides name the same route), else what the agent switched to."""
    if not getattr(agent, '_fallback_activated', False):
        try:
            provider, model = json.loads(os.environ.get(ROUTE_ENV) or 'null') or (None, None)
        except (ValueError, TypeError):
            provider = model = None
        if provider and model:
            return _key(provider, model)
    return _key(getattr(agent, 'provider', ''), getattr(agent, 'model', ''))


def notice(wall):
    """The one message of a wall, for the operations channel of the card that met it."""
    route = f"{wall['provider']}/{wall['model']}"
    if wall.get('reset_at'):
        when = time.strftime('%d/%m %H:%M UTC', time.gmtime(float(wall['reset_at'])))
        reopening = f'com reinício informado para {when}. A rota reabre nessa hora.'
    else:
        first, ceiling = _probe_spacing(0) // 60, PROBE_CEILING_SECONDS // 60
        reopening = (f'sem hora de reinício informada. Um lançamento sonda a rota em {first} min e depois a intervalos '
                     f'que dobram até {ceiling} min; ela reabre quando o provedor responder.')
    return (f"Rota {route} fechada: o provedor recusou pelo limite {wall['limit']}, {reopening} "
            'Os cards dessa rota esperam sem ocupar vaga e sem contar falha. Nenhuma outra rota é usada no lugar.')


def on_quota_error(agent, error):
    """True when the provider tied this refusal to a long usage window: the turn leaves on this attempt.

    Called by the conversation loop for a rate-limit or billing refusal the credential pool cannot rotate past.
    When the caller is a card's own worker and no fallback provider is left to take the turn, the route is closed
    for the dispatcher and the worker that closed it sends the notice."""
    try:
        window = quota_window(error)
    except Exception:  # noqa: BLE001 - an answer that cannot be read is retried as before this rule
        logger.warning('ROUTE_WALL_20261012 could not read a quota refusal', exc_info=True)
        return False
    if window is None:
        return False
    try:
        if _owns_card() and not agent._has_pending_fallback():
            provider, model = worker_route(agent)
            wall, opened = close(provider, model, limit=window['limit'], reset_at=window['reset_at'],
                                 task_id=os.environ.get('HERMES_KANBAN_TASK', '').strip(),
                                 board=os.environ.get('HERMES_KANBAN_BOARD', '').strip() or None)
            if opened:
                logger.warning('ROUTE_WALL_20261012 route %s/%s closed by limit %s (reset_at=%s)',
                               provider, model, wall['limit'], wall['reset_at'])
                agent._record_worker_fallback_notice(notice(wall))
    except Exception:  # noqa: BLE001 - the verdict stands; a route left unmarked is launched on as before this rule
        logger.warning('ROUTE_WALL_20261012 could not close the route or send its notice', exc_info=True)
    return True


def answered(agent):
    """Called at the top of each loop iteration of a card's worker. Once the provider has answered a worker
    that began after the wall was last met, the route is open: remove the wall.

    An answered call is one the runtime counted (``session_api_calls``, which it raises for an answer that carries
    its token usage). A worker already in flight when the wall was met proves nothing with a late answer."""
    try:
        began = getattr(agent, '_route_wall_began', None)
        if began is None:
            agent._route_wall_began = time.time()  # first iteration, before this worker's first provider call
            return False
        if not getattr(agent, 'session_api_calls', 0) or not _owns_card():
            return False
        route = worker_route(agent)
        if getattr(agent, '_route_wall_answered', None) == route:
            return False
        agent._route_wall_answered = route
        wall = read(*route)
        if wall is None or float(wall.get('walled_at') or 0) >= began:
            return False
        if reopen(*route):
            logger.warning('ROUTE_WALL_20261012 route %s/%s reopened: the provider answered', *route)
            return True
    except Exception:  # noqa: BLE001
        logger.debug('ROUTE_WALL_20261012 answer check failed', exc_info=True)
    return False


# ── The dispatcher's side ───────────────────────────────────────────────

def _profile_route(assignee):
    """(provider, model) the assignee's profile runs when a card pins neither."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.config import load_config
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        home = resolve_profile_env(normalize_profile_name(assignee))
    except Exception:  # noqa: BLE001 - no such profile here: the launch resolves it, not this lookup
        return None, None
    token = set_hermes_home_override(home)
    try:
        model = (load_config() or {}).get('model')
    finally:
        reset_hermes_home_override(token)
    if isinstance(model, dict):
        return model.get('provider'), model.get('default') or model.get('model')
    return None, model if isinstance(model, str) else None


def card_route(conn, task):
    """The (provider, model) ``_default_spawn`` launches this card on, or ``None`` when it cannot be told."""
    from hermes_cli import nfos_delivery as delivery
    provider = model = None
    if delivery.get_workflow(conn, task.id):
        from hermes_cli.nfos_principal_review import worker_model_args
        args = worker_model_args(task, conn)
        model = args[args.index('-m') + 1] if '-m' in args else None
        provider = args[args.index('--provider') + 1] if '--provider' in args else None
    elif task.model_override:
        model, provider = task.model_override, task.provider_override
    if not (provider and model) and task.assignee:
        base_provider, base_model = _profile_route(task.assignee)
        provider, model = provider or base_provider, model or base_model
    return _key(provider, model) if provider and model else None


def hold(conn, task, *, dry_run=False):
    """The wall that keeps the dispatcher from launching this card now, or ``None``.

    A held card is declared once in the wait registry: it waits with a named reason, an executor that is not the
    card and a time to look again, instead of standing in ``ready`` with nothing to show for it."""
    route = card_route(conn, task)
    wall = closed(*route) if route else None
    if wall is None or dry_run:
        return wall
    from hermes_cli import nfos_delivery as delivery, nfos_waits
    label = f"{wall['provider']}/{wall['model']}"
    try:
        nfos_waits.declare(
            conn, task.id, None, reason='provider_quota', executor={'kind': 'external', 'name': label},
            predicate={'kind': 'route_open', 'provider': wall['provider'], 'model': wall['model']},
            subject=f"a rota {label} (limite {wall['limit']} do provedor)",
            evidence={'limit': wall['limit'], 'reset_at': wall.get('reset_at'), 'closed_at': wall.get('closed_at')})
    except delivery.WorkflowError:
        pass  # this card's wait already ran its whole deadline in this round: it stays held, no longer as a wait
    return wall


def probe_launched(conn, task, *, board=None):
    """The dispatcher claimed this card. On a route still closed that launch is the probe."""
    route = card_route(conn, task)
    if route and read(*route) is not None:
        take_probe(*route, task_id=task.id, board=board)


def spawn_route(conn, task):
    """``ROUTE_ENV`` value for a launch: the route as the dispatcher spells it, or ``None``."""
    route = card_route(conn, task)
    return json.dumps(list(route)) if route else None


# ── The wait registry's side (nfos_waits, reason provider_quota) ────────

def wait_check(conn, wait):
    """Predicate ``route_open``: true once the route has no wall."""
    predicate = wait['predicate']
    wall = read(predicate.get('provider'), predicate.get('model'))
    if wall is None:
        return True, {'route': 'open'}
    return False, {'route': 'closed', 'limit': wall.get('limit'), 'reset_at': wall.get('reset_at'),
                   'open_at': int(open_at(wall)), 'probes': int(wall.get('probes') or 0)}


def wait_expired(conn, wait, why, now):
    """A route still closed at the wait's deadline. The registry has written the outcome; the dispatcher keeps
    holding the card, now outside a healthy wait, which is what makes it show as stalled to whoever watches."""
    return None
