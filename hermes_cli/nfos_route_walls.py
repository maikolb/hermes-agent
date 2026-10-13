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
* The route reopens when the provider answers a launch on it (``answered``), and a closed route is probed by the
  launch of what waits for it: one launch at a time (``take_probe``), which on the wall leaves on its first
  refused call and spends nothing of the balance of an escalated card that probes
  (``nfos_principal_review.escalation_usage``). With no reset time
  the spacing is the respawn guard's own (the rate-limit cooldown, doubling up to one hour), kept once for the
  route instead of once per card. With a reset time the route is probed every hour and at that time: the time
  the provider gave can only bring the next launch forward (``open_at``), so a limit lifted early (quota bought,
  plan changed) is found within the hour and nobody has to erase a file. That is of a route a card waits for:
  under a reset time still ahead a request is not a probe (``entry_held``), and a route only requests wait for is
  probed by nobody until that time. A route that closes again sooner than its next probe would have come is the
  same wall going on (``close``).
* One notice per wall, sent by the gateway's notifier to the management topic (``unannounced``, ``claim_notice``):
  a closed route is a fact of one profile of the host, and the card that met the wall may have no chat a passive
  notice is allowed in. While the wall stands and something waits for its route the notice is repeated there
  once a day (``still_closed``), with how long the route has been closed and what waits for it. The account the
  provider named stays on the wall and out of both texts.
* The coordinating session's route closes the same way when it meets the window on the turn of a preserved
  request (PRINCIPAL_WALL_20261012, "The intake's side" below): the request goes back to the Principal's inbox and
  is offered again when the route is due, the oldest entry first, its turn being the route's probe. A turn of a
  request that ends on quota with no window named closes the route as well, with no reset time: the wall is the
  only clock a held request has, and its notices the only word about it. With a reset time still ahead a request
  is not the probe of the hour: it waits for that time (``entry_held``), and the hourly probe is of the cards.
  With no card waiting, a limit lifted before that time is found only by a turn of a session of the profile that
  is not the turn of a request (``session_answered``); a person's message in the topic of a delivery project is
  not one, the intake takes it as one more request.

Nothing here selects another provider or model: a closed route makes its cards, and the requests whose decision
is the Principal's, wait.
"""
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# The cap the conversation loop puts on one Retry-After wait. A reset beyond it is a window, not a throttle.
LONG_WINDOW_FLOOR_SECONDS = 600
# Probe spacing ceiling, the respawn guard's own (QUOTA_BACKOFF_20260910).
PROBE_CEILING_SECONDS = 3600
# A probe that neither met the wall again nor got an answer in this time gave no verdict: another card may probe.
# The same for a notifier that took a wall's notice and neither sent it nor let it go.
PROBE_LEASE_SECONDS = 900
# While a wall stands, its notice is repeated at this interval (WALL_DAILY_NOTICE_20261012).
NOTICE_REPEAT_SECONDS = 86400
# The longest a writer waits for the lock of a route's wall. It is held for one read and one write, and the intake
# asks for it on the gateway's event loop: a holder that stopped (a frozen process) must not stop the gateway.
LOCK_WAIT_SECONDS = 5
# Wait registry: how often a held card's wait is rechecked and when it stops counting as a healthy wait.
WAIT_RECHECK_SECONDS = 300
WAIT_DEADLINE_SECONDS = 35 * 86400

ROUTE_ENV = 'HERMES_KANBAN_ROUTE'
# Beside a route's wall (``.json``): the wall it had when it last reopened, the notice of its wall, and the lock
# the writers of the wall take.
REOPENED, NOTICE, LOCK = '.reopened', '.notice', '.lock'
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


@contextlib.contextmanager
def _locked(profile, provider, model):
    """One writer at a time on this route's wall, among the processes of the host that write it: the gateway's
    dispatcher and intake, the worker of a card, a launch made outside the gateway. Held for one read and one
    write, never across a launch. Where the platform gives no file lock, or the lock is not given within
    ``LOCK_WAIT_SECONDS``, the writer goes on unserialised."""
    handle = None
    try:
        path = _path(profile, provider, model, LOCK)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, 'a+b')
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # gives up by itself after ten tries a second apart
        else:
            import fcntl
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
    except BlockingIOError:
        logger.warning('ROUTE_WALL_20261012 the lock on the wall of %s/%s@%s was not given in %s s: going on without it',
                       provider, model, profile, LOCK_WAIT_SECONDS)
    except (OSError, ImportError, AttributeError):
        logger.debug('ROUTE_WALL_20261012 no lock on the wall of %s/%s@%s', provider, model, profile, exc_info=True)
    try:
        yield
    finally:
        if handle is not None:
            if os.name == 'nt':
                try:
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, ImportError, AttributeError):
                    pass
            handle.close()  # closing the file gives the lock back


def _probe_spacing(probes):
    """The respawn guard's schedule (cooldown doubling up to one hour), once for the route."""
    try:
        from hermes_cli.kanban_db import _resolve_rate_limit_cooldown_seconds
        first = int(_resolve_rate_limit_cooldown_seconds())
    except Exception:  # noqa: BLE001
        first = 0
    first = first if first > 0 else 300
    return min(PROBE_CEILING_SECONDS, first * (2 ** min(int(probes), 12)))


