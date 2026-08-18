"""Honcho client initialization and configuration.

Resolution order for config file:
  1. $HERMES_HOME/honcho.json  (instance-local, enables isolated Hermes instances)
  2. ~/.honcho/config.json     (global, shared across all Honcho-enabled apps)
  3. Environment variables     (HONCHO_API_KEY, HONCHO_ENVIRONMENT)

Resolution order for host-specific settings:
  1. Explicit host block fields (always win)
  2. Flat/global fields from config root
  3. Defaults (host name as workspace/peer)
"""

from __future__ import annotations

import json
import os
import logging
import hashlib
import ipaddress
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from hermes_constants import get_hermes_home
from hermes_cli.profiles import _get_default_hermes_home
from plugins.plugin_utils import SingletonSlot
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from honcho import Honcho

logger = logging.getLogger(__name__)

HOST = "hermes"


def profile_host_key(profile: str | None) -> str:
    """Return the safe Honcho host key for a Hermes profile."""
    if not profile or profile in {"default", "custom"}:
        return HOST
    sanitized = "".join(c if c.isalnum() or c in "_-" else "_" for c in profile).strip("_")
    return f"{HOST}_{sanitized or 'profile'}"


def _host_block(raw: dict, host: str) -> dict:
    """Return host config, accepting legacy dot-form profile host keys."""
    hosts = raw.get("hosts") or {}
    block = hosts.get(host, {})
    if block or not host.startswith(f"{HOST}_"):
        return block
    legacy = f"{HOST}.{host[len(HOST) + 1:]}"
    return hosts.get(legacy, {})


def resolve_active_host() -> str:
    """Derive the Honcho host key from the active Hermes profile.

    Resolution order:
      1. HERMES_HONCHO_HOST env var (explicit override)
      2. Active profile name via profiles system -> ``hermes_<profile>``
      3. defaultHost from the active config, but only for the default profile
      4. Fallback: ``"hermes"`` (default profile)
    """
    explicit = os.environ.get("HERMES_HONCHO_HOST", "").strip()
    if explicit:
        return explicit

    try:
        from hermes_cli.profiles import get_active_profile_name
        profile = get_active_profile_name()
        profile_host = profile_host_key(profile)
    except Exception:
        profile_host = HOST

    # Honcho's generic config can carry a defaultHost (for example "local"),
    # but applying it before profile resolution makes every named Hermes
    # profile share that same host.  Keep named profiles isolated; only the
    # default Hermes profile may opt into the config's default host.
    if profile_host == HOST:
        try:
            path = resolve_config_path()
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                default_host = str(raw.get("defaultHost", "")).strip()
                if default_host:
                    return default_host
        except Exception:
            pass

    return profile_host


def resolve_global_config_path() -> Path:
    """Return the shared Honcho config path for the current HOME."""
    return Path.home() / ".honcho" / "config.json"


def resolve_config_path() -> Path:
    """Return the active Honcho config path.

    Resolution order:
      1. $HERMES_HOME/honcho.json      (profile-local, if it exists)
      2. ~/.hermes/honcho.json          (default profile — shared host blocks live here)
      3. ~/.honcho/config.json          (global, cross-app interop)

    Returns the global path if none exist (for first-time setup writes).
    """
    local_path = get_hermes_home() / "honcho.json"
    if local_path.exists():
        return local_path

    # Default profile's config — host blocks accumulate here via setup/clone
    default_path = _get_default_hermes_home() / "honcho.json"
    if default_path != local_path and default_path.exists():
        return default_path

    return resolve_global_config_path()


_RECALL_MODE_ALIASES = {"auto": "hybrid"}
_VALID_RECALL_MODES = {"hybrid", "context", "tools"}


def _normalize_recall_mode(val: str) -> str:
    """Normalize legacy recall mode values (e.g. 'auto' → 'hybrid')."""
    val = _RECALL_MODE_ALIASES.get(val, val)
    return val if val in _VALID_RECALL_MODES else "hybrid"


def _resolve_bool(*vals, default: bool) -> bool:
    """Resolve a bool config field: first non-None wins, else default.

    Variadic to support aliased keys (e.g. ``pinUserPeer`` shadowing
    ``pinPeerName`` for backwards compatibility).  Pass values in
    precedence order: caller's preferred alias first, then fallback
    aliases, in (host, root) interleaving as needed.
    """
    for val in vals:
        if val is not None:
            return bool(val)
    return default


def _parse_context_tokens(host_val, root_val) -> int | None:
    """Parse contextTokens: host wins, then root, then None (uncapped)."""
    for val in (host_val, root_val):
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    return None


def _parse_int_config(host_val, root_val, default: int) -> int:
    """Parse an integer config: host wins, then root, then default."""
    for val in (host_val, root_val):
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    return default


def _parse_float_config(host_val, root_val, default: float) -> float:
    """Parse a float config: host wins, then root, then default. Clamped ≥ 0."""
    for val in (host_val, root_val):
        if val is not None:
            try:
                return max(0.0, float(val))
            except (ValueError, TypeError):
                pass
    return default


