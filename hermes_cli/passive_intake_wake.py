"""One-shot adapter from durable passive intake raw to Hermes cron execution."""

from __future__ import annotations

import argparse
import re


PROFILE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,127}$")


def _bounded(value: str, pattern: re.Pattern[str], label: str) -> str:
    candidate = str(value or "").strip()
    if not pattern.fullmatch(candidate):
        raise argparse.ArgumentTypeError(f"invalid {label}")
    return candidate


def fire(profile: str, job_id: str) -> bool:
    """Use the same profile scoping and CAS claim as the Chronos fire path."""
    from hermes_cli.web_server import _fire_cron_job_for_profile

    return bool(_fire_cron_job_for_profile(profile, job_id))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-passive-intake-wake")
    parser.add_argument(
        "--profile",
        required=True,
        type=lambda value: _bounded(value, PROFILE_PATTERN, "profile"),
    )
    parser.add_argument(
        "--job-id",
        required=True,
        type=lambda value: _bounded(value, JOB_ID_PATTERN, "cron job ID"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # A lost claim means another runner already owns the job. Raw remains
    # durable and the minute cron is the recovery path, so it is not an error.
    fire(args.profile, args.job_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