def _next_probe(now, probes, reset_at):
    """When the route is probed next. With no reset time, the guard's schedule. With one, the ceiling of that
    schedule: the provider said how far the end of the window is, so the early, closely spaced probes have
    nothing to find, and one launch an hour is what finds a limit lifted before that time."""
    return now + (PROBE_CEILING_SECONDS if reset_at else _probe_spacing(probes))


def open_at(wall):
    """When the route admits its next launch: its next probe, or the reset time the provider gave when that comes
    first. The reset time only brings the launch forward. It never holds the probe back: a window whose end is
    days away is still tried every hour, because the limit may be lifted before the time the provider gave. Tried
    by the launch of a card: an inbox entry waits for the reset time (``entry_held``)."""
    probe_at, reset_at = float(wall.get('next_probe_at') or 0), float(wall.get('reset_at') or 0)
    return min(probe_at, reset_at) if reset_at else probe_at


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
            'next_probe_at': _next_probe(now, 0, reset_at), 'probe': None,
            'closed_by': {'task_id': task_id, 'board': board}}
    if request_id:
        wall['closed_by']['request_id'] = request_id
    prober = task_id or request_id
    with _locked(*route):
        before = _load(_path(*route, REOPENED)) or {}
        probes = int(before.get('probes') or 0) + 1
        goes_on = (before.get('limit') == wall['limit'] and before.get('closed_at')
                   and now - float(before.get('reopened_at') or 0) < _probe_spacing(probes))
        if goes_on:
            wall.update(closed_at=before['closed_at'], closed_by=before.get('closed_by') or wall['closed_by'],
                        probes=probes, next_probe_at=_next_probe(now, probes, reset_at))
        if _create(_path(*route), wall):
            return wall, not goes_on
        existing = read(*route)
        if existing is None:
            return None, False
        if prober and (existing.get('probe') or {}).get('task_id') == prober:
            probes = int(existing.get('probes') or 0) + 1
            existing.update(probes=probes, probe=None, walled_at=now, limit=str(limit), reset_at=reset_at,
                            account=account or existing.get('account'),
                            next_probe_at=_next_probe(now, probes, reset_at))
            _write(_path(*route), existing)
        return existing, False


def reopen(profile, provider, model, *, now=None):
    """The provider answered: the route is open. The wall is put aside, not erased, so ``close`` can tell a
    route that closes again at once from a new wall."""
    route = _key(profile, provider, model)
    with _locked(*route):
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


def _holds(wall, now):
    """A probe of this wall is in flight, or its next launch is not due yet."""
    probe = wall.get('probe') or {}
    return bool(probe and now - float(probe.get('at') or 0) < PROBE_LEASE_SECONDS) or now < open_at(wall)


def _awaits_reset(wall, now):
    """The provider gave this wall a reset time and it is still ahead."""
    return now < float(wall.get('reset_at') or 0)


def closed(profile, provider, model, *, now=None):
    """The wall holding launches on this route right now, or ``None``: the route is open, or its next launch is
    due and no probe is in flight, so the next launch is the probe (``take_probe``)."""
    wall = read(profile, provider, model)
    if wall is None:
        return None
    return wall if _holds(wall, time.time() if now is None else float(now)) else None