def _parse_string_map(host_obj: dict, root_obj: dict, key: str) -> dict[str, str]:
    """Parse a string-to-string map with host-level whole-map override."""
    source = host_obj[key] if key in host_obj else root_obj.get(key)
    if not isinstance(source, dict):
        return {}

    result: dict[str, str] = {}
    for raw_key, raw_value in source.items():
        alias_key = str(raw_key).strip()
        alias_value = str(raw_value).strip() if raw_value is not None else ""
        if alias_key and alias_value:
            result[alias_key] = alias_value
    return result


def _parse_optional_string(
    host_obj: dict, root_obj: dict, key: str, default: str = ""
) -> str:
    """Parse a string field where host-level empty string can override root."""
    if key in host_obj:
        value = host_obj.get(key)
    else:
        value = root_obj.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _parse_dialectic_depth(host_val, root_val) -> int:
    """Parse dialecticDepth: host wins, then root, then 1. Clamped to 1-3."""
    for val in (host_val, root_val):
        if val is not None:
            try:
                return max(1, min(int(val), 3))
            except (ValueError, TypeError):
                pass
    return 1


_VALID_REASONING_LEVELS = ("minimal", "low", "medium", "high", "max")


def _parse_dialectic_depth_levels(host_val, root_val, depth: int) -> list[str] | None:
    """Parse dialecticDepthLevels: optional array of reasoning levels per pass.

    Returns None when not configured (use proportional defaults).
    When configured, validates each level and truncates/pads to match depth.
    """
    for val in (host_val, root_val):
        if val is not None and isinstance(val, list):
            levels = [
                lvl if lvl in _VALID_REASONING_LEVELS else "low"
                for lvl in val[:depth]
            ]
            # Pad with "low" if array is shorter than depth
            while len(levels) < depth:
                levels.append("low")
            return levels
    return None


# Default HTTP timeout (seconds) applied when no explicit timeout is
# configured via HonchoClientConfig.timeout, honcho.timeout / requestTimeout,
# or HONCHO_TIMEOUT. Honcho calls happen on the post-response path of
# run_conversation; without a cap the agent can block indefinitely when
# the Honcho backend is unreachable, preventing the gateway from
# delivering the already-generated response.
_DEFAULT_HTTP_TIMEOUT = 30.0


def _is_local_base_url(base_url: str | None) -> bool:
    """Return True for loopback/LAN/VPN self-hosted Honcho URLs.

    Local Honcho deployments can run without auth, but the SDK requires a
    non-empty api_key argument.  Treat loopback plus RFC1918/link-local/ULA
    and carrier-grade-NAT IPs as local so LAN/VPN URLs such as
    ``http://192.168.2.112:8000`` get the same placeholder-key behavior as
    localhost.
    """
    if not base_url:
        return False

    try:
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").strip().lower()
    except Exception:
        host = ""

    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if not host:
        return False

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False

    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True

    # Tailscale/other VPN setups often sit in carrier-grade NAT space.
    if ip.version == 4 and ipaddress.ip_address("100.64.0.0") <= ip <= ipaddress.ip_address("100.127.255.255"):
        return True

    return False


