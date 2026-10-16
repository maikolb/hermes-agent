"""Stream diagnostics — per-attempt counters, exception chains, retry logging.

When a streaming chat-completions request dies mid-response, we want to
know why: which Cloudflare edge served the request, which OpenRouter
downstream provider answered, how many bytes/chunks we got before the
drop, the HTTP status, the underlying httpx error class.  These helpers
collect that info and emit it both to ``agent.log`` (full detail) and to
the user-facing status line (compact).

All helpers are extracted from :class:`AIAgent` for cleanliness.
``run_agent`` keeps thin forwarder methods so existing call sites and
tests that patch ``run_agent.<helper>`` keep working.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Per-attempt stream diagnostic headers.  Lowercased; httpx returns
# CIMultiDict so case-insensitive lookups already work, but we read .get()
# on the dict from agent.log for free-form post-hoc analysis.
STREAM_DIAG_HEADERS = (
    "cf-ray",
    "cf-cache-status",
    "x-openrouter-provider",
    "x-openrouter-model",
    "x-openrouter-id",
    "x-request-id",
    "x-vercel-id",
    "via",
    "server",
    "x-forwarded-for",
)


def stream_diag_init() -> Dict[str, Any]:
    """Return a fresh per-attempt diagnostic dict.

    Mutated in-place by the streaming functions and read from the retry
    block when a stream dies.  Lives on ``request_client_holder`` so it
    survives across the closure boundary.
    """
    return {
        "started_at": time.time(),
        "first_chunk_at": None,
        "chunks": 0,
        "bytes": 0,
        "headers": {},
        "http_status": None,
    }


def stream_diag_capture_response(agent: Any, diag: Dict[str, Any], http_response: Any) -> None:
    """Snapshot interesting headers + HTTP status from the live stream.

    Called once at stream open (before iterating chunks) so the metadata
    survives even if the stream dies before any chunk arrives.  Failures
    are swallowed — diag is best-effort.
    """
    if http_response is None or not isinstance(diag, dict):
        return
    try:
        diag["http_status"] = getattr(http_response, "status_code", None)
    except Exception:
        pass
    try:
        headers = getattr(http_response, "headers", None) or {}
        captured: Dict[str, str] = {}
        # Allow per-agent override of the headers list (back-compat).
        target_headers = getattr(agent, "_STREAM_DIAG_HEADERS", STREAM_DIAG_HEADERS)
        for name in target_headers:
            try:
                val = headers.get(name)
                if val:
                    # Truncate single-value to keep log lines bounded.
                    captured[name] = str(val)[:120]
            except Exception:
                continue
        diag["headers"] = captured
    except Exception:
        pass


# ── Geração e provedor de origem do OpenRouter na linha "API call #N" ────────
#
# NFOS_API_CALL_GEN_UPSTREAM_20261016: a linha de log de cada chamada passa a
# terminar em ``gen=<id> upstream=<provedor>`` quando o OpenRouter devolve os
# dois dados. O medidor cruza o id com a cobrança da geração. Sem o dado, nada
# é acrescentado e a linha fica idêntica à de antes.

# Alfabeto de um id de geração (``gen-1a2b...``) e de um nome de provedor
# (``Anthropic``, ``Google AI Studio``). Qualquer coisa fora disso, quebra de
# linha e espaço no id inclusive, descarta o campo: o valor vem da rede e não
# pode abrir uma segunda linha no log.
_GEN_ID_RE = re.compile(r"[A-Za-z0-9._:\-]{1,128}")
_UPSTREAM_RE = re.compile(r"[A-Za-z0-9._:/+\-]{1,64}")
# Ids que o próprio Hermes fabrica; não são geração do provedor.
_SYNTHETIC_ID_PREFIXES = ("stream-", "partial-stream-stub")


def _clean_gen_id(value: Any) -> Optional[str]:
    if isinstance(value, str) and _GEN_ID_RE.fullmatch(value):
        if not value.startswith(_SYNTHETIC_ID_PREFIXES):
            return value
    return None


def _clean_upstream(value: Any) -> Optional[str]:
    # "Google AI Studio" vira "Google_AI_Studio" para a linha seguir em
    # chave=valor. Só o espaço simples é aceito; quebra de linha, tab e o resto
    # descartam o campo.
    if isinstance(value, str):
        value = value.replace(" ", "_")
        if _UPSTREAM_RE.fullmatch(value):
            return value
    return None


def extra_field(obj: Any, name: str) -> Any:
    """Read a provider-specific field the OpenAI SDK keeps as an extra.

    OpenRouter's ``provider`` is not part of the OpenAI schema; the SDK models
    allow it as an extra (attribute access) and list it in ``model_extra``.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get(name)
    return value


