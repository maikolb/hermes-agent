#!/usr/bin/env python3
"""Native-hook entrypoint for AOF route and scope enforcement.

The host invokes Python directly. On Windows installers select pythonw.exe so
lifecycle hooks never materialize a console window or launch a shell.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MUTATION_TOOL = re.compile(
    r"(apply_patch|search_replace|(?:^|__)(write|edit|delete|move|rename|create)(?:_|$)|Write$|Edit$|NotebookEdit$)",
    re.IGNORECASE,
)
ROUTE_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File:\s*(\S.*)$")
PATCH_MOVE = re.compile(r"^\*\*\* Move to:\s*(\S.*)$")
SCOPE_SECTION = re.compile(r"(?ms)^#{2,3} In Scope\s*\r?\n(.*?)(?=^#{1,3} |\Z)")
BACKTICK_SPAN = re.compile(r"`([^`]+)`")
ZERO_RESULT = re.compile(
    r"(?:\b0\s+(?:files?|matches?|results?)\b|\bno\s+(?:files?|matches?|results?)\b|\"(?:count|total)\"\s*:\s*0)",
    re.IGNORECASE,
)
PATH_REWRITE = re.compile(
    r"(?:path[- ]rewrite|rewritten path|invalid path|cannot find the path|path does not exist|winerror\s*3)",
    re.IGNORECASE,
)
MAX_ROUTE_EVENT_SCAN = 2048
MAX_CLASSIFICATION_TEXT = 4096


class HookFailure(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-kind", choices=("codex", "claude", "grok"), default="codex")
    parser.add_argument(
        "--phase",
        choices=("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"),
        required=True,
    )
    parser.add_argument("--workspace-root", default="")
    parser.add_argument("--contract-path", default="")
    parser.add_argument("--input-json", default="")
    parser.add_argument("--discovery-runtime-path", default="")
    parser.add_argument("--registry-root", default="")
    parser.add_argument("--registry-path", default="GLOBAL_DISCOVERY_PROMOTIONS.json")
    parser.add_argument("--event-directory", default="aof-route-events")
    parser.add_argument("--python-path", default=sys.executable)
    parser.add_argument("--runtime-platform", default="auto")
    parser.add_argument("--governed-tool", action="append", default=[])
    return parser.parse_args()


def emit_block(host_kind: str, phase: str, reason: str) -> None:
    bounded = reason[:1800]
    if phase == "Stop" and host_kind == "codex":
        payload: dict[str, Any] = {"continue": False, "stopReason": bounded, "systemMessage": bounded}
    elif phase == "Stop":
        payload = {"decision": "block", "reason": bounded, "systemMessage": bounded}
    elif host_kind == "grok" and phase == "PreToolUse":
        payload: dict[str, Any] = {"decision": "deny", "reason": bounded}
    elif phase == "PreToolUse":
        payload: dict[str, Any] = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": bounded,
            }
        }
    elif host_kind == "claude" and phase == "PostToolUseFailure":
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUseFailure",
                "additionalContext": bounded,
            }
        }
    elif host_kind == "claude":
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "decision": "block",
                "reason": bounded,
            }
        }
    elif host_kind == "codex":
        payload = {"continue": False, "stopReason": bounded, "systemMessage": bounded}
    else:
        # Grok PostToolUse is observe-only. Keep the diagnostic visible to the
        # host without claiming that a completed side effect was rolled back.
        payload = {"systemMessage": bounded}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")))
    sys.stdout.flush()


def hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}


def run_process(command: list[str], cwd: Path | None = None, timeout: int = 12) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        **hidden_subprocess_kwargs(),
    )


def unique_text(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
    return result


def event_value(event: dict[str, Any], snake_name: str, camel_name: str) -> Any:
    """Read one canonical hook field across Codex/Claude and Grok payloads."""
    return event.get(snake_name) if snake_name in event else event.get(camel_name)


def apply_patch_paths(command: str) -> list[str]:
    candidates: list[str] = []
    for line in command.splitlines():
        match = PATCH_PATH.match(line) or PATCH_MOVE.match(line)
        if match:
            value = match.group(1).strip()
            if value and value not in candidates:
                candidates.append(value)
    return candidates


def normalize_slashes(value: str) -> str:
    return value.replace("\\", "/").rstrip("/")


def normalize_tool_name(value: str) -> str:
    normalized = value.strip().lower()
    return normalized if ROUTE_TOOL_NAME.fullmatch(normalized) else ""


def configured_governed_tools(values: Iterable[Any]) -> set[str]:
    governed: set[str] = set()
    for value in values:
        raw = str(value).strip()
        normalized = normalize_tool_name(raw)
        if not normalized or raw != normalized:
            raise HookFailure(f"Configured governed tool is not a stable lowercase identifier: {raw!r}")
        governed.add(normalized)
    return governed


def registry_governed_tools(args: argparse.Namespace) -> set[str]:
    registry_root = Path(args.registry_root).resolve(strict=False)
    registry = Path(args.registry_path)
    if not registry.is_absolute():
        registry = registry_root / registry
    registry = registry.resolve(strict=False)
    if not path_is_within(registry, registry_root):
        raise HookFailure(f"Discovery registry escapes its declared root: {registry}")
    if not registry.is_file():
        raise HookFailure(f"Discovery registry is missing: {registry}")
    try:
        payload = json.loads(registry.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise HookFailure(f"Discovery registry is invalid JSON: {registry}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or type(payload.get("schemaVersion")) is not int
        or payload["schemaVersion"] not in {1, 2}
    ):
        raise HookFailure("Discovery registry schemaVersion must be 1 or 2.")
    if payload["schemaVersion"] == 1:
        if "routePolicies" in payload:
            raise HookFailure("Discovery schemaVersion 1 registry cannot declare routePolicies.")
        return set()
    if "routePolicies" not in payload or not isinstance(payload["routePolicies"], list):
        raise HookFailure("Discovery schemaVersion 2 registry routePolicies must be an array.")
    declared: list[str] = []
    valid_statuses = {"candidate", "active", "blocked", "stale", "deprecated"}
    for policy in payload["routePolicies"]:
        if not isinstance(policy, dict):
            raise HookFailure("Discovery route policy must be an object.")
        if policy.get("status") not in valid_statuses:
            raise HookFailure("Discovery route-policy status is invalid.")
        consumers = policy.get("consumers")
        if (
            not isinstance(consumers, list)
            or not consumers
            or any(
                not isinstance(item, str) or not re.fullmatch(r"\*|[a-z][a-z0-9_-]*", item)
                for item in consumers
            )
            or len(consumers) != len(set(consumers))
        ):
            raise HookFailure("Discovery route-policy consumers must contain unique canonical identifiers or '*'.")
        match = policy.get("match")
        if not isinstance(match, dict):
            raise HookFailure("Discovery route-policy match must be an object.")
        tools = match.get("tools")
        if not isinstance(tools, list) or not tools:
            raise HookFailure("Discovery route-policy match.tools must be a non-empty array.")
        validated_tools = configured_governed_tools(tools)
        if len(validated_tools) != len(tools):
            raise HookFailure("Discovery route-policy match.tools must contain unique identifiers.")
        if policy["status"] != "active":
            continue
        if args.host_kind not in consumers and "*" not in consumers:
            continue
        declared.extend(validated_tools)
    return configured_governed_tools(declared)


def declared_governed_tools(args: argparse.Namespace) -> set[str]:
    if args.governed_tool:
        return configured_governed_tools(args.governed_tool)
    return registry_governed_tools(args)


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def repo_relative(value: str, root: Path) -> str | None:
    text = value.strip().strip('"')
    candidate = Path(text)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
        if not path_is_within(resolved, root):
            return None
        return resolved.relative_to(root.resolve(strict=False)).as_posix()
    normalized = normalize_slashes(text)
    if normalized.startswith("./"):
        normalized = normalized[2:]
    collapsed = os.path.normpath(normalized).replace("\\", "/")
    return collapsed


def looks_like_scope_path(candidate: str) -> bool:
    return bool(
        re.search(r"[\\/]", candidate)
        or re.match(r"^[A-Za-z0-9_*.-]+\.[A-Za-z0-9*]+$", candidate)
        or re.match(r"^\.[A-Za-z0-9_*.-]+$", candidate)
    )


def extract_scope(contract: Path, root: Path) -> tuple[list[str], list[str], int]:
    content = contract.read_text(encoding="utf-8-sig")
    bodies = SCOPE_SECTION.findall(content)
    if not bodies:
        raise HookFailure(f"Contract has no 'In Scope' section: {contract}")
    repo_entries: list[str] = []
    external_entries: list[str] = []
    for body in bodies:
        for raw in BACKTICK_SPAN.findall(body):
            candidate = raw.strip()
            if not candidate or re.search(r"\s", candidate):
                continue
            candidate = candidate.split("#", 1)[0].rstrip(":,;.")
            if not candidate or not looks_like_scope_path(candidate):
                continue
            relative = repo_relative(candidate, root)
            if relative is None:
                normalized = normalize_slashes(str(Path(candidate).resolve(strict=False)))
                if normalized not in external_entries:
                    external_entries.append(normalized)
            elif relative and relative not in repo_entries:
                repo_entries.append(relative)
    contract_relative = repo_relative(str(contract), root)
    if contract_relative and contract_relative not in repo_entries:
        repo_entries.append(contract_relative)
    repo_scope_count = sum(1 for entry in repo_entries if entry != contract_relative)
    if repo_scope_count == 0:
        raise HookFailure(
            "No machine-checkable In Scope paths found. Declare exact backtick-quoted files, directories, or globs."
        )
    return repo_entries, external_entries, repo_scope_count


def load_scope(contract: Path, root: Path) -> tuple[list[str], list[str], int]:
    """Prefer a fresh scope manifest over re-parsing the contract markdown.

    A manifest is fresh when <contract>.scope.json exists, parses, declares
    schema 1 and its source_sha256 matches the current contract bytes. Any
    other condition falls back to extract_scope, so installations without
    manifests keep today's behavior byte for byte.
    """
    manifest_path = contract.with_name(contract.name + ".scope.json")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            isinstance(payload, dict)
            and payload.get("schema") == 1
            and isinstance(payload.get("repo_scope"), list)
            and isinstance(payload.get("external_scope"), list)
            and isinstance(payload.get("repo_scope_count"), int)
        ):
            digest = hashlib.sha256(contract.read_bytes()).hexdigest()
            if payload.get("source_sha256") == digest:
                return (
                    [str(item) for item in payload["repo_scope"]],
                    [str(item) for item in payload["external_scope"]],
                    payload["repo_scope_count"],
                )
    except (OSError, ValueError):
        pass
    return extract_scope(contract, root)


def in_scope(candidate: str, entries: Iterable[str]) -> bool:
    value = normalize_slashes(candidate)
    if os.name == "nt":
        value = value.lower()
    for raw_entry in entries:
        entry = normalize_slashes(raw_entry)
        if os.name == "nt":
            entry = entry.lower()
        if "*" in entry and fnmatch.fnmatchcase(value, entry):
            return True
        if value == entry or value.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def validate_candidates(candidates: list[str], contract: Path, root: Path) -> None:
    repo_entries, external_entries, _ = load_scope(contract, root)
    violations: list[str] = []
    for raw in candidates:
        candidate = raw.strip().strip('"')
        relative = repo_relative(candidate, root)
        if relative is not None:
            if relative == ".." or relative.startswith("../") or not in_scope(relative, repo_entries):
                violations.append(relative)
            continue
        normalized = normalize_slashes(str(Path(candidate).resolve(strict=False)))
        if not in_scope(normalized, external_entries):
            violations.append(normalized)
    if violations:
        raise HookFailure(
            "Scope path admission failed; candidate path(s) outside declared scope: " + ", ".join(violations)
        )


def status_paths(root: Path) -> list[str]:
    result = run_process(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
    )
    if result.returncode != 0:
        raise HookFailure("git status failed: " + result.stdout.strip())
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        payload = line[3:]
        values = payload.split(" -> ", 1) if " -> " in payload else [payload]
        for value in values:
            path = value.strip().strip('"')
            if path and path not in paths:
                paths.append(path)
    return paths


def validate_diff(contract: Path, root: Path) -> None:
    repo_entries, _, repo_scope_count = load_scope(contract, root)
    changed = status_paths(root)
    violations = [path for path in changed if not in_scope(normalize_slashes(path), repo_entries)]
    if violations:
        raise HookFailure(
            f"Scope alignment failed: {len(violations)} of {len(changed)} changed file(s) are outside "
            f"the {repo_scope_count} declared scope path(s): {', '.join(violations)}"
        )


def record_ungoverned_route_allow(
    args: argparse.Namespace,
    event: dict[str, Any],
    raw_tool_name: str,
    normalized_tool_name: str,
) -> None:
    """Best-effort metadata-only evidence; route-event schemas remain isolated."""
    try:
        registry_root = Path(args.registry_root).resolve(strict=False)
        event_root = Path(args.event_directory)
        if not event_root.is_absolute():
            event_root = registry_root / event_root
        event_root = event_root.resolve(strict=False)
        if not path_is_within(event_root, registry_root):
            raise HookFailure(f"Route admission event directory escapes its declared root: {event_root}")
        admission_root = (event_root / "hook-admission").resolve(strict=False)
        if not path_is_within(admission_root, event_root):
            raise HookFailure(f"Route admission directory escapes its event root: {admission_root}")
        admission_root.mkdir(parents=True, exist_ok=True)
        event_id = uuid.uuid4().hex
        correlation = str(event_value(event, "tool_use_id", "toolUseId") or "")
        record = {
            "schemaVersion": 1,
            "eventKind": "aof-route-admission",
            "eventId": event_id,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "consumer": args.host_kind,
            "phase": "pre-tool",
            "tool": normalized_tool_name or "unrecognized",
            "rawToolNameHash": hashlib.sha256(raw_tool_name.encode("utf-8", errors="replace")).hexdigest(),
            "correlationHash": hashlib.sha256(correlation.encode("utf-8", errors="replace")).hexdigest(),
            "decision": "allow",
            "reasonCode": "tool-not-governed",
        }
        handle, temporary_name = tempfile.mkstemp(prefix=".admission-", suffix=".tmp", dir=admission_root)
        temporary = Path(temporary_name)
        destination = admission_root / f"{event_id}.json"
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(record, stream, separators=(",", ":"), sort_keys=True)
                stream.write("\n")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    except Exception as exc:
        sys.stderr.write(f"AOF route admission log unavailable: {type(exc).__name__}\n")
        sys.stderr.flush()


def resolve_route(args: argparse.Namespace, event: dict[str, Any], tool_name: str, tool_input: Any) -> dict[str, Any] | None:
    if not args.discovery_runtime_path and not args.registry_root:
        return None
    if not args.discovery_runtime_path or not args.registry_root:
        raise HookFailure("Discovery runtime and registry root must be supplied together.")
    runtime = Path(args.discovery_runtime_path).resolve(strict=False)
    registry_root = Path(args.registry_root).resolve(strict=False)
    registry = Path(args.registry_path)
    if not registry.is_absolute():
        registry = registry_root / registry
    if not runtime.is_file():
        raise HookFailure(f"Discovery runtime is missing: {runtime}")
    if not registry.is_file():
        raise HookFailure(f"Discovery registry is missing: {registry}")
    event_root = Path(args.event_directory)
    if not event_root.is_absolute():
        event_root = registry_root / event_root
    event_root = event_root.resolve(strict=False)
    if not path_is_within(event_root, registry_root):
        raise HookFailure(f"Discovery event directory escapes its declared registry root: {event_root}")
    event_root.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".hook-args-", suffix=".json", dir=event_root)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(tool_input or {}, separators=(",", ":")), encoding="utf-8")
        correlation = str(event_value(event, "tool_use_id", "toolUseId") or uuid.uuid4().hex)
        command = [
            args.python_path,
            str(runtime),
            "resolve-route",
            "--workspace-root",
            str(registry_root),
            "--registry",
            args.registry_path,
            "--registry-scope",
            "global",
            "--consumer",
            args.host_kind,
            "--runtime-platform",
            args.runtime_platform,
            "--tool",
            tool_name,
            "--tool-args-file",
            str(temporary),
            "--correlation-id",
            correlation,
            "--event-directory",
            args.event_directory,
        ]
        result = run_process(command, cwd=registry_root)
        if result.returncode != 0:
            raise HookFailure(f"route resolver exited {result.returncode}: {result.stdout.strip()}")
        return json.loads(result.stdout)
    finally:
        temporary.unlink(missing_ok=True)


def route_event_root(args: argparse.Namespace) -> tuple[Path, Path]:
    registry_root = Path(args.registry_root).resolve(strict=False)
    event_root = Path(args.event_directory)
    if not event_root.is_absolute():
        event_root = registry_root / event_root
    event_root = event_root.resolve(strict=False)
    if not path_is_within(event_root, registry_root):
        raise HookFailure(f"Discovery event directory escapes its declared registry root: {event_root}")
    return registry_root, event_root


def load_discovery_runtime(args: argparse.Namespace):
    runtime = Path(args.discovery_runtime_path).resolve(strict=True)
    module_name = "aof_discovery_runtime_" + hashlib.sha256(
        str(runtime).encode("utf-8")
    ).hexdigest()[:16]
    specification = importlib.util.spec_from_file_location(module_name, runtime)
    if specification is None or specification.loader is None:
        raise HookFailure("Discovery route runtime could not be loaded for event validation.")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except Exception as exc:
        raise HookFailure("Discovery route runtime failed to load for event validation.") from exc
    return module


def load_route_event_validator(args: argparse.Namespace):
    module = load_discovery_runtime(args)
    validator = getattr(module, "validate_route_event", None)
    if not callable(validator):
        raise HookFailure("Discovery route runtime does not expose validate_route_event.")
    return validator


def discovery_run_id(event: dict[str, Any]) -> str:
    for snake, camel in (
        ("run_id", "runId"),
        ("session_id", "sessionId"),
        ("conversation_id", "conversationId"),
        ("thread_id", "threadId"),
        ("task_id", "taskId"),
        # Hermes route events carry no run/session/thread/task id: their only
        # turn identity is correlationId ("hermes:<sha>"). Without this entry
        # every turn-scoped guard silently degrades to a no-op on that host.
        ("correlation_id", "correlationId"),
    ):
        value = event_value(event, snake, camel)
        if isinstance(value, str) and RUN_ID.fullmatch(value):
            return value
    return ""


def configured_positive_int(name: str, default: int) -> int:
    value = os.environ.get(name, str(default)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise HookFailure(f"{name} must be a positive integer") from exc
    if not 1 <= parsed <= 100_000:
        raise HookFailure(f"{name} must be between 1 and 100000")
    return parsed


def explicit_count(event: dict[str, Any], snake_name: str, camel_name: str) -> int:
    value = event_value(event, snake_name, camel_name)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, value)
    return 0


def record_discovery_activity(
    args: argparse.Namespace,
    event: dict[str, Any],
    tool_name: str,
    normalized_tool: str,
) -> None:
    run_id = discovery_run_id(event)
    if not run_id or not normalized_tool:
        return
    registry_root, _ = route_event_root(args)
    runtime = load_discovery_runtime(args)
    runtime.record_discovery_run_activity(
        Path(args.event_directory),
        registry_root,
        run_id=run_id,
        consumer=args.host_kind,
        tool=normalized_tool,
        tool_call_id=str(event_value(event, "tool_use_id", "toolUseId") or ""),
        mutation=bool(MUTATION_TOOL.search(tool_name)),
        delivery_created=event_value(event, "delivery_created", "deliveryCreated") is True,
    )


def breaker_runtime(args: argparse.Namespace) -> Any:
    """Load the shared runtime that owns the breaker primitives.

    The primitives are host-agnostic and live in ``discovery_promotions.py`` so
    no adapter or hook carries a private copy: a per-host copy is exactly how
    the first breaker reached Codex and Claude while leaving Hermes uncovered.
    """
    if not args.discovery_runtime_path:
        return None
    path = Path(args.discovery_runtime_path).resolve(strict=False)
    if not path.is_file():
        return None
    module_name = f"aof_discovery_runtime_{hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16]}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: a dataclass defined in the loaded module
    # resolves its annotations through sys.modules[cls.__module__], which is
    # None for an unregistered dynamic module. Mirrors the Hermes plugin loader.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def breaker_event_directory(args: argparse.Namespace) -> Path | None:
    if not args.registry_root or not args.event_directory:
        return None
    directory = Path(args.event_directory)
    if not directory.is_absolute():
        directory = Path(args.registry_root) / directory
    return directory


def enforce_discovery_pass(args: argparse.Namespace, event: dict[str, Any]) -> None:
    mode = os.environ.get("AOF_DISCOVERY_ENFORCEMENT_MODE", "warn").strip().lower()
    if mode not in {"warn", "block"}:
        raise HookFailure("AOF_DISCOVERY_ENFORCEMENT_MODE must be warn or block")
    threshold = configured_positive_int(
        "AOF_DISCOVERY_MIN_TOOL_CALLS",
        10,
    )
    run_id = discovery_run_id(event)
    if not run_id:
        raw = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        run_id = f"unidentified-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"
    registry_root, _ = route_event_root(args)
    runtime = load_discovery_runtime(args)
    activity = runtime.discovery_run_activity_summary(
        Path(args.event_directory),
        registry_root,
        run_id=run_id,
        consumer=args.host_kind,
    )
    metrics = {
        "toolCallCount": max(
            int(activity["toolCallCount"]),
            explicit_count(event, "tool_call_count", "toolCallCount"),
        ),
        "filesTouched": max(
            int(activity["filesTouched"]),
            explicit_count(event, "files_touched", "filesTouched"),
        ),
        "deliveryCreated": bool(activity["deliveryCreated"])
        or event_value(event, "delivery_created", "deliveryCreated") is True,
    }
    has_explicit_classification = any(
        name in event
        for name in (
            "tool_call_count", "toolCallCount", "files_touched", "filesTouched",
            "delivery_created", "deliveryCreated",
        )
    )
    assume_nontrivial = os.environ.get("AOF_DISCOVERY_ASSUME_NONTRIVIAL", "1").strip().lower() not in {
        "0", "false", "no",
    }
    nontrivial = (
        metrics["toolCallCount"] >= threshold
        or metrics["filesTouched"] > 0
        or metrics["deliveryCreated"]
        or (assume_nontrivial and not has_explicit_classification and metrics["toolCallCount"] == 0)
    )
    if not nontrivial:
        return
    pass_event = runtime.find_discovery_pass_event(
        Path(args.event_directory),
        registry_root,
        run_id=run_id,
        consumer=args.host_kind,
    )
    if pass_event is not None and pass_event.get("nonTrivial") is True:
        return
    runtime.record_discovery_pass_violation(
        Path(args.event_directory),
        registry_root,
        run_id=run_id,
        consumer=args.host_kind,
        mode=mode,
        metrics=metrics,
    )
    reason = f"AOF Discovery Pass missing for non-trivial run '{run_id}'; closeout is non-compliant."
    if mode == "block":
        emit_block(args.host_kind, "Stop", reason)
    else:
        sys.stderr.write(reason + "\n")
        sys.stderr.flush()


def find_pending_route_decision(
    args: argparse.Namespace,
    *,
    correlation_id: str,
    tool_name: str,
    argument_hash: str,
) -> Path | None:
    """Find one unmatched decision for this host call without persisting raw payloads."""
    if not correlation_id:
        return None
    expected_platform = str(args.runtime_platform or "auto").casefold()
    if expected_platform == "auto":
        expected_platform = (
            "windows" if sys.platform.startswith("win")
            else "linux" if sys.platform.startswith("linux")
            else "darwin" if sys.platform == "darwin"
            else expected_platform
        )
    _, event_root = route_event_root(args)
    if not event_root.is_dir():
        return None
    validate_event = load_route_event_validator(args)
    paths = sorted(
        event_root.glob("*.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )[:MAX_ROUTE_EVENT_SCAN]
    completed: set[str] = set()
    decisions: list[Path] = []
    for path in paths:
        try:
            if path.stat().st_size > 65_536:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        try:
            validate_event(payload)
        except Exception:
            continue
        if payload.get("eventKind") == "discovery-route-outcome":
            decision_id = payload.get("decisionEventId")
            if isinstance(decision_id, str):
                completed.add(decision_id)
            continue
        if (
            payload.get("eventKind") == "discovery-route-decision"
            and payload.get("consumer") == args.host_kind
            and payload.get("platform") == expected_platform
            and payload.get("correlationId") == correlation_id
            and payload.get("tool") == tool_name
            and payload.get("argumentHash") == argument_hash
            and isinstance(payload.get("eventId"), str)
        ):
            decisions.append(path)
    for path in decisions:
        try:
            event_id = json.loads(path.read_text(encoding="utf-8"))["eventId"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
            continue
        if event_id not in completed:
            return path
    return None


def classify_tool_outcome(
    event: dict[str, Any],
    tool_name: str,
    tool_input: dict[str, Any],
) -> tuple[str, str, str, int]:
    response = event_value(event, "tool_response", "toolResponse")
    if response is None:
        response = event_value(event, "tool_result", "toolResult")
    status = str(event.get("status") or "").strip().lower()
    success: bool | None = None
    error_parts: list[str] = []
    if isinstance(response, dict):
        if isinstance(response.get("success"), bool):
            success = response["success"]
        response_status = response.get("status")
        if not status and isinstance(response_status, str):
            status = response_status.strip().lower()
        for key in ("error", "error_type", "errorType", "error_message", "errorMessage"):
            value = response.get(key)
            if value:
                error_parts.append(str(value))
    for key in ("error", "error_type", "errorType", "error_message", "errorMessage"):
        value = event.get(key)
        if value:
            error_parts.append(str(value))
    try:
        rendered = json.dumps(response, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = str(response or "")
    diagnostic = " ".join([*error_parts, rendered])[:MAX_CLASSIFICATION_TEXT]
    duration_value = event_value(event, "duration_ms", "durationMs")
    try:
        duration_ms = max(0, min(int(duration_value or 0), 86_400_000))
    except (TypeError, ValueError):
        duration_ms = 0
    interrupted = event_value(event, "is_interrupt", "isInterrupt") is True
    if interrupted or status in {"cancelled", "canceled"}:
        return "cancelled", "not-run", "", duration_ms
    failed = success is False or bool(error_parts) or status in {
        "error", "failed", "failure", "tool_error", "tool-error",
    }
    if failed:
        failure_class = "path-rewrite" if PATH_REWRITE.search(diagnostic) else "tool-error"
        return "failed", "failed", failure_class, duration_ms
    if tool_name == "search_files" and ZERO_RESULT.search(diagnostic):
        for key in ("path", "root", "directory", "file_path"):
            candidate = tool_input.get(key)
            if not isinstance(candidate, str) or not candidate:
                continue
            try:
                path = Path(candidate)
                if path.is_file():
                    return "failed", "failed", "contradictory-zero-result", duration_ms
                if path.is_dir():
                    pattern = tool_input.get("pattern") or tool_input.get("query") or tool_input.get("name")
                    if (
                        isinstance(pattern, str)
                        and pattern
                        and not any(char in pattern for char in "[](){}*+?|")
                        and (path / pattern).exists()
                    ):
                        return "failed", "failed", "contradictory-zero-result", duration_ms
            except OSError:
                continue
    return "succeeded", "passed", "", duration_ms


def record_route_outcome_event(
    args: argparse.Namespace,
    decision_event: Path,
    *,
    outcome: str,
    validation: str,
    failure_class: str = "",
    duration_ms: int = 0,
) -> None:
    runtime = Path(args.discovery_runtime_path).resolve(strict=False)
    registry_root, _ = route_event_root(args)
    command = [
        args.python_path,
        str(runtime),
        "record-route-outcome",
        "--workspace-root",
        str(registry_root),
        "--decision-event",
        str(decision_event),
        "--outcome",
        outcome,
        "--validation",
        validation,
        "--duration-ms",
        str(duration_ms),
        "--registry",
        args.registry_path,
    ]
    if failure_class:
        command.extend(("--failure-class", failure_class))
    result = run_process(command, cwd=registry_root)
    if result.returncode != 0:
        raise HookFailure(f"route outcome recorder exited {result.returncode}: {result.stdout.strip()}")


def validate_route_response(route: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(route, dict):
        raise HookFailure("route resolver response must be a JSON object")
    status = route.get("status")
    allows_execution = route.get("allowsExecution")
    reason_code = route.get("reasonCode")
    if status not in {"matched", "no-match", "ambiguous"}:
        raise HookFailure(f"route resolver returned unsupported status: {status!r}")
    if not isinstance(allows_execution, bool):
        raise HookFailure("route resolver response must declare boolean allowsExecution")
    if not isinstance(reason_code, str) or not reason_code.strip():
        raise HookFailure("route resolver response must declare a non-empty reasonCode")
    if status == "no-match":
        if not allows_execution:
            raise HookFailure("route resolver returned an inconsistent no-match denial")
        return route, True
    if status == "matched":
        if not isinstance(route.get("policyId"), str) or not route["policyId"].strip():
            raise HookFailure("matched route resolver response must declare a non-empty policyId")
        return route, allows_execution
    if allows_execution:
        raise HookFailure("route resolver returned an inconsistent ambiguous allow")
    return route, False


def main() -> int:
    args = parse_args()
    raw_input = args.input_json or sys.stdin.read()
    try:
        if not raw_input.strip():
            raise ValueError("hook input is empty")
        event = json.loads(raw_input)
        if not isinstance(event, dict):
            raise ValueError("hook input must be a JSON object")
    except Exception as exc:
        emit_block(args.host_kind, args.phase, f"AOF scope enforcement rejected malformed hook input: {exc}")
        return 0

    root_candidate = args.workspace_root or str(
        event_value(event, "workspace_root", "workspaceRoot") or event.get("cwd") or Path.cwd()
    )
    try:
        root = Path(root_candidate).resolve(strict=False)
    except Exception:
        emit_block(args.host_kind, args.phase, f"AOF scope enforcement could not resolve workspace root '{root_candidate}'.")
        return 0
    contract = Path(args.contract_path) if args.contract_path else root / "docs" / "EXECUTION_CONTRACT.md"
    if not contract.is_absolute():
        contract = root / contract
    contract = contract.resolve(strict=False)
    tool_name = str(event_value(event, "tool_name", "toolName") or "")
    normalized_tool = normalize_tool_name(tool_name)
    tool_input = event_value(event, "tool_input", "toolInput")
    if not isinstance(tool_input, dict):
        tool_input = {}

    if args.phase == "Stop":
        try:
            if not args.discovery_runtime_path or not args.registry_root:
                raise HookFailure("Discovery Pass enforcement is missing its runtime or registry authority")
            enforce_discovery_pass(args, event)
        except Exception as exc:
            mode = os.environ.get("AOF_DISCOVERY_ENFORCEMENT_MODE", "warn").strip().lower()
            reason = f"AOF Discovery Pass enforcement unavailable: {exc}"
            if mode == "block":
                emit_block(args.host_kind, "Stop", reason)
            else:
                sys.stderr.write(reason[:1800] + "\n")
                sys.stderr.flush()
        return 0

    if args.phase == "PreToolUse":
        route_configured = bool(args.discovery_runtime_path or args.registry_root)
        if route_configured:
            try:
                record_discovery_activity(args, event, tool_name, normalized_tool)
            except Exception as exc:
                sys.stderr.write(f"AOF Discovery Pass activity unavailable: {type(exc).__name__}\n")
                sys.stderr.flush()
            try:
                governed_tools = declared_governed_tools(args)
            except Exception as exc:
                emit_block(args.host_kind, args.phase, f"AOF route-policy taxonomy is invalid: {exc}")
                return 0
            if normalized_tool not in governed_tools:
                record_ungoverned_route_allow(args, event, tool_name, normalized_tool)
            else:
                # Circuit breaker: refuse an already-exhausted signature before
                # the runtime is consulted again. Fail-open on any internal
                # error so an unrelated breaker fault never blocks real work.
                breaker = None
                breaker_dir: Path | None = None
                run_id = ""
                signature = ""
                try:
                    run_id = discovery_run_id(event)
                    breaker_dir = breaker_event_directory(args)
                    breaker = breaker_runtime(args) if breaker_dir and run_id else None
                    prior = None
                    if breaker is not None:
                        signature = breaker.breaker_signature(normalized_tool, tool_input)
                        prior = breaker.breaker_exhausted_entry(
                            breaker_dir, run_id=run_id, signature=signature
                        )
                    if isinstance(prior, dict) and prior.get("exhausted") is True:
                        reason = (
                            f"AOF route policy '{prior.get('policyId')}' already denied this exact "
                            f"'{tool_name}' call {prior.get('attempts')} time(s) this turn "
                            f"(declared maxAttemptsPerTurn={prior.get('cap')}). "
                            "Change the arguments or take an alternative; the identical call will not be retried."
                        )
                        if prior.get("alternatives"):
                            reason += f" Alternatives: {prior.get('alternatives')}"
                        try:
                            breaker.emit_breaker_trip_event(
                                breaker_dir,
                                consumer=args.host_kind,
                                run_id=run_id,
                                signature=signature,
                                tool_name=tool_name,
                                policy_id=prior.get("policyId"),
                                cap=int(prior.get("cap") or 0),
                                observed=int(prior.get("attempts") or 0) + 1,
                            )
                        except Exception as exc:
                            sys.stderr.write(f"AOF breaker trip not recorded: {type(exc).__name__}\n")
                            sys.stderr.flush()
                        emit_block(args.host_kind, args.phase, reason)
                        return 0
                except Exception as exc:
                    sys.stderr.write(f"AOF route breaker unavailable: {type(exc).__name__}\n")
                    sys.stderr.flush()
                    breaker = None

                try:
                    route, allows_execution = validate_route_response(
                        resolve_route(args, event, normalized_tool, tool_input)
                    )
                    if not allows_execution:
                        alternatives = " | ".join(
                            str(item.get("description"))
                            for item in route.get("alternatives", [])
                            if isinstance(item, dict) and item.get("description")
                        )
                        reason = (
                            f"AOF route policy '{route.get('policyId') or route.get('policyIds')}' blocked '{tool_name}' before dispatch: "
                            f"{route.get('reasonCode')}."
                        )
                        if alternatives:
                            reason += f" Alternatives: {alternatives}"
                        # Count this denial and arm the breaker once the
                        # policy-declared budget for the turn is spent.
                        if breaker is not None and breaker_dir is not None and signature:
                            try:
                                cap = breaker.route_retry_cap(route)
                                if cap is not None:
                                    attempts = breaker.breaker_note_denial(
                                        breaker_dir,
                                        run_id=run_id,
                                        signature=signature,
                                        cap=cap,
                                        policy_id=route.get("policyId") or route.get("policyIds"),
                                        alternatives=alternatives,
                                    )
                                    if attempts >= cap:
                                        reason += (
                                            f" Retry budget for this turn is spent ({attempts}/{cap}); "
                                            "the identical call will be refused from here."
                                        )
                            except Exception as exc:
                                sys.stderr.write(f"AOF route breaker not recorded: {type(exc).__name__}\n")
                                sys.stderr.flush()
                        decision_path = route.get("eventPath")
                        if isinstance(decision_path, str) and decision_path:
                            try:
                                record_route_outcome_event(
                                    args,
                                    Path(decision_path),
                                    outcome="blocked",
                                    validation="not-run",
                                )
                            except Exception as exc:
                                sys.stderr.write(f"AOF route outcome unavailable: {type(exc).__name__}\n")
                                sys.stderr.flush()
                        emit_block(args.host_kind, args.phase, reason)
                        return 0
                except Exception as exc:
                    emit_block(args.host_kind, args.phase, f"AOF route-policy preflight failed closed: {exc}")
                    return 0

        if not contract.is_file() or not MUTATION_TOOL.search(tool_name):
            return 0
        try:
            contract_text = contract.read_text(encoding="utf-8-sig")
            if re.search(r"(?mi)^- Mode:\s*REVIEW_ONLY\s*$", contract_text):
                raise HookFailure("mutating tool denied because the active contract mode is REVIEW_ONLY")
            values = [
                tool_input.get(name)
                for name in (
                    "file_path",
                    "path",
                    "notebook_path",
                    "destination",
                    "destination_path",
                    "target_path",
                    "new_path",
                )
            ]
            candidates = unique_text(values)
            if tool_name.lower() == "apply_patch":
                candidates = unique_text([candidates, apply_patch_paths(str(tool_input.get("command") or ""))])
            if not candidates:
                raise HookFailure(
                    f"mutating tool '{tool_name}' denied because no candidate file path could be extracted"
                )
            validate_candidates(candidates, contract, root)
        except Exception as exc:
            emit_block(args.host_kind, args.phase, f"AOF pre-tool path admission failed. {exc}")
        return 0

    route_configured = bool(args.discovery_runtime_path or args.registry_root)
    if route_configured:
        try:
            governed_tools = declared_governed_tools(args)
            correlation_id = str(event_value(event, "tool_use_id", "toolUseId") or "")
            if normalized_tool in governed_tools and correlation_id:
                canonical_arguments = json.dumps(
                    tool_input,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                argument_hash = hashlib.sha256(canonical_arguments.encode("utf-8")).hexdigest()
                decision_event = find_pending_route_decision(
                    args,
                    correlation_id=correlation_id,
                    tool_name=normalized_tool,
                    argument_hash=argument_hash,
                )
                if decision_event is not None:
                    outcome, validation, failure_class, duration_ms = classify_tool_outcome(
                        event,
                        normalized_tool,
                        tool_input,
                    )
                    record_route_outcome_event(
                        args,
                        decision_event,
                        outcome=outcome,
                        validation=validation,
                        failure_class=failure_class,
                        duration_ms=duration_ms,
                    )
        except Exception as exc:
            sys.stderr.write(f"AOF route outcome unavailable: {type(exc).__name__}\n")
            sys.stderr.flush()

    if not contract.is_file():
        return 0
    try:
        validate_diff(contract, root)
    except Exception as exc:
        emit_block(args.host_kind, args.phase, f"AOF post-tool diff validation failed. {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
