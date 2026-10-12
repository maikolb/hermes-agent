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
  kanban worker also closes its route here.
* A route is the profile a card is launched as, the provider and the model. The profile is part of it because the
  quota that ran out belongs to a credential, and the credential is the profile's: two profiles of one host on the
  same provider and model with an account each do not share a window, and the wall of one must not hold the cards
  of the other. The profile's name stands for its credential because the dispatcher has to name the route before
  it launches, and what identifies the credential a launch will use (which pool entry, the profile's or the
  root's, or a key in the environment) is only settled inside the worker, by reading the secret store. Two
  profiles that do share one account each meet the wall themselves: two walls, two notices, two probes. The
  account the provider names in its refusal (``metadata.workspace``) is kept on the wall for whoever reads it,
  and decides nothing.
* A closed route is one file under the shared kanban root, so every board of the host sees it and every
  dispatcher finds the wall of the profile it launches a card as.
  The dispatcher asks ``closed`` before it claims a card: while the route is closed the card is not launched,
  holds no slot, counts no failure and stands in the wait registry (``nfos_waits``, reason ``provider_quota``).
  That wait is the card's only while the route is what the card waits for: a card whose route changed, or that
  another hold took, loses it (``wait_withdrawn``).
* The route reopens at the reset time the provider gave. With none, and whenever a reset time passes with the
  wall still up, one launch probes it: a worker on the wall leaves on its first refused call. The spacing is the
  respawn guard's own (the rate-limit cooldown, doubling up to one hour), kept once for the route instead of
  once per card. A worker the provider answers reopens the route (``answered``). A route that closes again
  sooner than its next probe would have come is the same wall going on (``close``).
* One notice per wall, sent by the gateway's notifier to the management topic (``unannounced``, ``claim_notice``):
  a closed route is a fact of the host, and the card that met the wall may have no chat a passive notice is
  allowed in.
* The coordinating session's route closes the same way when it meets the window on the turn of a preserved
  request (PRINCIPAL_WALL_20261012, "The intake's side" below): the request goes back to the Principal's inbox and
  is offered again when the route reopens, its turn being the probe of a route with no reset time.

Nothing here selects another provider or model: a closed route makes its cards, and the requests whose decision
is the Principal's, wait.
"""
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

# The cap the conversation loop puts on one Retry-After wait. A reset beyond it is a window, not a throttle.
LONG_WINDOW_FLOOR_SECONDS = 600
# Probe spacing ceiling, the respawn guard's own (QUOTA_BACKOFF_20260910).
PROBE_CEILING_SECONDS = 3600
# A probe that neither met the wall again nor got an answer in this time gave no verdict: another card may probe.
# The same for a notifier that took a wall's notice and neither sent it nor let it go.
PROBE_LEASE_SECONDS = 900
# Wait registry: how often a held card's wait is rechecked and when it stops counting as a healthy wait.
WAIT_RECHECK_SECONDS = 300
WAIT_DEADLINE_SECONDS = 35 * 86400

ROUTE_ENV = 'HERMES_KANBAN_ROUTE'
# Beside a route's wall (``.json``): the wall it had when it last reopened, and the notice of its wall.
REOPENED, NOTICE = '.reopened', '.notice'
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
    """Epoch seconds from epoch seconds, epoch milliseconds or an ISO-8601 string (UTC when it names no zone:
    a provider's clock is not this host's)."""
    if isinstance(value, bool) or value in (None, ''):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        try:
            moment = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        except ValueError:
            return None
        return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()
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


def quota_account(error):
    """The account the provider says it refused (opencode-go: ``metadata.workspace``), or ``None``.

    Kept on the wall so a person can tell whose quota ran out. It is no part of the route and of no decision:
    the dispatcher cannot know it before a launch, and most providers do not say it."""
    for payload in _payloads(error):
        metadata = payload.get('metadata')
        account = metadata.get('workspace') if isinstance(metadata, dict) else None
        if isinstance(account, str) and account.strip():
            return account.strip()[:120]
    return None


# ── The closed routes of this host ──────────────────────────────────────

def _key(profile, provider, model):
    """A route: the profile the card is launched as (its credential), the provider and the model. The profile is
    spelled as ``profiles.normalize_profile_name`` spells it, which is what the launch passes to ``-p``."""
    return tuple(str(part or '').strip().lower() for part in (profile, provider, model))


def _route(wall):
    return wall.get('profile'), wall.get('provider'), wall.get('model')


def _dir():
    from hermes_cli.kanban_db import kanban_home
    return kanban_home() / 'kanban' / 'route_walls'


def _path(profile, provider, model, kind='.json'):
    """The file of this route: named by the route so a person can find it, and by its digest so two routes
    never share one."""
    profile, provider, model = _key(profile, provider, model)
    digest = hashlib.sha256(f'{profile}\n{provider}\n{model}'.encode()).hexdigest()[:12]
    readable = re.sub(r'[^a-z0-9._-]+', '_', f'{profile}--{provider}--{model}')[:120]
    return _dir() / f'{readable}.{digest}{kind}'


def _load(path):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def read(profile, provider, model):
    """The wall closing this route, or ``None``."""
    return _load(_path(profile, provider, model))


def walls():
    """Every closed route of this host."""
    try:
        files = sorted(_dir().glob('*.json'))
    except OSError:
        return []
    return [record for record in map(_load, files) if record and all(_route(record))]


def any_closed():
    """Cheap gate for the dispatcher: is any route of this host closed at all?"""
    try:
        with os.scandir(_dir()) as entries:
            return any(entry.name.endswith('.json') for entry in entries)
    except OSError:
        return False


def _staged(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f'{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp')
    staged.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    return staged


def _write(path, record):
    os.replace(_staged(path, record), path)


def _create(path, record):
    """Write the file only if it is not there. One caller wins."""
    staged = _staged(path, record)
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


def close(profile, provider, model, *, limit, reset_at=None, account=None, task_id=None, board=None,
          request_id=None, now=None):
    """A worker met a long usage window on this route. Returns ``(wall, opened)``; ``opened`` is true only for
    the call that closed an open route with a new wall. ``account`` is what the provider named in its refusal,
    kept as information. ``request_id`` stands where ``task_id`` does when it was the coordinating session that
    met the window, on the turn of that inbox entry (PRINCIPAL_WALL_20261012).

    A route that closes again by the same limit sooner after it reopened than its next probe would have waited
    had not reached the end of the window (a rolling window gives quota back a little at a time). It is the same
    wall going on: it keeps its ``closed_at``, which is what its notice is known by, and its schedule moves one
    step further instead of starting over.

    On a route already closed nothing changes unless the caller is the probe: its refusal is the verdict of the
    probe, so the next one is scheduled and the provider's current reset time replaces the old one."""
    route = profile, provider, model = _key(profile, provider, model)
    if not all(route):
        return None, False
    now = time.time() if now is None else float(now)
    wall = {'profile': profile, 'provider': provider, 'model': model, 'limit': str(limit), 'reset_at': reset_at,
            'account': account, 'closed_at': now, 'walled_at': now, 'probes': 0,
            'next_probe_at': now + _probe_spacing(0), 'probe': None,
            'closed_by': {'task_id': task_id, 'board': board}}
    if request_id:
        wall['closed_by']['request_id'] = request_id
    prober = task_id or request_id
    before = _load(_path(*route, REOPENED)) or {}
    probes = int(before.get('probes') or 0) + 1
    goes_on = (before.get('limit') == wall['limit'] and before.get('closed_at')
               and now - float(before.get('reopened_at') or 0) < _probe_spacing(probes))
    if goes_on:
        wall.update(closed_at=before['closed_at'], closed_by=before.get('closed_by') or wall['closed_by'],
                    probes=probes, next_probe_at=now + _probe_spacing(probes))
    if _create(_path(*route), wall):
        return wall, not goes_on
    existing = read(*route)
    if existing is None:
        return None, False
    if prober and (existing.get('probe') or {}).get('task_id') == prober:
        probes = int(existing.get('probes') or 0) + 1
        existing.update(probes=probes, probe=None, walled_at=now, limit=str(limit), reset_at=reset_at,
                        account=account or existing.get('account'), next_probe_at=now + _probe_spacing(probes))
        _write(_path(*route), existing)
    return existing, False


def reopen(profile, provider, model, *, now=None):
    """The provider answered: the route is open. The wall is put aside, not erased, so ``close`` can tell a
    route that closes again at once from a new wall."""
    route = _key(profile, provider, model)
    wall = read(*route)
    if wall is None:
        return False
    wall.update(reopened_at=time.time() if now is None else float(now), probe=None)
    try:
        _write(_path(*route, REOPENED), wall)
    except OSError:  # the route opens all the same; its next wall will be a new one
        logger.warning('ROUTE_WALL_20261012 could not keep the reopened wall of %s', label(wall), exc_info=True)
    try:
        _path(*route).unlink()
        return True
    except OSError:
        return False


def closed(profile, provider, model, *, now=None):
    """The wall holding launches on this route right now, or ``None``: the route is open, or its reopening is
    due and no probe is in flight, so the next launch is the probe (``take_probe``)."""
    wall = read(profile, provider, model)
    if wall is None:
        return None
    now = time.time() if now is None else float(now)
    probe = wall.get('probe') or {}
    if probe and now - float(probe.get('at') or 0) < PROBE_LEASE_SECONDS:
        return wall
    return wall if now < open_at(wall) else None


def take_probe(profile, provider, model, *, task_id, board=None, now=None):
    """The dispatcher claimed ``task_id`` on a closed route whose reopening is due: that launch is the probe."""
    wall = read(profile, provider, model)
    if wall is None:
        return None
    wall['probe'] = {'task_id': task_id, 'board': board, 'at': time.time() if now is None else float(now)}
    _write(_path(profile, provider, model), wall)
    return wall


def release_probe(profile, provider, model, *, task_id):
    """The launch that took the probe did not happen: the turn of probing goes back at once."""
    wall = read(profile, provider, model)
    if wall is not None and (wall.get('probe') or {}).get('task_id') == task_id:
        wall['probe'] = None
        _write(_path(profile, provider, model), wall)


def label(wall):
    """A route as logs and the dispatcher's diagnosis name it: ``<provider>/<model>@<profile>``."""
    return f"{wall.get('provider')}/{wall.get('model')}@{wall.get('profile')}"


# ── The one notice of a wall ────────────────────────────────────────────

def _span(seconds):
    minutes = max(1, round(float(seconds) / 60))
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    return ' '.join(f'{amount} {unit}' for amount, unit in ((days, 'd'), (hours, 'h'), (minutes, 'min')) if amount)


def notice(wall):
    """The one message of a wall, for the operations channel. It says whose route closed: another profile on the
    same provider and model goes on with its own credential."""
    route = f"{wall['provider']}/{wall['model']}"
    account = f" (conta {wall['account']})" if wall.get('account') else ''
    if wall.get('reset_at'):
        when = time.strftime('%d/%m %H:%M UTC', time.gmtime(float(wall['reset_at'])))
        asked = _span(float(wall['reset_at']) - float(wall.get('walled_at') or wall.get('closed_at') or 0))
        reopening = (f'com reinício informado para {when} (o provedor pediu {asked} de espera). '
                     'A rota reabre nessa hora.')
    else:
        first, ceiling = _probe_spacing(0) // 60, PROBE_CEILING_SECONDS // 60
        reopening = (f'sem hora de reinício informada. Um lançamento sonda a rota em {first} min e depois a intervalos '
                     f'que dobram até {ceiling} min; ela reabre quando o provedor responder.')
    # PRINCIPAL_WALL_20261012: the window was met by the coordinating session, on the turn of a request.
    intake = (' É a rota da sessão coordenadora (Principal): os pedidos que chegam pelo Telegram ficam gravados e a '
              'decisão de entrada de cada um é retomada, na ordem de chegada, quando a rota reabrir.'
              if (wall.get('closed_by') or {}).get('request_id') else '')
    return (f"Rota {route} fechada para o perfil {wall['profile']}: o provedor recusou pelo limite {wall['limit']}"
            f'{account}, {reopening} Os cards desse perfil nessa rota esperam sem ocupar vaga e sem contar falha. '
            f'Nenhuma outra rota é usada no lugar.{intake}')


def _noticed(wall, sent, now):
    """This wall's notice went out, or a notifier holds it right now."""
    if not sent or sent.get('closed_at') != wall.get('closed_at'):
        return False
    return bool(sent.get('sent_at')) or now - float(sent.get('claimed_at') or 0) < PROBE_LEASE_SECONDS


def unannounced(*, now=None):
    """The closed routes whose notice has not gone out. The gateway's notifier sends it."""
    now = time.time() if now is None else float(now)
    return [wall for wall in walls() if not _noticed(wall, _load(_path(*_route(wall), NOTICE)), now)]


def claim_notice(wall, *, now=None):
    """Take this wall's notice for the caller. False when another notifier holds it or has sent it."""
    now = time.time() if now is None else float(now)
    path = _path(*_route(wall), NOTICE)
    claim = {'closed_at': wall.get('closed_at'), 'claimed_at': now, 'sent_at': None, 'deliveries': []}
    sent = _load(path)
    if _noticed(wall, sent, now):
        return False
    if sent is None and not path.exists():
        return _create(path, claim)
    _write(path, claim)  # what is there is of an earlier wall of this route, or a claim nobody finished
    return True


def notice_sent(wall, deliveries, *, now=None):
    """The receipt of the notice: a restart or a second gateway does not send it again."""
    _write(_path(*_route(wall), NOTICE),
           {'closed_at': wall.get('closed_at'), 'claimed_at': None, 'deliveries': list(deliveries),
            'sent_at': time.time() if now is None else float(now)})


# ── The worker's side ───────────────────────────────────────────────────

def _owns_card():
    """This execution is the dispatcher's own worker of a card (not a delegated child, not an in-process cron)."""
    if not os.environ.get('HERMES_KANBAN_TASK', '').strip():
        return False
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, is_dispatcher_owned_worker_context
    return is_dispatcher_owned_worker_context() and os.environ.get(DELEGATED_CHILD_ENV_MARKER) != '1'


def _wake_receipt():
    """The receipt of the wake this turn was started on, when the runtime woke a gateway session with one (a
    preserved request, a card's question): the gateway hands it to the agent's thread. ``None`` for a turn a
    person's message started, and outside a gateway."""
    try:
        from gateway.wake import current_notify_receipt
        receipt = current_notify_receipt.get()
    except Exception:  # noqa: BLE001 - no gateway in this process
        return None
    return receipt if isinstance(receipt, dict) and receipt else None


def _intake_receipt():
    """The inbox entry whose turn this is, when the caller is the coordinating session woken on a preserved
    request."""
    receipt = _wake_receipt()
    if receipt and receipt.get('kind') == 'nfos_coordinator_input' and receipt.get('request_id'):
        return receipt
    return None


def _note_refused_wake(agent):
    receipt = _wake_receipt()
    if receipt is not None:
        try:
            agent._quota_refused_wake = receipt
        except Exception:  # noqa: BLE001
            pass


def wake_refused_on_quota(agent):
    """This agent is running a turn the runtime started with a wake, and the provider has refused that turn on
    quota (PRINCIPAL_WALL_20261012). Nobody in the chat of that wake asked the turn anything: what the turn
    reports about the refusal stays in the log."""
    receipt = _wake_receipt()
    return receipt is not None and getattr(agent, '_quota_refused_wake', None) is receipt


def _route_owner():
    """Who this execution is to the route it runs on, as ``close`` takes it: the card whose worker it is, the
    inbox entry whose turn it is, or ``None`` for any other session, which closes nothing."""
    if _owns_card():
        return {'task_id': os.environ.get('HERMES_KANBAN_TASK', '').strip(),
                'board': os.environ.get('HERMES_KANBAN_BOARD', '').strip() or None}
    receipt = _intake_receipt()
    return {'request_id': str(receipt['request_id'])} if receipt else None


def _entry_met_wall(route, window):
    receipt = _intake_receipt()
    from pathlib import Path
    from hermes_cli import kanban_db as kb, nfos_delivery as delivery
    with kb.connect_closing(Path(receipt['db_path'])) as conn:
        delivery.coordinator_met_wall(conn, receipt, route=route, limit=window['limit'], reset_at=window['reset_at'])


def _own_profile():
    """The profile this process runs as: the one whose home, and so whose credentials, it reads. For a card's
    worker that is the profile the dispatcher launched it as (``-p`` and ``HERMES_HOME`` of ``_default_spawn``)."""
    from hermes_cli.profiles import get_active_profile_name
    return get_active_profile_name()


def worker_route(agent):
    """The route this worker is on: the one the dispatcher launched it with while it runs its primary model
    (the dispatcher's own spelling, so both sides name the same route), else what the agent switched to. The
    profile is the launch's in both cases, and this process's own when the launch did not say."""
    try:
        launched = json.loads(os.environ.get(ROUTE_ENV) or 'null')
    except (ValueError, TypeError):
        launched = None
    profile, provider, model = launched if isinstance(launched, list) and len(launched) == 3 else (None, None, None)
    if getattr(agent, '_fallback_activated', False) or not (provider and model):
        provider, model = getattr(agent, 'provider', ''), getattr(agent, 'model', '')
    return _key(profile or _own_profile(), provider, model)


def on_quota_error(agent, error):
    """True when the provider tied this refusal to a long usage window: the turn leaves on this attempt.

    Called by the conversation loop for a rate-limit or billing refusal the credential pool cannot rotate past.
    When the caller is a card's own worker and no fallback provider is left to take the turn, the route is closed
    for the dispatcher. The same when the caller is the coordinating session on the turn of an inbox entry
    (PRINCIPAL_WALL_20261012): the route is closed for the intake, and the entry keeps the name of the route it
    waits for. The notice of the wall is the gateway's to send (``unannounced``)."""
    _note_refused_wake(agent)
    try:
        window = quota_window(error)
    except Exception:  # noqa: BLE001 - an answer that cannot be read is retried as before this rule
        logger.warning('ROUTE_WALL_20261012 could not read a quota refusal', exc_info=True)
        return False
    if window is None:
        return False
    try:
        owner = None if agent._has_pending_fallback() else _route_owner()
        if owner:
            route = worker_route(agent)
            wall, opened = close(*route, limit=window['limit'], reset_at=window['reset_at'],
                                 account=quota_account(error), **owner)
            if opened:
                logger.warning('ROUTE_WALL_20261012 route %s closed by limit %s (reset_at=%s)',
                               label(wall), wall['limit'], wall['reset_at'])
            if wall and owner.get('request_id'):
                _entry_met_wall(route, window)
    except Exception:  # noqa: BLE001 - the verdict stands; a route left unmarked is launched on as before this rule
        logger.warning('ROUTE_WALL_20261012 could not close the route', exc_info=True)
    return True


def answered(agent):
    """Called at the top of each loop iteration of a card's worker. Once the provider has answered a call that
    began after the wall was last met, the route is open: the wall is put aside.

    An answered call is one the runtime counted (``session_api_calls``, which it raises for an answer that carries
    its token usage). The mark taken here one iteration earlier is the soonest that call can have begun, so the
    answer to a call already in flight when the wall was met proves nothing, and the next call of the same worker
    does."""
    try:
        calls = int(getattr(agent, 'session_api_calls', 0) or 0)
        mark, agent._route_wall_mark = getattr(agent, '_route_wall_mark', None), (time.time(), calls)
        if mark is None or calls <= mark[1] or not any_closed() or not _owns_card():
            return False
        route = worker_route(agent)
        wall = read(*route)
        if wall is None or float(wall.get('walled_at') or 0) >= mark[0]:
            return False
        if reopen(*route):
            logger.warning('ROUTE_WALL_20261012 route %s reopened: the provider answered', label(wall))
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
    """The route ``_default_spawn`` launches this card on, or ``None`` when it cannot be told: the profile the
    worker runs as (the card's assignee, whatever profile this dispatcher is), the provider and the model."""
    from hermes_cli import nfos_delivery as delivery
    if not task.assignee:
        return None  # the launch itself refuses a card with no assignee
    provider = model = None
    if delivery.get_workflow(conn, task.id):
        from hermes_cli.nfos_principal_review import worker_model_args
        args = worker_model_args(task, conn)
        model = args[args.index('-m') + 1] if '-m' in args else None
        provider = args[args.index('--provider') + 1] if '--provider' in args else None
    elif task.model_override:
        model, provider = task.model_override, task.provider_override
    if not (provider and model):
        base_provider, base_model = _profile_route(task.assignee)
        provider, model = provider or base_provider, model or base_model
    return _key(task.assignee, provider, model) if provider and model else None


def _other_hold(conn, task_id):
    """Something else already keeps this card from being launched: a question waiting for its answer, an approval
    of the request, a maintenance pause. Each has its own trail, and whoever watches the board must go on seeing
    it: a card in that state is not said to be waiting for the route."""
    from hermes_cli import kanban_db as kb, nfos_delivery as delivery
    from hermes_cli.nfos_workspace_repair import maintenance_pause_pending
    return bool(kb._nfos_decision_open(conn, task_id) or delivery.task_support_approval_pending(conn, task_id)
                or maintenance_pause_pending(conn, task_id))


def hold(conn, task, *, dry_run=False):
    """The wall that keeps the dispatcher from launching this card now, or ``None``.

    A held card is declared once in the wait registry: it waits with a named reason, an executor that is not the
    card and a time to look again, instead of standing in ``ready`` with nothing to show for it. A card another
    hold also keeps is not launched on the closed route either, and is left out of the registry."""
    route = card_route(conn, task)
    wall = closed(*route) if route else None
    if wall is None or dry_run or _other_hold(conn, task.id):
        return wall
    from hermes_cli import nfos_delivery as delivery, nfos_waits
    name = f"{wall['provider']}/{wall['model']}"
    try:
        nfos_waits.declare(
            conn, task.id, None, reason='provider_quota', executor={'kind': 'external', 'name': name},
            predicate={'kind': 'route_open', 'profile': wall['profile'], 'provider': wall['provider'],
                       'model': wall['model']},
            subject=f"a rota {name} do perfil {wall['profile']} (limite {wall['limit']} do provedor)",
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


# ── The intake's side (PRINCIPAL_WALL_20261012) ─────────────────────────
#
# A Telegram request is persisted on arrival and handed to the coordinating session (the Principal), which decides
# by intent whether it is new work. That decision is a turn on the Principal's own route. When the turn ends on
# the provider's quota the decision was not taken: the entry goes back to the inbox and is offered again once that
# route is open, the oldest entry first, which makes its turn the probe of a route still closed. Nothing is said
# in the topic of who asked, and no other route takes the decision.

QUOTA_REASONS = ('rate_limit', 'billing')


def quota_failure(result):
    """The turn ended on its provider's quota, by the two reasons the executor leaves its card on."""
    return bool(isinstance(result, dict) and result.get('failed') and result.get('failure_reason') in QUOTA_REASONS)


def session_route(profile):
    """The route a session of this profile runs on unless it switched model: the profile's own."""
    try:
        provider, model = _profile_route(profile)
    except Exception:  # noqa: BLE001 - a profile whose config cannot be read names no route
        logger.debug('PRINCIPAL_WALL_20261012 no route read for profile %s', profile, exc_info=True)
        return None
    return _key(profile, provider, model) if provider and model else None


def entry_route(payload, profile):
    """The route the Principal decides this inbox entry on: the one its last refused turn named, else the
    profile's own."""
    named = ((payload.get('coordination') or {}).get('quota') or {}).get('route')
    if isinstance(named, (list, tuple)) and len(named) == 3 and all(named):
        return _key(*named)
    return session_route(profile)


def entry_turn(payload, profile, request_id):
    """May this inbox entry be offered to the Principal now? ``False`` while a wall holds its route. On a route
    whose reopening is due the offer is the probe, and is taken here, so one entry probes at a time. Returns the
    route when the offer is that probe, else ``True``."""
    try:
        if not any_closed():
            return True
        route = entry_route(payload, profile)
        if not route or read(*route) is None:
            return True
        if closed(*route) is not None:
            return False
        take_probe(*route, task_id=request_id)
        return route
    except Exception:  # noqa: BLE001 - a wall that cannot be read holds nothing: offered as before this rule
        logger.warning('PRINCIPAL_WALL_20261012 could not read the route of request %s', request_id, exc_info=True)
        return True


def entry_refused(conn, receipt, *, reason, profile):
    """The Principal's turn on this inbox entry ended on quota: back to the inbox it goes.

    On a route a wall holds, the wall says when it is offered again (the reset time the provider gave, or the next
    probe). With no wall (a throttle that outlasted the client's retries, credit that ran out with no window named),
    it waits the respawn guard's spacing, counted on the entry."""
    from hermes_cli import nfos_delivery as delivery
    row = delivery.get_request(conn, receipt['request_id'])
    if row is None:
        return None
    payload = json.loads(row['payload'])
    route = entry_route(payload, profile)
    turns = int(((payload.get('coordination') or {}).get('quota') or {}).get('turns') or 0)
    walled = bool(route and read(*route) is not None)
    returned = delivery.return_coordinator_input(
        conn, receipt, reason=reason, retry_at=None if walled else time.time() + _probe_spacing(turns))
    if returned is not None:
        logger.warning('PRINCIPAL_WALL_20261012 request %s back in the Principal inbox: its turn ended on %s (%s)',
                       receipt['request_id'], reason,
                       f'route {route[1]}/{route[2]}@{route[0]} closed' if walled else 'no usage window named')
    return returned


def session_answered(profile, model, *, started_at, conn=None, request_id=None):
    """The provider answered a turn of a session of this profile: a wall on the route that turn ran on is over.

    The route is the one the inbox entry of that turn was held on, when it names one, or the profile's own, and
    only when the model that answered is that route's: a turn that ran on another model (a session switched to
    it, a fallback that took the turn) proves nothing about the closed one. As for a card's worker
    (``answered``), only a turn begun after the wall was last met is proof."""
    routes = []
    if conn is not None and request_id:
        from hermes_cli import nfos_delivery as delivery
        row = delivery.get_request(conn, request_id)
        if row is not None:
            routes.append(entry_route(json.loads(row['payload']), profile))
    routes.append(session_route(profile))
    answered_model = str(model or '').strip().lower()
    reopened = []
    for route in routes:
        if not route or route[2] != answered_model or route in reopened:
            continue
        wall = read(*route)
        if wall is None or float(wall.get('walled_at') or 0) >= float(started_at):
            continue
        if reopen(*route):
            logger.warning('ROUTE_WALL_20261012 route %s reopened: the provider answered the coordinating session',
                           label(wall))
            reopened.append(route)
    return reopened


# ── The wait registry's side (nfos_waits, reason provider_quota) ────────

def wait_check(conn, wait):
    """Predicate ``route_open``: true once the route has no wall."""
    predicate = wait['predicate']
    wall = read(predicate.get('profile'), predicate.get('provider'), predicate.get('model'))
    if wall is None:
        return True, {'route': 'open'}
    return False, {'route': 'closed', 'limit': wall.get('limit'), 'reset_at': wall.get('reset_at'),
                   'open_at': int(open_at(wall)), 'probes': int(wall.get('probes') or 0)}


def wait_withdrawn(conn, wait):
    """Why this wait is no longer true of its card, or ``None``.

    The wall file tells whether the route is closed; only the card tells whether that route is still the one it
    is launched on. A card moved to another route (the profile's worker route changed, a model pinned on the
    card, an escalation, the card given to another profile) is launched there while the old wall stands with no
    card left to probe it, and a wait left open would keep saying the card waits. The same for a card another
    hold took over."""
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, wait['task_id'])
    if task is None:
        return None
    predicate = wait['predicate']
    if card_route(conn, task) != _key(predicate.get('profile'), predicate.get('provider'), predicate.get('model')):
        return 'route_changed'
    return 'other_hold' if _other_hold(conn, task.id) else None


def wait_expired(conn, wait, why, now):
    """A route still closed at the wait's deadline. The registry has written the outcome; the dispatcher keeps
    holding the card, now outside a healthy wait, which is what makes it show as stalled to whoever watches."""
    return None