def attach_stream_generation(
    mock_response: Any,
    *,
    chunk_id: Any,
    chunk_provider: Any,
    headers: Any,
) -> None:
    """Carry the real generation id and upstream provider on a streamed response.

    The streaming path builds its own response with a made-up ``stream-<uuid>``
    id. The real values are the last chunk's ``id`` and ``provider``; when the
    chunks do not carry them, the headers captured at stream open
    (``x-openrouter-id``, ``x-openrouter-provider``) fill in.
    """
    hdrs = headers if isinstance(headers, dict) else {}
    try:
        mock_response.openrouter_generation_id = _clean_gen_id(chunk_id) or _clean_gen_id(
            hdrs.get("x-openrouter-id")
        )
        mock_response.openrouter_upstream_provider = _clean_upstream(
            chunk_provider
        ) or _clean_upstream(hdrs.get("x-openrouter-provider"))
    except Exception:
        pass


def generation_log_suffix(agent: Any, response: Any) -> str:
    """Return `` gen=<id> upstream=<provedor>`` or ``""`` when there is no data.

    Only OpenRouter answers carry the data; other providers (and the Codex
    adapter) get an empty string so their line stays exactly as it was.
    """
    try:
        is_openrouter = getattr(agent, "provider", None) == "openrouter"
        if not is_openrouter:
            check = getattr(agent, "_is_openrouter_url", None)
            is_openrouter = bool(check()) if callable(check) else False
        if not is_openrouter:
            return ""
        gen_id = _clean_gen_id(getattr(response, "openrouter_generation_id", None)) or (
            _clean_gen_id(getattr(response, "id", None))
        )
        upstream = _clean_upstream(getattr(response, "openrouter_upstream_provider", None)) or (
            _clean_upstream(extra_field(response, "provider"))
        )
        parts = []
        if gen_id:
            parts.append(f"gen={gen_id}")
        if upstream:
            parts.append(f"upstream={upstream}")
        return (" " + " ".join(parts)) if parts else ""
    except Exception:
        return ""


def flatten_exception_chain(error: BaseException) -> str:
    """Return a compact ``Outer(msg) <- Inner(msg) <- ...`` rendering.

    OpenAI SDK wraps httpx errors as ``APIConnectionError`` /
    ``APIError`` and only the wrapper's class is visible at the catch
    site — but the underlying ``RemoteProtocolError`` /
    ``ConnectError`` / ``ReadError`` is what tells us WHY the stream
    died.  Walks ``__cause__`` then ``__context__`` (deduped, max 4
    deep) to surface the chain in one line.
    """
    seen: List[BaseException] = []
    link: Optional[BaseException] = error
    while link is not None and len(seen) < 4:
        if link in seen:
            break
        seen.append(link)
        nxt = getattr(link, "__cause__", None) or getattr(
            link, "__context__", None
        )
        if nxt is None or nxt is link:
            break
        link = nxt
    parts: List[str] = []
    for e in seen:
        msg = str(e).strip().replace("\n", " ")
        if len(msg) > 140:
            msg = msg[:140] + "…"
        parts.append(f"{type(e).__name__}({msg})" if msg else type(e).__name__)
    return " <- ".join(parts) if parts else type(error).__name__