def take_probe(profile, provider, model, *, task_id, board=None, now=None):
    """A launch of ``task_id`` is about to go out on this route. On a closed route whose next launch is due that
    launch is the probe, and only one launch is: the wall is read again and marked under the route's lock, so of
    the launchers that all saw the route due (the dispatchers of two boards, a dispatcher and the intake) one
    takes it.

    Returns the wall, with this launch as its probe; ``None`` when the route has no wall (it is open and every
    launch goes); ``False`` when the wall holds this launch after all: another launch took the probe first, or
    the verdict of that probe has already moved the schedule.

    The probe is of the launch that took it, not of every launch that carries its id: a second launcher of the
    same card or of the same inbox entry (two gateways serving one profile) is held like any other, so it never
    reaches the point where it would give back, with ``release_probe``, a probe it does not have."""
    now = time.time() if now is None else float(now)
    if read(profile, provider, model) is None:
        return None
    with _locked(profile, provider, model):
        wall = read(profile, provider, model)
        if wall is None:
            return None
        if _holds(wall, now):
            return False
        wall['probe'] = {'task_id': task_id, 'board': board, 'at': now}
        _write(_path(profile, provider, model), wall)
        return wall


def release_probe(profile, provider, model, *, task_id):
    """The launch that took the probe did not happen: the turn of probing goes back at once."""
    if read(profile, provider, model) is None:
        return
    with _locked(profile, provider, model):
        wall = read(profile, provider, model)
        if wall is not None and (wall.get('probe') or {}).get('task_id') == task_id:
            wall['probe'] = None
            _write(_path(profile, provider, model), wall)


def label(wall):
    """A route as logs and the dispatcher's diagnosis name it: ``<provider>/<model>@<profile>``."""
    return f"{wall.get('provider')}/{wall.get('model')}@{wall.get('profile')}"


# ── The notices of a wall ───────────────────────────────────────────────
#
# One when the route closes, and one a day while the wall stands with something waiting for its route
# (WALL_DAILY_NOTICE_20261012: a notice on the first day and silence for a week is a board standing still in
# silence). Both go to the management topic and to no other; neither names the account the provider refused, which
# stays on the wall for whoever reads the file.

def _span(seconds):
    minutes = max(1, round(float(seconds) / 60))
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    return ' '.join(f'{amount} {unit}' for amount, unit in ((days, 'd'), (hours, 'h'), (minutes, 'min')) if amount)


def _reset_time(wall):
    return time.strftime('%d/%m %H:%M UTC', time.gmtime(float(wall['reset_at'])))


# What becomes of the inbox entries of a wall with a reset time still ahead (``entry_held``, ``session_answered``),
# and which turn reopens the route before that time. Not a person's message in the topic of a delivery project: the
# intake takes it as a request before any turn (``_nfos_receive``), so it tests nothing.
ENTRIES_RESUMED = ('Os pedidos na caixa do Principal são retomados nessa hora, ou antes se o provedor responder a '
                   'um turno de sessão desse perfil nessa rota que não seja o de um pedido: mensagem no tópico de '
                   'gestão, despertar de decisão de card. Mensagem nova em tópico de projeto não testa a rota: '
                   'entra como pedido.')


def notice(wall):
    """The message of a wall when its route closes, for the operations channel. It says whose route closed:
    another profile on the same provider and model goes on with its own credential."""
    route = f"{wall['provider']}/{wall['model']}"
    ceiling = PROBE_CEILING_SECONDS // 60
    # PRINCIPAL_WALL_20261012: the window was met by the coordinating session, on the turn of a request.
    by_session = bool((wall.get('closed_by') or {}).get('request_id'))
    if wall.get('reset_at'):
        asked = _span(float(wall['reset_at']) - float(wall.get('walled_at') or wall.get('closed_at') or 0))
        # A request is not the probe of the hour (``entry_held``): the text promises no probe on its account.
        reopening = (f'com reinício informado para {_reset_time(wall)} (o provedor pediu {asked} de espera). '
                     + (ENTRIES_RESUMED if by_session else
                        f'Um lançamento sonda a rota a cada {ceiling} min e nessa hora; ela reabre quando o provedor '
                        'responder, mesmo antes da hora informada.'))
    else:
        first = _probe_spacing(0) // 60
        reopening = (f'sem hora de reinício informada. Um lançamento sonda a rota em {first} min e depois a intervalos '
                     f'que dobram até {ceiling} min; ela reabre quando o provedor responder.')
    intake = (' É a rota da sessão coordenadora (Principal): os pedidos que chegam pelo Telegram ficam gravados e a '
              'decisão de entrada de cada um é retomada, na ordem de chegada, quando a rota reabrir.'
              if by_session else '')
    return (f"Rota {route} fechada para o perfil {wall['profile']}: o provedor recusou pelo limite {wall['limit']}, "
            f'{reopening} Os cards desse perfil nessa rota esperam sem ocupar vaga e sem contar falha. '
            f'Nenhuma outra rota é usada no lugar. Enquanto ela seguir fechada com card ou pedido esperando por ela, '
            f'sai aqui um aviso por dia.{intake}')