def _resolve_optional_float(*values: Any) -> float | None:
    """Return the first non-empty value coerced to a positive float."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


_VALID_OBSERVATION_MODES = {"unified", "directional"}
_OBSERVATION_MODE_ALIASES = {"shared": "unified", "separate": "directional", "cross": "directional"}


def _normalize_observation_mode(val: str) -> str:
    """Normalize observation mode values."""
    val = _OBSERVATION_MODE_ALIASES.get(val, val)
    return val if val in _VALID_OBSERVATION_MODES else "directional"


# Observation presets — granular booleans derived from legacy string mode.
# Explicit per-peer config always wins over presets.
_OBSERVATION_PRESETS = {
    "directional": {
        "user_observe_me": True, "user_observe_others": True,
        "ai_observe_me": True, "ai_observe_others": True,
    },
    "unified": {
        "user_observe_me": True, "user_observe_others": False,
        "ai_observe_me": False, "ai_observe_others": True,
    },
}


def _resolve_observation(
    mode: str,
    observation_obj: dict | None,
) -> dict:
    """Resolve per-peer observation booleans.

    Config forms:
      String shorthand:  ``"observationMode": "directional"``
      Granular object:   ``"observation": {"user": {"observeMe": true, "observeOthers": true},
                                           "ai": {"observeMe": true, "observeOthers": false}}``

    Granular fields override preset defaults.
    """
    preset = _OBSERVATION_PRESETS.get(mode, _OBSERVATION_PRESETS["directional"])
    if not observation_obj or not isinstance(observation_obj, dict):
        return dict(preset)

    user_block = observation_obj.get("user") or {}
    ai_block = observation_obj.get("ai") or {}

    return {
        "user_observe_me": user_block.get("observeMe", preset["user_observe_me"]),
        "user_observe_others": user_block.get("observeOthers", preset["user_observe_others"]),
        "ai_observe_me": ai_block.get("observeMe", preset["ai_observe_me"]),
        "ai_observe_others": ai_block.get("observeOthers", preset["ai_observe_others"]),
    }


def _bound_session_value(name: str) -> str | None:
    """Read only a value explicitly bound in this task's session ContextVars.

    ``get_session_env`` intentionally falls back to ``os.environ`` for CLI
    compatibility.  That fallback is unsafe for workspace partitioning in a
    multiplexed gateway: a fresh worker thread has no inherited ContextVars and
    could otherwise consume stale process-global identity.  Returning ``None``
    for the unbound sentinel lets callers distinguish that worker-thread case
    from an explicitly bound empty value.
    """
    try:
        from gateway import session_context

        var = session_context._VAR_MAP.get(name)
        if var is None:
            return None
        value = var.get()
        if value is session_context._UNSET:
            return None
        return str(value)
    except Exception:
        return None


def _telegram_project_workspace_id(
    base_workspace: str,
) -> tuple[bool, str | None]:
    """Return task-binding state and a digest-only Telegram project workspace.

    The digest uses length-framed inputs so no two component tuples can be
    confused by delimiters.  Raw profile/chat/project identifiers are never
    retained in the returned identifier or the client cache key.
    """
    platform = _bound_session_value("HERMES_SESSION_PLATFORM")
    if platform is None:
        return False, None

    profile = _bound_session_value("HERMES_SESSION_PROFILE")
    chat_id = _bound_session_value("HERMES_SESSION_CHAT_ID")
    project_id = _bound_session_value("HERMES_PROJECT_ID")
    if (
        platform.strip().lower() != "telegram"
        or profile is None
        or not (chat_id or "").strip()
        or not (project_id or "").strip()
    ):
        return True, None

    digest = hashlib.sha256(b"honcho-telegram-project-workspace-v1")
    for component in (base_workspace, profile, chat_id, project_id):
        encoded = component.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return True, digest.hexdigest()


def _capture_bound_workspace_id(base_workspace: str) -> str | None:
    """Capture only the derived digest for use by non-context-propagating threads."""
    _, scoped_workspace = _telegram_project_workspace_id(base_workspace)
    return scoped_workspace





@dataclass
class HonchoClientConfig:
    """Configuration for Honcho client, resolved for a specific host."""

    host: str = HOST
    workspace_id: str = "hermes"
    api_key: str | None = None
    environment: str = "production"
    # Optional base URL for self-hosted Honcho (overrides environment mapping)
    base_url: str | None = None
    # Optional request timeout in seconds for Honcho SDK HTTP calls
    timeout: float | None = None
    # Identity
    peer_name: str | None = None
    ai_peer: str = "hermes"
    # When True, ``peer_name`` wins over any gateway-supplied runtime
    # identity (Telegram UID, Discord ID, …) when resolving the user peer.
    # This keeps memory unified across platforms for single-user deployments
    # where Honcho's one peer-name is an unambiguous identity — otherwise
    # each platform would fork memory into its own peer (#14984).  Default
    # ``False`` preserves existing multi-user behaviour.
    pin_peer_name: bool = False
    # Map gateway runtime user IDs to stable Honcho user peers. Host-level
    # config replaces the root map as a whole so profiles can intentionally
    # own their identity mappings.
    user_peer_aliases: dict[str, str] = field(default_factory=dict)
    # Optional prefix for unknown gateway runtime user IDs, e.g. "telegram_".
    runtime_peer_prefix: str = ""
    # Toggles
    enabled: bool = False
    save_messages: bool = True
    # Write frequency: "async" (background thread), "turn" (sync per turn),
    # "session" (flush on session end), or int (every N turns)
    write_frequency: str | int = "async"
    # Prefetch budget (None = no cap; set to an integer to bound auto-injected context)
    context_tokens: int | None = None
    # Dialectic (peer.chat) settings
    # reasoning_level: "minimal" | "low" | "medium" | "high" | "max"
    dialectic_reasoning_level: str = "low"
    # When true, the model can override reasoning_level per-call via the
    # honcho_reasoning tool param (agentic). When false, always uses
    # dialecticReasoningLevel and ignores model-provided overrides.
    dialectic_dynamic: bool = True
    # Automatic-injection cap; explicit honcho_reasoning calls bypass it.
    dialectic_max_chars: int = 600
    # Dialectic depth: how many .chat() calls per dialectic cycle (1-3).
    # Depth 1: single call. Depth 2: self-audit + targeted synthesis.
    # Depth 3: self-audit + synthesis + reconciliation.
    dialectic_depth: int = 1
    # Optional per-pass reasoning level override. Array of reasoning levels
    # matching dialectic_depth length. When None, uses proportional defaults
    # derived from dialectic_reasoning_level.
    dialectic_depth_levels: list[str] | None = None
    # When true, the auto-injected dialectic scales reasoning level up on
    # longer queries. See HonchoMemoryProvider for thresholds.
    reasoning_heuristic: bool = True
    # Ceiling for the heuristic-selected reasoning level.
    reasoning_level_cap: str = "high"
    # Honcho API limits — configurable for self-hosted instances
    # Max chars per message sent via add_messages() (Honcho cloud: 25000)
    message_max_chars: int = 25000
    # Max chars for dialectic query input to peer.chat() (Honcho cloud: 10000)
    dialectic_max_input_chars: int = 10000
    # Recall mode: how memory retrieval works when Honcho is active.
    # "hybrid"  — auto-injected context + Honcho tools available (model decides)
    # "context" — auto-injected context only, Honcho tools removed
    # "tools"   — Honcho tools only, no auto-injected context
    recall_mode: str = "hybrid"
    # Eager init in tools mode — when true, initializes session during
    # initialize() instead of deferring to first tool call
    init_on_session_start: bool = False
    # Injection frequency: "every-turn" (default) or "first-turn" (inject only on turn 1)
    injection_frequency: str = "every-turn"
    # Minimum turns between peer.context() API calls (base layer refresh cadence)
    context_cadence: int = 1
    # Minimum turns between dialectic prefetch fires (supplement layer cadence)
    dialectic_cadence: int = 1
    # Rewrite the latest user message into a retrieval query before dialectic.
    # Off by default: adds one auxiliary LLM call per dialectic fire
    # (model/timeout under auxiliary.memory_query_rewrite in config.yaml).
    query_rewrite: bool = False
    # Bounded synchronous waits on turn 1, in seconds. 0 disables the wait
    # entirely (fully async first turn; context surfaces on later turns).
    first_turn_base_wait: float = 3.0
    first_turn_dialectic_wait: float = 2.0
    # Observation mode: legacy string shorthand ("directional" or "unified").
    # Kept for backward compat; granular per-peer booleans below are preferred.
    observation_mode: str = "directional"
    # Per-peer observation booleans — maps 1:1 to Honcho's SessionPeerConfig.
    # Resolved from "observation" object in config, falling back to observation_mode preset.
    user_observe_me: bool = True
    user_observe_others: bool = True
    ai_observe_me: bool = True
    ai_observe_others: bool = True
    # Session resolution
    session_strategy: str = "per-directory"
    session_peer_prefix: bool = False
    sessions: dict[str, str] = field(default_factory=dict)
    # Raw global config for anything else consumers need
    raw: dict[str, Any] = field(default_factory=dict)
    # True when Honcho was explicitly configured for this host (hosts.hermes
    # block exists or enabled was set explicitly), vs auto-enabled from a
    # stray HONCHO_API_KEY env var.
    explicitly_configured: bool = False
    # Digest-only effective workspace captured while gateway ContextVars are
    # bound.  Honcho owns background threads that do not inherit ContextVars;
    # retaining this non-sensitive value keeps those calls in the same scope.
    _bound_workspace_id: str | None = field(
        default=None, repr=False, compare=False
    )

    @classmethod
    def from_env(
        cls,
        workspace_id: str = "hermes",
        host: str | None = None,
    ) -> HonchoClientConfig:
        """Create config from environment variables (fallback)."""
        resolved_host = host or resolve_active_host()
        api_key = os.environ.get("HONCHO_API_KEY")
        base_url = os.environ.get("HONCHO_BASE_URL", "").strip() or None
        timeout = _resolve_optional_float(os.environ.get("HONCHO_TIMEOUT"))
        return cls(
            host=resolved_host,
            workspace_id=workspace_id,
            _bound_workspace_id=_capture_bound_workspace_id(workspace_id),
            api_key=api_key,
            environment=os.environ.get("HONCHO_ENVIRONMENT", "production"),
            base_url=base_url,
            timeout=timeout,
            ai_peer=resolved_host,
            enabled=bool(api_key or base_url),
        )

    @classmethod
    def from_global_config(
        cls,
        host: str | None = None,
        config_path: Path | None = None,
    ) -> HonchoClientConfig:
        """Create config from the resolved Honcho config path.

        Resolution: $HERMES_HOME/honcho.json -> ~/.honcho/config.json -> env vars.
        When host is None, derives it from the active Hermes profile.
        """
        resolved_host = host or resolve_active_host()
        path = config_path or resolve_config_path()
        if not path.exists():
            logger.debug("No global Honcho config at %s, falling back to env", path)
            return cls.from_env(host=resolved_host)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read %s: %s, falling back to env", path, e)
            return cls.from_env(host=resolved_host)

        host_block = _host_block(raw, resolved_host)
        # A hosts.hermes block or explicit enabled flag means the user
        # intentionally configured Honcho for this host.
        _explicitly_configured = bool(host_block) or raw.get("enabled") is True

        # Explicit host block fields win, then flat/global, then defaults
        workspace = (
            host_block.get("workspace")
            or raw.get("workspace")
            or resolved_host
        )
        ai_peer = (
            host_block.get("aiPeer")
            or raw.get("aiPeer")
            or resolved_host
        )
        api_key = (
            host_block.get("apiKey")
            or raw.get("apiKey")
            or os.environ.get("HONCHO_API_KEY")
        )

        environment = (
            host_block.get("environment")
            or raw.get("environment", "production")
        )

        base_url = (
            raw.get("baseUrl")
            or raw.get("base_url")
            or os.environ.get("HONCHO_BASE_URL", "").strip()
            or None
        )
        # Host config wins over flat/global config and environment.
        timeout = _resolve_optional_float(
            host_block.get("timeout"),
            host_block.get("requestTimeout"),
            raw.get("timeout"),
            raw.get("requestTimeout"),
            os.environ.get("HONCHO_TIMEOUT"),
        )

        # Auto-enable when API key or base_url is present (unless explicitly disabled)
        # Host-level enabled wins, then root-level, then auto-enable if key/url exists.
        host_enabled = host_block.get("enabled")
        root_enabled = raw.get("enabled")
        if host_enabled is not None:
            enabled = host_enabled
        elif root_enabled is not None:
            enabled = root_enabled
        else:
            # Not explicitly set anywhere -> auto-enable if API key or base_url exists
            enabled = bool(api_key or base_url)

        # write_frequency: accept int or string
        raw_wf = (
            host_block.get("writeFrequency")
            or raw.get("writeFrequency")
            or "async"
        )
        try:
            write_frequency: str | int = int(raw_wf)
        except (TypeError, ValueError):
            write_frequency = str(raw_wf)

        # saveMessages: host wins (None-aware since False is valid)
        host_save = host_block.get("saveMessages")
        save_messages = host_save if host_save is not None else raw.get("saveMessages", True)

        # sessionStrategy / sessionPeerPrefix: host first, root fallback
        session_strategy = (
            host_block.get("sessionStrategy")
            or raw.get("sessionStrategy", "per-directory")
        )
        host_prefix = host_block.get("sessionPeerPrefix")
        session_peer_prefix = (
            host_prefix if host_prefix is not None
            else raw.get("sessionPeerPrefix", False)
        )

        return cls(
            host=resolved_host,
            workspace_id=workspace,
            _bound_workspace_id=_capture_bound_workspace_id(str(workspace)),
            api_key=api_key,
            environment=environment,
            base_url=base_url,
            timeout=timeout,
            peer_name=host_block.get("peerName") or raw.get("peerName"),
            ai_peer=ai_peer,
            pin_peer_name=_resolve_bool(
                # ``pinUserPeer`` is the clearer name (the resolver pins
                # the user-side peer to ``peerName``, ignoring runtime
                # identity).  ``pinPeerName`` is the original key from
                # #14984 and stays accepted for backward compatibility.
                # Host-level keys win over root-level; among same-level
                # keys, ``pinUserPeer`` wins over ``pinPeerName``.
                host_block.get("pinUserPeer"),
                host_block.get("pinPeerName"),
                raw.get("pinUserPeer"),
                raw.get("pinPeerName"),
                default=False,
            ),
            user_peer_aliases=_parse_string_map(
                host_block,
                raw,
                "userPeerAliases",
            ),
            runtime_peer_prefix=_parse_optional_string(
                host_block,
                raw,
                "runtimePeerPrefix",
            ),
            enabled=enabled,
            save_messages=save_messages,
            write_frequency=write_frequency,
            context_tokens=_parse_context_tokens(
                host_block.get("contextTokens"),
                raw.get("contextTokens"),
            ),
            dialectic_reasoning_level=(
                host_block.get("dialecticReasoningLevel")
                or raw.get("dialecticReasoningLevel")
                or "low"
            ),
            dialectic_dynamic=_resolve_bool(
                host_block.get("dialecticDynamic"),
                raw.get("dialecticDynamic"),
                default=True,
            ),
            dialectic_max_chars=_parse_int_config(
                host_block.get("dialecticMaxChars"),
                raw.get("dialecticMaxChars"),
                default=600,
            ),
            dialectic_depth=_parse_dialectic_depth(
                host_block.get("dialecticDepth"),
                raw.get("dialecticDepth"),
            ),
            dialectic_depth_levels=_parse_dialectic_depth_levels(
                host_block.get("dialecticDepthLevels"),
                raw.get("dialecticDepthLevels"),
                depth=_parse_dialectic_depth(host_block.get("dialecticDepth"), raw.get("dialecticDepth")),
            ),
            reasoning_heuristic=_resolve_bool(
                host_block.get("reasoningHeuristic"),
                raw.get("reasoningHeuristic"),
                default=True,
            ),
            reasoning_level_cap=(
                host_block.get("reasoningLevelCap")
                or raw.get("reasoningLevelCap")
                or "high"
            ),
            message_max_chars=_parse_int_config(
                host_block.get("messageMaxChars"),
                raw.get("messageMaxChars"),
                default=25000,
            ),
            dialectic_max_input_chars=_parse_int_config(
                host_block.get("dialecticMaxInputChars"),
                raw.get("dialecticMaxInputChars"),
                default=10000,
            ),
            recall_mode=_normalize_recall_mode(
                host_block.get("recallMode")
                or raw.get("recallMode")
                or "hybrid"
            ),
            init_on_session_start=_resolve_bool(
                host_block.get("initOnSessionStart"),
                raw.get("initOnSessionStart"),
                default=False,
            ),
            # Host cadence settings override flat/global values.
            injection_frequency=(
                host_block.get("injectionFrequency")
                or raw.get("injectionFrequency", "every-turn")
            ),
            context_cadence=_parse_int_config(
                host_block.get("contextCadence"),
                raw.get("contextCadence"),
                default=1,
            ),
            dialectic_cadence=_parse_int_config(
                host_block.get("dialecticCadence"),
                raw.get("dialecticCadence"),
                default=1,
            ),
            query_rewrite=_resolve_bool(
                host_block.get("queryRewrite"),
                raw.get("queryRewrite"),
                default=False,
            ),
            first_turn_base_wait=_parse_float_config(
                host_block.get("firstTurnBaseWait"),
                raw.get("firstTurnBaseWait"),
                default=3.0,
            ),
            first_turn_dialectic_wait=_parse_float_config(
                host_block.get("firstTurnDialecticWait"),
                raw.get("firstTurnDialecticWait"),
                default=2.0,
            ),
            # Migration guard: existing configs without an explicit
            # observationMode keep the old "unified" default so users
            # aren't silently switched to full bidirectional observation.
            # New installations (no host block, no credentials) get
            # "directional" (all observations on) as the new default.
            observation_mode=_normalize_observation_mode(
                host_block.get("observationMode")
                or raw.get("observationMode")
                or ("unified" if _explicitly_configured else "directional")
            ),
            **_resolve_observation(
                _normalize_observation_mode(
                    host_block.get("observationMode")
                    or raw.get("observationMode")
                    or ("unified" if _explicitly_configured else "directional")
                ),
                host_block.get("observation") or raw.get("observation"),
            ),
            session_strategy=session_strategy,
            session_peer_prefix=session_peer_prefix,
            sessions=raw.get("sessions", {}),
            raw=raw,
            explicitly_configured=_explicitly_configured,
        )

    @staticmethod
    def _git_repo_name(cwd: str) -> str | None:
        """Return the git repo root directory name, or None if not in a repo."""
        import subprocess

        try:
            root = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, cwd=cwd, timeout=5,
                stdin=subprocess.DEVNULL,
            )
            if root.returncode == 0:
                return Path(root.stdout.strip()).name
        except (OSError, subprocess.TimeoutExpired):
            pass
        return None

    # Honcho enforces a 100-char limit on session IDs. Long gateway session keys
    # (Matrix "!room:server" + thread event IDs, Telegram supergroup reply
    # chains, Slack thread IDs with long workspace prefixes) can overflow this
    # limit after sanitization; the Honcho API then rejects every call for that
    # session with "session_id too long". See issue #13868.
    _HONCHO_SESSION_ID_MAX_LEN = 100
    _HONCHO_SESSION_ID_HASH_LEN = 8

    @classmethod
    def _enforce_session_id_limit(cls, sanitized: str, original: str) -> str:
        """Truncate a sanitized session ID to Honcho's 100-char limit.

        The common case (short keys) short-circuits with no modification.
        For over-limit keys, keep a prefix of the sanitized ID and append a
        deterministic ``-<sha256 prefix>`` suffix so two distinct long keys
        that share a leading segment don't collide onto the same truncated ID.
        The hash is taken over the *original* pre-sanitization key, so two
        inputs that sanitize to the same string still collide intentionally
        (same logical session), but two inputs that only share a prefix do not.
        """
        max_len = cls._HONCHO_SESSION_ID_MAX_LEN
        if len(sanitized) <= max_len:
            return sanitized

        hash_len = cls._HONCHO_SESSION_ID_HASH_LEN
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:hash_len]
        # max_len - hash_len - 1 (for the '-' separator) chars of the sanitized
        # prefix, then '-<hash>'. Strip any trailing hyphen from the prefix so
        # the result doesn't double up on separators.
        prefix_len = max_len - hash_len - 1
        prefix = sanitized[:prefix_len].rstrip("-")
        return f"{prefix}-{digest}"

    def resolve_session_name(
        self,
        cwd: str | None = None,
        session_title: str | None = None,
        session_id: str | None = None,
        gateway_session_key: str | None = None,
    ) -> str | None:
        """Resolve Honcho session name.

        Resolution order:
          1. Gateway session key (stable per-chat identifier from gateway platforms)
          2. per-session strategy — Hermes session_id ({timestamp}_{hex}); authoritative,
             so a generated title never remaps a live conversation
          3. Manual directory override from sessions map
          4. Hermes session title (from /title command; non-per-session)
          5. per-repo strategy — git repo root directory name
          6. per-directory strategy — directory basename
          7. global strategy — workspace name
        """
        import re

        if not cwd:
            cwd = os.getcwd()

        # Gateway per-chat key wins everywhere — gateways (telegram/discord/…)
        # need per-chat isolation no cwd/strategy name can provide.
        if gateway_session_key:
            sanitized = re.sub(r'[^a-zA-Z0-9_-]+', '-', gateway_session_key).strip('-')
            if sanitized:
                return self._enforce_session_id_limit(sanitized, gateway_session_key)

        # per-session: the run's session_id IS the identity — resolve before the
        # cwd map / title so an auto-generated title can't remap a live
        # conversation onto a second Honcho session mid-stream.
        if self.session_strategy == "per-session" and session_id:
            if self.session_peer_prefix and self.peer_name:
                return f"{self.peer_name}-{session_id}"
            return session_id

        # Manual override (cwd → name), for non-per-session strategies.
        manual = self.sessions.get(cwd)
        if manual:
            return manual

        # /title mid-session remap (non-per-session).
        if session_title:
            sanitized = re.sub(r'[^a-zA-Z0-9_-]+', '-', session_title).strip('-')
            if sanitized:
                if self.session_peer_prefix and self.peer_name:
                    return f"{self.peer_name}-{sanitized}"
                return sanitized

        # per-repo: one Honcho session per git repository
        if self.session_strategy == "per-repo":
            base = self._git_repo_name(cwd) or Path(cwd).name
            if self.session_peer_prefix and self.peer_name:
                return f"{self.peer_name}-{base}"
            return base

        # per-directory: one Honcho session per working directory (default)
        if self.session_strategy in {"per-directory", "per-session"}:
            base = Path(cwd).name
            if self.session_peer_prefix and self.peer_name:
                return f"{self.peer_name}-{base}"
            return base

        # global: single session across all directories
        return self.workspace_id


@dataclass(frozen=True)
class _HonchoClientSignature:
    """Non-secret identity for one reusable SDK client.

    The credential participates only through its SHA-256 digest.  Keeping the
    raw key out of cache keys also keeps it out of accidental cache/debug reprs.
    """

    host: str
    workspace_id: str
    base_url: str | None
    environment: str
    credential_sha256: bytes = field(repr=False)
    timeout: float


class _KeyedHonchoClientSlots:
    """Thread-safe singleton slots partitioned by client signature.

    ``get(factory)``/``peek()`` without a signature remain available for the
    old private ``_honcho_client_slot`` test surface.  Runtime acquisitions use
    ``signature=...`` and therefore never inherit another profile's client.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._slots: dict[_HonchoClientSignature, SingletonSlot[Any]] = {}
        self._legacy_slot: SingletonSlot[Any] = SingletonSlot()

    def get(
        self,
        factory: Callable[[], Any],
        *,
        signature: _HonchoClientSignature | None = None,
    ) -> Any:
        if signature is None:
            return self._legacy_slot.get(factory)
        with self._lock:
            slot = self._slots.get(signature)
            if slot is None:
                slot = SingletonSlot()
                self._slots[signature] = slot
        # Each key has its own lock, so unrelated profiles can build in
        # parallel while same-key callers still construct exactly once.
        return slot.get(factory)

    def peek(
        self, signature: _HonchoClientSignature | None = None
    ) -> Any | None:
        if signature is not None:
            with self._lock:
                slot = self._slots.get(signature)
            return slot.peek() if slot is not None else None

        legacy = self._legacy_slot.peek()
        if legacy is not None:
            return legacy
        with self._lock:
            cached = [
                value
                for slot in self._slots.values()
                if (value := slot.peek()) is not None
            ]
        return cached[0] if len(cached) == 1 else None

    def reset(
        self, signature: _HonchoClientSignature | None = None
    ) -> None:
        if signature is not None:
            with self._lock:
                slot = self._slots.pop(signature, None)
            if slot is not None:
                slot.reset()
            return

        with self._lock:
            slots = list(self._slots.values())
            self._slots.clear()
        self._legacy_slot.reset()
        for slot in slots:
            slot.reset()


