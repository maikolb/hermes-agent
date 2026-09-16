"""Runtime identity stamped on every task run (Entrega 1B).

Answers one question for any run, after the fact: **which code and which
configuration executed it**. The baseline survey of 2026-09-15 established
why this cannot be answered today:

  * The installed release directory is not a git checkout, so there is no
    ``HEAD`` to read at the path that actually runs.
  * ``hermes_cli.__version__`` is ``0.20.5`` in all three releases that are
    in service simultaneously, so the package version distinguishes nothing.
  * ``/usr/local/bin/hermes`` is a shell script that routes to a different
    release per profile, so the unit file does not name the code either.
  * Files get copied into a release *while it is serving*: ``nfos_delivery.py``
    was rewritten 51s before a gateway restart, so even the release directory
    name (which records its install date) does not describe its contents.

The only identity that holds under all four is a **digest of the bytes that
are on disk**, taken at claim time. Hashing the package costs ~0.02s measured
on this host, against a 60s dispatch interval.

Deliberately **not cached per process**: the failure mode above is precisely
files changing underneath a live process. A per-process cache would hide the
one event this module exists to expose. Two runs of the same process reporting
different code digests *is* the signal.

Nothing here may raise. A failure to identify the runtime must never prevent a
task from being claimed, so every entry point degrades to a partial record.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

METADATA_KEY = "runtime_identity"
MANIFEST_DIRNAME = "runtime-manifests"

# Extensions that change behaviour. Bytecode and caches are excluded: they are
# derived, and including them makes the digest unstable for no added meaning.
_CODE_SUFFIXES = (".py",)
_SKIP_DIRS = frozenset({"__pycache__", ".git", "node_modules", ".pytest_cache", ".mypy_cache"})


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_digest(root: Path) -> tuple[str, list[dict[str, Any]]]:
    """Digest of ``root``'s source tree, plus the per-file manifest.

    The aggregate hashes the sorted ``relpath sha256`` lines rather than the
    concatenated bytes, so a file being renamed changes the digest even when
    the content set is identical.
    """
    entries: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in sorted(filenames):
            if not name.endswith(_CODE_SUFFIXES):
                continue
            full = Path(dirpath) / name
            try:
                entries.append({
                    "path": str(full.relative_to(root)).replace(os.sep, "/"),
                    "sha256": _sha256_file(full),
                    "bytes": full.stat().st_size,
                })
            except OSError:
                continue
    entries.sort(key=lambda e: e["path"])
    agg = hashlib.sha256()
    for e in entries:
        agg.update(("%s %s\n" % (e["path"], e["sha256"])).encode("utf-8"))
    return agg.hexdigest(), entries


def _manifest_dir() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home
        target = Path(get_hermes_home()) / MANIFEST_DIRNAME
        target.mkdir(parents=True, exist_ok=True)
        return target
    except Exception:
        return None


def _persist_manifest(kind: str, digest: str, payload: Mapping[str, Any]) -> Optional[str]:
    """Write ``payload`` once per digest. Returns the filename, or None.

    Content-addressed, so a digest already on disk is never rewritten: the
    manifest for a given digest is immutable by construction.
    """
    directory = _manifest_dir()
    if directory is None:
        return None
    name = "%s-%s.json" % (kind, digest[:16])
    final = directory / name
    try:
        if final.exists():
            return name
        tmp = final.with_name(final.name + ".tmp-%d" % os.getpid())
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(final)
        return name
    except OSError:
        return None


def code_identity(root: Optional[Path] = None) -> dict[str, Any]:
    """Identify the code actually loaded into this interpreter.

    ``root`` defaults to the directory of the imported ``hermes_cli`` package,
    not to a configured path: what matters is the tree Python resolved, which
    may differ from any path recorded in a unit file or a wrapper script.
    """
    out: dict[str, Any] = {}
    try:
        if root is None:
            import hermes_cli
            root = Path(hermes_cli.__file__).resolve().parent
        root = Path(root)
        digest, entries = _tree_digest(root)
        out["root"] = str(root)
        out["digest"] = "sha256:" + digest
        out["files"] = len(entries)
        try:
            import hermes_cli as _hc
            out["package_version"] = getattr(_hc, "__version__", None)
        except Exception:
            pass
        # A checkout still carries a revision; an installed release does not.
        # Recorded when present, never relied upon.
        try:
            from hermes_cli.main import _read_git_revision_fingerprint
            rev = _read_git_revision_fingerprint(root.parent)
            if rev:
                out["git"] = rev
        except Exception:
            pass
        out["manifest"] = _persist_manifest("code", digest, {
            "root": str(root),
            "digest": "sha256:" + digest,
            "files": entries,
        })
    except Exception as exc:  # never block a claim
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


SECRET_PLACEHOLDER = "<secret>"

# Fallback if the canonical set ever moves; the real one is imported below.
_FALLBACK_SECRET_KEYS = frozenset({
    "api_key", "apikey", "key", "token", "access_token", "refresh_token",
    "id_token", "secret", "client_secret", "password", "passwd", "auth",
    "authorization", "private_key", "bearer", "jwt",
})


def _secret_key_set() -> frozenset[str]:
    try:
        from hermes_cli.config import _SECRET_CONFIG_KEYS
        return _SECRET_CONFIG_KEYS
    except Exception:
        return _FALLBACK_SECRET_KEYS


def _strip_secrets(value: Any, secret_keys: frozenset[str], _depth: int = 0) -> Any:
    """Replace every credential-shaped value with a **fixed** placeholder.

    Deliberately not ``redact_config_value``: that helper masks for *display*
    and preserves the leading and trailing characters, so the masked form of
    two different credentials differs. Hashing that would move the config
    digest on every credential rotation, and two otherwise identical runs
    would look like they ran under different configurations — noise in exactly
    the comparison this stamp exists to support. A fixed placeholder makes the
    digest describe the configuration and nothing else, and leaks strictly
    less than a partial mask.
    """
    if _depth > 20:
        return value
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in secret_keys and isinstance(v, str) and v:
                out[k] = SECRET_PLACEHOLDER
            else:
                out[k] = _strip_secrets(v, secret_keys, _depth + 1)
        return out
    if isinstance(value, list):
        return [_strip_secrets(v, secret_keys, _depth + 1) for v in value]
    return value


def config_identity() -> dict[str, Any]:
    """Identify the effective configuration, after profile and global merge.

    Secrets are stripped **before** hashing, so the digest is stable across a
    credential rotation and no secret can be inferred from it. The manifest
    written to disk is the same stripped tree that was hashed, so a digest
    always has a retrievable, faithful manifest.
    """
    out: dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config_readonly, get_config_path
        effective = _strip_secrets(load_config_readonly(), _secret_key_set())
        canonical = json.dumps(effective, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        out["digest"] = "sha256:" + digest
        try:
            out["path"] = str(get_config_path())
        except Exception:
            pass
        out["manifest"] = _persist_manifest("config", digest, effective)
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def _process_started_at() -> Optional[int]:
    try:
        return int(Path("/proc/self").stat().st_ctime)
    except Exception:
        return None


def _systemd_unit() -> Optional[str]:
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if ".service" in line:
                return line.rsplit("/", 1)[-1].strip() or None
    except Exception:
        pass
    return None


def process_identity() -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        out["pid"] = os.getpid()
        out["ppid"] = os.getppid()
        out["executable"] = sys.executable
        out["host"] = socket.gethostname()
        started = _process_started_at()
        if started:
            out["started_at"] = started
        unit = _systemd_unit()
        if unit:
            out["unit"] = unit
        profile = os.environ.get("HERMES_PROFILE") or os.environ.get("HERMES_HOME")
        if profile:
            out["hermes_home"] = profile
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def runtime_identity(code_root: Optional[Path] = None) -> dict[str, Any]:
    """The compact record stored on the run row.

    Only digests and pointers live in the database; the full file lists and
    the redacted configuration tree live in the manifest directory, addressed
    by those digests.
    """
    return {
        "code": code_identity(code_root),
        "config": config_identity(),
        "process": process_identity(),
        "stamped_at": int(time.time()),
    }


def stamp_metadata(metadata: Optional[Mapping[str, Any]] = None,
                   *, code_root: Optional[Path] = None) -> dict[str, Any]:
    """Return ``metadata`` with the runtime identity added.

    Never overwrites an identity already present: the first stamp is the one
    that describes the code that started the run, and a later writer on the
    same row must not relabel it.
    """
    try:
        base: dict[str, Any] = dict(metadata or {})
    except Exception:
        base = {}
    try:
        if METADATA_KEY not in base:
            base[METADATA_KEY] = runtime_identity(code_root)
    except Exception:
        pass
    return base


def merge_run_metadata(conn, run_id: int,
                       metadata: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge ``metadata`` onto a run's existing metadata, preserving identity.

    Callers that replace the whole ``metadata`` column would otherwise erase
    the identity written at claim time, leaving a completed run that cannot
    be attributed to any code or configuration.
    """
    prior: dict[str, Any] = {}
    try:
        row = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        if row and row["metadata"]:
            loaded = json.loads(row["metadata"])
            if isinstance(loaded, dict):
                prior = loaded
    except Exception:
        prior = {}
    merged = dict(prior)
    try:
        merged.update(metadata or {})
    except Exception:
        pass
    if METADATA_KEY in prior:
        merged[METADATA_KEY] = prior[METADATA_KEY]
    elif METADATA_KEY not in merged:
        merged = stamp_metadata(merged)
    return merged