def still_closed(wall, count, *, now=None):
    """The day's message of a wall that still stands: for how long the route has been closed and what waits for
    it (``count``, from ``waiting``). ``None`` when nothing waits for the route: the day's notice is of a wall
    somebody waits behind. A wall whose cards went to another route, were cancelled or are held by something else
    is probed by no launch and holds nobody, and a notice a day about it would never end; it has had its notice,
    and gets the day's again from the day something waits for it.

    The probe is the launch of what waits, so the text speaks of it only when the count shows a card or an entry
    (a count that is only partial says neither). An entry that waits for the reset time is not a probe
    (``entry_held``): of the entries the text then says when they are resumed, and the probe it speaks of is the
    one of a card, when a card waits."""
    now = time.time() if now is None else float(now)
    waits = bool(count['cards'] or count['entries'])
    if not waits and not count.get('unread'):
        return None
    route = f"{wall['provider']}/{wall['model']}"
    reset = (f'O reinício informado pelo provedor é {_reset_time(wall)}.' if wall.get('reset_at')
             else 'O provedor não informou hora de reinício.')
    partial = (' A contagem é parcial: houve quadro que não pôde ser lido.' if count.get('unread') else '')
    resumed = bool(count['entries']) and _awaits_reset(wall, now)
    probes = 'O lançamento de um card' if resumed else 'Um lançamento'
    probing = (f' {probes} sonda a rota a intervalos de até {PROBE_CEILING_SECONDS // 60} min e ela reabre '
               'quando o provedor responder.' if count['cards'] or (waits and not resumed) else '')
    since = _span(now - float(wall.get('closed_at') or now))
    return (f"Rota {route} do perfil {wall['profile']} segue fechada há {since}, pelo limite {wall['limit']}. "
            f"Cards esperando por ela: {count['cards']}. Pedidos na caixa do Principal, ainda sem card: "
            f"{count['entries']}.{partial} {reset}{' ' + ENTRIES_RESUMED if resumed else ''}{probing} "
            'Nenhuma outra rota é usada no lugar.')


