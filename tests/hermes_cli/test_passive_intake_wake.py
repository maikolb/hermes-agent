from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from hermes_cli import passive_intake_wake


def test_main_calls_existing_cross_profile_fire(monkeypatch):
    observed = []
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.web_server",
        SimpleNamespace(
            _fire_cron_job_for_profile=lambda profile, job_id: observed.append(
                (profile, job_id)
            )
            or True
        ),
    )

    assert passive_intake_wake.main(
        [
            "--profile",
            "hermes-project-factory",
            "--job-id",
            "b87b386b5cd5",
        ]
    ) == 0
    assert observed == [("hermes-project-factory", "b87b386b5cd5")]


@pytest.mark.parametrize(
    ("flag", "value"),
    (("--profile", "../escape"), ("--job-id", "bad/id")),
)
def test_parser_rejects_unbounded_targets(flag, value):
    argv = [
        "--profile",
        "hermes-project-factory",
        "--job-id",
        "b87b386b5cd5",
    ]
    argv[argv.index(flag) + 1] = value

    with pytest.raises(SystemExit):
        passive_intake_wake.main(argv)