_honcho_client_slot = _KeyedHonchoClientSlots()


def _normalize_sdk_base_url(base_url: str | None) -> str | None:
    if not base_url:
        return None
    import re as _re

    return _re.sub(r"/v\d+/*$", "", base_url).rstrip("/")


def _resolve_client_settings(
    config: HonchoClientConfig,
) -> tuple[str | None, float]:
    """Resolve the exact transport settings used by both cache and builder."""
    resolved_base_url = config.base_url
    resolved_timeout = config.timeout
    if not resolved_base_url or resolved_timeout is None:
        try:
            from hermes_cli.config import load_config_readonly

            honcho_cfg = load_config_readonly().get("honcho", {})
            if isinstance(honcho_cfg, dict):
                if not resolved_base_url:
                    configured_url = honcho_cfg.get("base_url")
                    if isinstance(configured_url, str):
                        resolved_base_url = configured_url.strip() or None
                if resolved_timeout is None:
                    resolved_timeout = _resolve_optional_float(
                        honcho_cfg.get("timeout"),
                        honcho_cfg.get("request_timeout"),
                    )
        except Exception:
            pass

    if resolved_timeout is None:
        resolved_timeout = _DEFAULT_HTTP_TIMEOUT
    return _normalize_sdk_base_url(resolved_base_url), resolved_timeout


