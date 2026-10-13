"""POOL_WALL_20261012: a route that resolves no credential because its whole pool is inside a usage window.

A profile with several accounts on one provider keeps, for each, the last refusal its provider answered (the kind of
limit and the reset time of the window). When every one of them is inside its window the route resolution of a new
process, or of the gateway's next turn, finds nothing to run with and fails before any call is made
(``AuthError`` of code ``codex_rate_limited``). That launch or turn met the quota wall all the same: it leaves by
the door of a call the provider refused, the one ``nfos_route_walls`` already has.

Two callers, one for each way the runtime starts work on a route:

* the worker of a card (``cli.py``, the quiet single-query path): the quota exit code, and the route closed;
* a turn of a gateway session (``gateway/run.py``): the turn's result is a quota failure, so the turn of an inbox
  entry closes the route and gives the entry back, and a wake nobody typed says nothing in its chat.

The wall is asked the question the conversation loop asks of a refused call (``nfos_route_walls.on_quota_error``),
about the refusal the pool recorded. This module adds no rule of its own to the wall.
"""
import logging
import time
from types import SimpleNamespace

logger = logging.getLogger(__name__)


class RecordedQuotaRefusal(Exception):
    """A refusal the credential pool keeps from the provider, as the provider answered it: the kind of limit and
    the reset time of the usage window."""

    status_code = 429

    def __init__(self, kind, reset_at):
        super().__init__(f'{kind}: every credential of the route is inside a usage window')
        self.body = {'type': kind, 'resets_at': reset_at}


def quota_cause(error):
    """The quota refusal behind a route that did not resolve, or ``None`` for any other failure (a login that is
    missing or revoked, a broken setup). The gateway wraps the resolution's own error, so its causes are read."""
    from hermes_cli.auth import is_rate_limited_auth_error

    seen = 0
    while error is not None and seen < 8:
        if is_rate_limited_auth_error(error):
            return error
        error, seen = error.__cause__, seen + 1
    return None


def _reset_of(entry):
    try:
        return float(entry.last_error_reset_at or 0)
    except (TypeError, ValueError):
        return 0.0


def close_route(error, *, model, provider=''):
    """Closes the route this execution is on, for whoever owns the execution (a card's worker, the turn of an
    inbox entry; any other session closes nothing), with the soonest reset time the pool keeps.

    The refusal replayed is the one whose window ends first; one with no reset time on record names no window.
    Returns what ``on_quota_error`` answered, ``False`` when the pool still has a credential to offer (then the
    resolution failed for another reason and nothing is closed). Never raises."""
    try:
        from agent.credential_pool import STATUS_EXHAUSTED, load_pool
        from hermes_cli import nfos_route_walls

        provider = getattr(error, 'provider', '') or provider
        pool = load_pool(provider)
        refused = [entry for entry in pool.entries() if entry.last_status == STATUS_EXHAUSTED]
        if not refused or pool.has_available():
            return False
        now = time.time()
        ahead = [entry for entry in refused if _reset_of(entry) > now]
        soonest = min(ahead, key=_reset_of) if ahead else refused[0]
        session = SimpleNamespace(provider=provider, model=model, _credential_pool=pool, _fallback_activated=False,
                                  _has_pending_fallback=lambda: False)
        verdict = nfos_route_walls.on_quota_error(session, RecordedQuotaRefusal(
            soonest.last_error_reason or getattr(error, 'code', None) or 'rate_limit', _reset_of(soonest) or None))
        logger.warning('POOL_WALL_20261012 no credential resolved on %s/%s: all %d of the pool are inside a usage '
                       'window', provider, model, len(refused))
        return verdict
    except Exception:  # noqa: BLE001 - the caller's exit stands; a route left unmarked is launched on as before
        logger.warning('POOL_WALL_20261012 could not close the route of an unresolved launch', exc_info=True)
        return False


def unresolved_turn(error, *, model):
    """What a gateway turn whose route did not resolve adds to its result. For a pool inside its usage windows:
    the two keys of a turn that ended on the provider's quota (``nfos_route_walls.quota_failure``), with the route
    closed by then for the inbox entry whose turn it was. For any other failure: nothing."""
    cause = quota_cause(error)
    if cause is None:
        return {}
    close_route(cause, model=model)
    return {'failed': True, 'failure_reason': 'rate_limit'}