def log_stream_retry(
    agent: Any,
    *,
    kind: str,
    error: BaseException,
    attempt: int,
    max_attempts: int,
    mid_tool_call: bool,
    diag: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a transient stream-drop and retry to ``agent.log``.

    Always logs a structured WARNING so users have a breadcrumb regardless
    of UI verbosity.  Subagents in particular benefit because their
    retries no longer spam the parent's terminal — but the file log keeps
    full detail (provider, error class, attempt, base_url, subagent_id).

    When *diag* is provided (the per-attempt stream-diagnostic dict from
    :func:`stream_diag_init`), the WARNING also captures upstream headers
    (cf-ray, x-openrouter-provider, x-openrouter-id), HTTP status, bytes
    streamed before the drop, and elapsed time on the dying attempt.
    These are the breadcrumbs needed to answer "is one CF edge / one
    downstream provider responsible, or is it random across runs?"
    """
    try:
        try:
            _summary = agent._summarize_api_error(error)
        except Exception:
            _summary = str(error)
        if _summary and len(_summary) > 240:
            _summary = _summary[:240] + "…"

        # Inner-cause chain (httpx errors hide under openai.APIError).
        try:
            _chain = flatten_exception_chain(error)
        except Exception:
            _chain = type(error).__name__

        # Per-attempt counters and upstream headers.
        _now = time.time()
        _bytes = 0
        _chunks = 0
        _elapsed = 0.0
        _ttfb = None
        _headers_repr = "-"
        _http_status = "-"
        if isinstance(diag, dict):
            try:
                _bytes = int(diag.get("bytes") or 0)
                _chunks = int(diag.get("chunks") or 0)
                _started = float(diag.get("started_at") or _now)
                _elapsed = max(0.0, _now - _started)
                _first = diag.get("first_chunk_at")
                if _first is not None:
                    _ttfb = max(0.0, float(_first) - _started)
                headers = diag.get("headers") or {}
                if isinstance(headers, dict) and headers:
                    _headers_repr = " ".join(
                        f"{k}={v}" for k, v in headers.items()
                    )
                if diag.get("http_status") is not None:
                    _http_status = str(diag.get("http_status"))
            except Exception:
                pass

        logger.warning(
            "Stream %s on attempt %s/%s — retrying. "
            "subagent_id=%s depth=%s provider=%s base_url=%s "
            "error_type=%s error=%s "
            "chain=%s "
            "http_status=%s bytes=%d chunks=%d elapsed=%.2fs ttfb=%s "
            "upstream=[%s]",
            kind,
            attempt,
            max_attempts,
            getattr(agent, "_subagent_id", None) or "-",
            getattr(agent, "_delegate_depth", 0),
            agent.provider or "-",
            agent.base_url or "-",
            type(error).__name__,
            _summary,
            _chain,
            _http_status,
            _bytes,
            _chunks,
            _elapsed,
            f"{_ttfb:.2f}s" if _ttfb is not None else "-",
            _headers_repr,
            extra={"mid_tool_call": mid_tool_call},
        )
    except Exception:
        logger.debug("stream-retry log emit failed", exc_info=True)


def emit_stream_drop(
    agent: Any,
    *,
    error: BaseException,
    attempt: int,
    max_attempts: int,
    mid_tool_call: bool,
    diag: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a single user-visible line for a stream drop+retry.

    Both top-level agents and subagents announce drops in the UI — the
    parent prefixes subagent lines with ``[subagent-N]`` via ``log_prefix``
    so they're easy to attribute.  All cases also write a structured
    WARNING to ``agent.log`` via :func:`log_stream_retry` with the full
    diagnostic detail (subagent_id, provider, base_url, error_type,
    cf-ray, x-openrouter-provider, bytes/chunks, elapsed) for post-hoc
    analysis.

    The user-visible status line is intentionally compact: provider,
    error class, attempt N/M, plus ``after Xs`` when the stream dropped
    mid-flight.  Full diagnostic detail goes to ``agent.log`` only —
    ``hermes logs --level WARNING | grep "Stream drop"`` to inspect.
    """
    kind = "drop mid tool-call" if mid_tool_call else "drop"
    log_stream_retry(
        agent,
        kind=kind,
        error=error,
        attempt=attempt,
        max_attempts=max_attempts,
        mid_tool_call=mid_tool_call,
        diag=diag,
    )
    provider = agent.provider or "provider"
    # Compose a brief "after Xs" suffix when we have timing data — helps
    # the user distinguish "couldn't connect" (0s) from "died after 30s
    # of streaming" (likely upstream idle-kill or proxy timeout).
    _suffix = ""
    if isinstance(diag, dict):
        try:
            started = diag.get("started_at")
            if started is not None:
                _suffix = f" after {max(0.0, time.time() - float(started)):.1f}s"
        except Exception:
            pass
    try:
        agent._buffer_status(
            f"⚠️ {provider} stream {kind} ({type(error).__name__}){_suffix} "
            f"— reconnecting, retry {attempt}/{max_attempts}"
        )
        agent._touch_activity(
            f"stream retry {attempt}/{max_attempts} "
            f"after {type(error).__name__}"
        )
    except Exception:
        pass


__all__ = [
    "STREAM_DIAG_HEADERS",
    "stream_diag_init",
    "stream_diag_capture_response",
    "attach_stream_generation",
    "extra_field",
    "generation_log_suffix",
    "flatten_exception_chain",
    "log_stream_retry",
    "emit_stream_drop",
]