def _effective_api_key(
    config: HonchoClientConfig, resolved_base_url: str | None
) -> str | None:
    """Return the credential the SDK will receive for these settings."""
    if not _is_local_base_url(resolved_base_url):
        return config.api_key

    # Local/LAN/VPN servers usually run without cloud auth.  Only an explicit
    # host-block key opts into local auth; otherwise use the SDK placeholder.
    raw = config.raw if isinstance(config.raw, dict) else {}
    host_block = _host_block(raw, config.host)
    return config.api_key if host_block.get("apiKey") else "local"


def _client_signature(
    config: HonchoClientConfig,
    effective_workspace_id: str,
    resolved_base_url: str | None,
    resolved_timeout: float,
    effective_api_key: str | None,
) -> _HonchoClientSignature:
    credential_sha256 = hashlib.sha256(
        (effective_api_key or "").encode("utf-8")
    ).digest()
    return _HonchoClientSignature(
        host=config.host,
        workspace_id=effective_workspace_id,
        base_url=resolved_base_url,
        environment=config.environment,
        credential_sha256=credential_sha256,
        timeout=resolved_timeout,
    )


def _effective_workspace_id(config: HonchoClientConfig) -> str:
    """Resolve the workspace before cache lookup without exposing raw IDs.

    An explicitly bound current task always wins.  If this call runs in one of
    Honcho's own worker threads (no ContextVars), reuse the digest captured on
    the config while the gateway context was still present.  Legacy/non-project
    contexts return the configured workspace unchanged.
    """
    context_bound, scoped_workspace = _telegram_project_workspace_id(
        str(config.workspace_id)
    )
    if context_bound:
        return scoped_workspace or config.workspace_id
    return config._bound_workspace_id or config.workspace_id