def waiting(wall, *, owner_of):
    """What waits for this route on the boards of the host, for the day's notice: the cards held on it (their open
    ``provider_quota`` rows of the wait registry, for a card that stands in a lane the dispatcher launches from or
    is running as the probe: those are the cards whose launch probes the route; a blocked card is not one) and the
    Principal's inbox entries kept with no card (still ``coordinating``, taken by no turn, on this route).
    ``owner_of`` names the profile an entry's session runs as, as the intake does. Read-only; a board that cannot
    be read is counted in ``unread`` and the text says so."""
    from hermes_cli import kanban_db as kb
    route = _key(*_route(wall))
    found = {'cards': 0, 'entries': 0, 'unread': 0}
    seen = set()
    for meta in kb.list_boards(include_archived=False):
        slug = meta.get('slug') or kb.DEFAULT_BOARD
        path = Path(meta.get('db_path') or kb.kanban_db_path(board=slug)).resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
                conn.row_factory = sqlite3.Row
                tables = {row['name'] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('nfos_waits','nfos_requests')")}
                held = conn.execute("SELECT w.predicate FROM nfos_waits w JOIN tasks t ON t.id=w.task_id "
                                    "WHERE w.reason='provider_quota' AND w.status='open' "
                                    "AND t.status IN ('ready','review','running')"
                                    ).fetchall() if 'nfos_waits' in tables else []
                kept = conn.execute("SELECT payload FROM nfos_requests WHERE status='coordinating'"
                                    ).fetchall() if 'nfos_requests' in tables else []
            for row in held:
                predicate = json.loads(row['predicate'] or '{}')
                found['cards'] += _key(*(predicate.get(part) for part in ('profile', 'provider', 'model'))) == route
            for row in kept:
                payload = json.loads(row['payload'])
                if not (payload.get('coordination') or {}).get('wake_accepted'):
                    found['entries'] += entry_route(payload, owner_of(payload)) == route
        except (sqlite3.Error, OSError, ValueError, TypeError):
            found['unread'] += 1
            logger.warning('WALL_DAILY_NOTICE_20261012 board %s could not be read for the count', slug, exc_info=True)
    return found


def _noticed(wall, sent, now):
    """Nothing is to be said of this wall now: its notice went out less than a day ago, or a notifier holds it."""
    if not sent or sent.get('closed_at') != wall.get('closed_at'):
        return False
    if now - float(sent.get('claimed_at') or 0) < PROBE_LEASE_SECONDS:
        return True
    return bool(sent.get('sent_at')) and now - float(sent['sent_at']) < NOTICE_REPEAT_SECONDS


def _notices(sent):
    """How many notices this receipt counts (one, for a receipt written before they were counted)."""
    return int(sent.get('notices') or bool(sent.get('sent_at')))


def unannounced(*, now=None):
    """The closed routes with a notice due: the one of the wall, or the day's while the wall stands. The gateway's
    notifier sends it."""
    now = time.time() if now is None else float(now)
    return [wall for wall in walls() if not _noticed(wall, _load(_path(*_route(wall), NOTICE)), now)]


def claim_notice(wall, *, now=None):
    """Take the notice this wall is due for the caller. False when another notifier holds it or has sent it, and
    when the wall the notifier listed is no longer the one standing (the route reopened, or closed anew, in the
    meantime: a notice of a closed route must not go out for a route the provider has just answered); else the
    claim, whose ``notices`` counts the ones this wall has had (none yet: the wall's own notice is due)."""
    now = time.time() if now is None else float(now)
    route = _route(wall)
    path = _path(*route, NOTICE)
    with _locked(*route):  # two gateways of the host at the same due time: one takes it
        standing = read(*route)
        if standing is None or standing.get('closed_at') != wall.get('closed_at'):
            return False
        sent = _load(path)
        if _noticed(wall, sent, now):
            return False
        same = bool(sent) and sent.get('closed_at') == wall.get('closed_at')
        # What is there of another wall of this route is replaced; a claim nobody finished is taken over.
        claim = {'closed_at': wall.get('closed_at'), 'claimed_at': now,
                 'sent_at': sent.get('sent_at') if same else None, 'notices': _notices(sent) if same else 0,
                 'deliveries': list(sent.get('deliveries') or []) if same else []}
        _write(path, claim)
        return claim


def notice_sent(wall, deliveries, *, now=None):
    """The receipt of the notice: a restart or a second gateway does not send it again before the next one is due.
    ``deliveries`` are the ones of the latest notice."""
    route = _route(wall)
    path = _path(*route, NOTICE)
    with _locked(*route):
        sent = _load(path) or {}
        notices = _notices(sent) if sent.get('closed_at') == wall.get('closed_at') else 0
        _write(path, {'closed_at': wall.get('closed_at'), 'claimed_at': None, 'deliveries': list(deliveries),
                      'sent_at': time.time() if now is None else float(now), 'notices': notices + 1})


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
    person's message started, and outside a gateway.

    ``None`` as well for an agent that only runs inside that turn (a ``delegate_task`` child, a cron job fired in
    process): it inherits the receipt with the turn's context and is not the session the wake was for. Its
    provider and model are its own, so a refusal it meets says nothing of the session's route."""
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context
        from gateway.wake import current_notify_receipt
        receipt = current_notify_receipt.get() if is_dispatcher_owned_worker_context() else None
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


def _entry_turn_refused(receipt, route, limit, reset_at):
    """The inbox entry keeps the route its turn was last refused on, as the session ran it, and what the provider
    said of the limit: the turn's end reads it there (``entry_refused``)."""
    from pathlib import Path
    from hermes_cli import kanban_db as kb, nfos_delivery as delivery
    with kb.connect_closing(Path(receipt['db_path'])) as conn:
        delivery.coordinator_turn_refused(conn, receipt, route=route, limit=limit, reset_at=reset_at)


def _soonest_reset(agent, reset_at, now):
    """The reset time of the route when its profile has several credentials on the provider.

    The refusal that reaches ``on_quota_error`` is of the last credential the pool tried, and the route answers
    again when the first of them is back. The pool keeps the reset time each provider answer gave
    (``last_error_reset_at``); the soonest one still ahead is the route's, when it is sooner than this refusal's."""
    try:
        known = [_absolute(getattr(entry, 'last_error_reset_at', None)) for entry in agent._credential_pool.entries()]
        ahead = [at for at in known if at is not None and at > now]
    except Exception:  # noqa: BLE001 - no pool on this agent, or one that cannot be read: the refusal's own time
        return reset_at
    soonest = min(ahead, default=None)
    return soonest if soonest is not None and (reset_at is None or soonest < reset_at) else reset_at


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
    waits for. The notice of the wall is the gateway's to send (``unannounced``).

    On the turn of an inbox entry a refusal with no window behind it is also the last attempt when the route is
    already closed: that turn is the route's probe and this is its verdict, which the turn's end records
    (``entry_refused``)."""
    _note_refused_wake(agent)
    now = time.time()
    try:
        window = quota_window(error, now=now)
    except Exception:  # noqa: BLE001 - an answer that cannot be read is retried as before this rule
        logger.warning('ROUTE_WALL_20261012 could not read a quota refusal', exc_info=True)
        return False
    probe_refused = False
    try:
        owner = None if agent._has_pending_fallback() else _route_owner()
        intake = _intake_receipt()
        if owner or intake is not None:
            route = worker_route(agent)
            reset_at = _soonest_reset(agent, window['reset_at'], now) if window else None
            if window and owner:
                wall, opened = close(*route, limit=window['limit'], reset_at=reset_at,
                                     account=quota_account(error), **owner)
                if opened:
                    logger.warning('ROUTE_WALL_20261012 route %s closed by limit %s (reset_at=%s)',
                                   label(wall), wall['limit'], wall['reset_at'])
            elif owner and owner.get('request_id'):
                probe_refused = read(*route) is not None
            if intake is not None:
                _entry_turn_refused(intake, route, window['limit'] if window else _error_kind(_payloads(error)),
                                    reset_at)
    except Exception:  # noqa: BLE001 - the verdict stands; a route left unmarked is launched on as before this rule
        logger.warning('ROUTE_WALL_20261012 could not close the route', exc_info=True)
    return window is not None or probe_refused


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
    """The dispatcher is about to claim this card. On a route still closed that launch is the probe, and one
    launch is: the wall that holds the card when another launch took the probe first, else ``None``."""
    route = card_route(conn, task)
    if route and take_probe(*route, task_id=task.id, board=board) is False:
        return read(*route)  # ``None`` if the route reopened this instant: the card goes
    return None


def probe_not_launched(conn, task):
    """The claim of the card that took the probe was refused: the turn of probing goes back at once."""
    route = card_route(conn, task)
    if route:
        release_probe(*route, task_id=task.id)


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
#
# Not in the wait registry: a held entry has no card yet and ``nfos_waits`` is keyed by card (``task_id NOT NULL``),
# so it is not a row there and has none of a row's deadline, attempt cap or expiry outcome. It keeps no clock of its
# own either: the wall of its route, the same file the ``provider_quota`` rows of held cards check, is what says
# when it is offered again, and the wall's notices are the only word about it. A request-keyed row is a change of the
# registry's schema, which this module does not make.

QUOTA_REASONS = ('rate_limit', 'billing')


def quota_failure(result):
    """The turn ended on its provider's quota, by the two reasons the executor leaves its card on.

    Not a billing verdict the classifier itself holds unverified: that body may be the provider rejecting the
    content of this very message, which no reopening of the route would change."""
    return bool(isinstance(result, dict) and result.get('failed') and result.get('failure_reason') in QUOTA_REASONS
                and not result.get('billing_unverified'))


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


def entry_held(profile, provider, model, *, now=None):
    """The wall that keeps the inbox entries of this route from being offered now, or ``None``.

    It holds an entry as it holds a card (``closed``), and for longer when the provider gave a reset time: until
    that time the entry waits and is not the probe of the hour. The probe of an entry is the whole turn of its
    request, and every one the provider refuses leaves the wake of the request once more in the session of the
    topic (measured on a wall of 150 h with a reset time: 151 refused turns and 151 copies, about 135 thousand
    tokens, against 1 and 1 when the entry waits). The probe of the hour is left to the cards of the route. With
    no reset time the wall is the only clock the entry has, and it probes at the route's spacing.

    What reopens the route before the reset time when no card waits for it: a turn of a session of that profile
    on that model the provider answers (``session_answered``), which is never the turn of a held entry. The wake
    of a card's decision is one, and a person's message in a topic the intake takes no request from (the
    management topic). A person's message in the topic of a delivery project is not: the intake takes it as a
    request before any turn (``_nfos_receive``), and as an inbox entry it is held with the others. With none of
    those turns the entries wait for the reset time, as they did before the hourly probe."""
    wall = read(profile, provider, model)
    if wall is None:
        return None
    now = time.time() if now is None else float(now)
    return wall if _holds(wall, now) or _awaits_reset(wall, now) else None


def entry_turn(payload, profile, request_id):
    """May this inbox entry be offered to the Principal now? ``False`` while a wall holds it (``entry_held``). On
    a route whose reopening is due, at the reset time the provider gave or at the route's spacing when it gave
    none, the offer is the probe, and is taken here, so one entry probes at a time. Returns the route when the
    offer is that probe, else ``True``."""
    try:
        if not any_closed():
            return True
        route = entry_route(payload, profile)
        if not route or read(*route) is None:
            return True
        if entry_held(*route) is not None:
            return False
        taken = take_probe(*route, task_id=request_id)
        if taken is False:  # a card's launch took the probe between the two readings
            return False
        return route if taken else True
    except Exception:  # noqa: BLE001 - a wall that cannot be read holds nothing: offered as before this rule
        logger.warning('PRINCIPAL_WALL_20261012 could not read the route of request %s', request_id, exc_info=True)
        return True


def entry_refused(conn, receipt, *, reason, profile, answered_calls=0):
    """The Principal's turn on this inbox entry ended on quota: its route is closed and back to the inbox it goes.

    A turn that ends on quota was refused past everything the client does about a refusal, whatever the provider
    said of a window. When the provider named one, the route was closed during the turn (``on_quota_error``) and
    nothing changes here. When it named none (a throttle that outlasted the client's retries, credit that ran out,
    a usage limit with no reset time in the answer), the route is closed here with no reset time: one probe at the
    route's spacing, the oldest entry first, and the notices of a wall, so that an entry never waits on a clock
    of its own with nobody told. On a route already closed whose probe this turn was, this is the probe's verdict.

    The wall is then what says when the entry is offered again. Only an entry whose route cannot be told (no
    refusal recorded and no route in the profile's config) waits the same spacing counted on itself.
    ``answered_calls`` is what the provider had answered of that turn before refusing it, kept on the entry for
    its next offer."""
    from hermes_cli import nfos_delivery as delivery
    row = delivery.get_request(conn, receipt['request_id'])
    if row is None:
        return None
    payload = json.loads(row['payload'])
    quota = (payload.get('coordination') or {}).get('quota') or {}
    route = entry_route(payload, profile)
    wall = None
    if route:
        wall, opened = close(*route, limit=quota.get('limit') or reason, reset_at=None,
                             request_id=str(receipt['request_id']))
        if opened:
            logger.warning('ROUTE_WALL_20261012 route %s closed: a turn of the coordinating session ended on %s '
                           'with no usage window named', label(wall), reason)
    returned = delivery.return_coordinator_input(
        conn, receipt, reason=reason, route=route if wall else None,
        retry_at=None if wall else time.time() + _probe_spacing(int(quota.get('turns') or 0)),
        answered_calls=answered_calls)
    if returned is not None:
        logger.warning('PRINCIPAL_WALL_20261012 request %s back in the Principal inbox: its turn ended on %s (%s)',
                       receipt['request_id'], reason, f'route {label(wall)} closed' if wall else 'no route named')
    return returned


def entry_gave_no_verdict(conn, receipt, *, profile):
    """The turn of this inbox entry ended with nothing from the provider about quota (another failure, an
    interruption, no call made). When that turn was the probe of a closed route it gave no verdict: the probing
    goes back at once, to the next entry, instead of standing still for the probe's lease."""
    from hermes_cli import nfos_delivery as delivery
    row = delivery.get_request(conn, receipt['request_id'])
    route = entry_route(json.loads(row['payload']), profile) if row is not None else session_route(profile)
    if route:
        release_probe(*route, task_id=receipt['request_id'])


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
