"""Emit a structured scope manifest next to an execution contract.

The manifest freezes the machine-checkable scope extracted from the
markdown contract at validation time, keyed by the contract's sha256.
The AOF scope hook prefers a fresh manifest over re-parsing markdown,
which removes the parser-fragility class of failures (heading case,
backtick formats) from the enforcement path. Single source of truth:
this module imports the extraction from the hook itself.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aof_scope_hook import extract_scope  # noqa: E402

MANIFEST_SCHEMA = 1


def contract_sha256(contract: Path) -> str:
    return hashlib.sha256(contract.read_bytes()).hexdigest()


def build_manifest(contract: Path, workspace_root: Path) -> dict:
    repo_entries, external_entries, repo_scope_count = extract_scope(contract, workspace_root)
    return {
        "schema": MANIFEST_SCHEMA,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_sha256": contract_sha256(contract),
        "workspace_root": str(workspace_root.resolve(strict=False)),
        "repo_scope": sorted(repo_entries),
        "external_scope": sorted(external_entries),
        "repo_scope_count": repo_scope_count,
    }


def manifest_path_for(contract: Path) -> Path:
    return contract.with_name(contract.name + ".scope.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="emit_scope_manifest")
    parser.add_argument("--contract", required=True)
    parser.add_argument("--workspace-root", required=True)
    args = parser.parse_args(argv)
    contract = Path(args.contract).resolve(strict=False)
    root = Path(args.workspace_root).resolve(strict=False)
    manifest = build_manifest(contract, root)
    target = manifest_path_for(contract)
    target.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"scope manifest written: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