def _apply_fresh_oauth_token(config: HonchoClientConfig) -> None:
    """Refresh OAuth before cache lookup and point ``config.api_key`` at it.

    A rotated token changes the credential digest in the client signature, so
    the old SDK client is invalidated without ever storing the raw token in the
    cache key.  Static API keys and refresh failures remain fail-open.
    """
    try:
        from plugins.memory.honcho import oauth

        token, _ = oauth.ensure_fresh_token(resolve_config_path(), config.host)
        if token:
            config.api_key = token
    except Exception:
        logger.warning("Honcho OAuth pre-build refresh failed", exc_info=True)


def get_honcho_client(config: HonchoClientConfig | None = None) -> Honcho:
    """Get or create the Honcho client singleton for this exact signature.

    When no config is provided, attempts to load ~/.honcho/config.json
    first, falling back to environment variables.

    Thread-safe: each host/workspace/transport/credential signature is built
    exactly once even under concurrent first calls.  Different profiles never
    receive whichever client happened to initialize the old global slot first.
    """
    if config is None:
        config = HonchoClientConfig.from_global_config()

    # Refresh before deriving the signature: a token rotation must select a new
    # slot instead of returning an SDK client that still carries the old Bearer.
    _apply_fresh_oauth_token(config)

    if not config.api_key and not config.base_url:
        raise ValueError(
            "Honcho API key not found. "
            "Get your API key at https://app.honcho.dev, "
            "then run 'hermes honcho setup' or set HONCHO_API_KEY. "
            "For local instances, set HONCHO_BASE_URL instead."
        )

    resolved_base_url, resolved_timeout = _resolve_client_settings(config)
    effective_api_key = _effective_api_key(config, resolved_base_url)
    resolved_workspace = _effective_workspace_id(config)
    signature = _client_signature(
        config,
        resolved_workspace,
        resolved_base_url,
        resolved_timeout,
        effective_api_key,
    )

    # Snapshot every constructor input before entering the per-key slot.  The
    # caller may reuse/mutate its config object (OAuth refresh does), but that
    # must never make a client disagree with the signature under which it lives.
    resolved_host = config.host
    resolved_environment = config.environment

    # Build inside the keyed singleton factory so same-signature racers share
    # one client while unrelated profiles can initialize concurrently.
    def _build() -> "Honcho":
        # Lazy dependency failures fall through to the canonical import error.
        try:
            from tools.lazy_deps import FeatureUnavailable, ensure as _lazy_ensure
            _lazy_ensure("memory.honcho", prompt=False)
        except ImportError:
            # lazy_deps module missing — fall through to the raw import below.
            pass
        except Exception:
            # FeatureUnavailable or unexpected error. Don't crash here; let the
            # actual import attempt produce the canonical error message.
            pass

        try:
            from honcho import Honcho
        except ImportError:
            raise ImportError(
                "honcho-ai is required for Honcho integration. "
                "Install it with: pip install honcho-ai  "
                "(or run `hermes honcho setup` to configure)."
            )

        if resolved_base_url:
            logger.info(
                "Initializing Honcho client (base_url: %s, workspace: %s)",
                resolved_base_url,
                resolved_workspace,
            )
        else:
            logger.info(
                "Initializing Honcho client (host: %s, workspace: %s)",
                resolved_host,
                resolved_workspace,
            )

        kwargs: dict = {
            "workspace_id": resolved_workspace,
            "api_key": effective_api_key,
            "environment": resolved_environment,
        }
        if resolved_base_url:
            kwargs["base_url"] = resolved_base_url
        kwargs["timeout"] = resolved_timeout
        return Honcho(**kwargs)

    return _honcho_client_slot.get(_build, signature=signature)


def reset_honcho_client() -> None:
    """Reset every keyed Honcho client singleton (useful for testing)."""
    _honcho_client_slot.reset()
