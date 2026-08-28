from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.request
from pathlib import Path

from .core import Factory, FactoryConfig, FactoryError, StateStore, read_secret, stable_request_id


DEFAULT_CONFIG = Path("/etc/workflow-factory/config.json")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="workflow-factory")
    result.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = result.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--profile", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--description", default="Projeto criado automaticamente pelo Hermes.")
    create.add_argument("--request-id")
    create.add_argument("--source", type=Path)
    status = commands.add_parser("status")
    status.add_argument("--request-id", required=True)
    commands.add_parser("doctor")
    return result


def doctor(config: FactoryConfig) -> dict[str, object]:
    checks: dict[str, object] = {
        "profiles": config.profiles_path.is_file(),
        "github_cli": shutil.which(config.github_cli) is not None,
        "github_token": False,
        "dokploy_token": False,
        "registry_token": False,
        "dokploy_api": False,
        "domain_base": config.domain_base,
    }
    for key, path, label in (
        ("github_token", config.github_token_file, "GitHub"),
        ("dokploy_token", config.dokploy_api_key_file, "Dokploy"),
        ("registry_token", config.registry_token_file, "GHCR"),
    ):
        try:
            read_secret(path, label)
            checks[key] = True
        except FactoryError:
            pass
    try:
        request = urllib.request.Request(
            f"{config.dokploy_base_url}/project.all",
            headers={"x-api-key": read_secret(config.dokploy_api_key_file, "Dokploy")},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            checks["dokploy_api"] = 200 <= response.status < 300
    except Exception:
        pass
    checks["ok"] = all(value is True for key, value in checks.items() if key != "domain_base")
    return checks


def main() -> int:
    args = parser().parse_args()
    try:
        config = FactoryConfig.load(args.config)
        if args.command == "create":
            source_identity = str(args.source.resolve()) if args.source else None
            request_id = args.request_id or stable_request_id(args.profile, args.name, args.description, source_identity)
            result = Factory(config).create(args.profile, args.name, args.description, request_id, args.source)
        elif args.command == "status":
            result = StateStore(config.state_db).get(args.request_id)
        else:
            result = doctor(config)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("ok", True) else 1
    except FactoryError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
