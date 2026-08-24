"""Behavioral coverage for fail-closed gateway snapshot reconciliation."""

from gateway import status


def _runtime_record(profile_home, **overrides):
    record = {
        "pid": 4312,
        "start_time": 700,
        "gateway_state": "running",
        "kind": "hermes-gateway",
        "argv": ["hermes", "-p", "coder", "gateway", "run"],
        "hermes_home": str(profile_home),
    }
    record.update(overrides)
    return record


def test_running_snapshot_without_pid_is_stale_and_fails_closed(tmp_path):
    profile_home = tmp_path / "profiles" / "coder"
    record = _runtime_record(profile_home)
    record.pop("pid")

    result = status.reconcile_runtime_status(record, expected_home=profile_home)

    assert result.classification == "stale"
    assert result.reason == "missing_pid"
    assert status.get_runtime_status_running_pid(
        record,
        expected_home=profile_home,
    ) is None


def test_running_snapshot_with_dead_pid_is_stale(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "coder"
    record = _runtime_record(profile_home)
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: False)

    result = status.reconcile_runtime_status(record, expected_home=profile_home)

    assert result.classification == "stale"
    assert result.reason == "pid_not_alive"


def test_reused_pid_with_different_birth_marker_is_stale(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "coder"
    record = _runtime_record(profile_home)
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: 701)

    result = status.reconcile_runtime_status(record, expected_home=profile_home)

    assert result.classification == "stale"
    assert result.reason == "process_birth_mismatch"


def test_reused_pid_owned_by_other_profile_is_stale(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "coder"
    record = _runtime_record(profile_home)
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: 700)
    monkeypatch.setattr(
        status,
        "_read_process_cmdline",
        lambda _pid: "hermes -p reviewer gateway run --replace",
    )

    result = status.reconcile_runtime_status(record, expected_home=profile_home)

    assert result.classification == "stale"
    assert result.reason == "process_identity_mismatch"
    assert status.get_runtime_status_running_pid(
        record,
        expected_home=profile_home,
    ) is None


def test_matching_pid_birth_command_and_profile_is_running(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "coder"
    record = _runtime_record(profile_home)
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: 700)
    monkeypatch.setattr(
        status,
        "_read_process_cmdline",
        lambda _pid: "hermes --profile coder gateway run --replace",
    )

    result = status.reconcile_runtime_status(record, expected_home=profile_home)

    assert result.classification == "running"
    assert result.pid == 4312
    assert status.get_runtime_status_running_pid(
        record,
        expected_home=profile_home,
    ) == 4312


def test_matching_hidden_windows_launcher_is_running(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    launcher = profile_home / "launch-coder-gateway-hidden.pyw"
    launcher.write_text("# generated launcher\n", encoding="utf-8")
    record = _runtime_record(profile_home)
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: 700)
    monkeypatch.setattr(
        status,
        "_read_process_cmdline",
        lambda _pid: f'pythonw.exe "{launcher}"',
    )

    result = status.reconcile_runtime_status(record, expected_home=profile_home)

    assert result.classification == "running"
    assert result.pid == 4312


def test_unreadable_command_without_profile_home_is_unknown(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "coder"
    record = _runtime_record(profile_home)
    record.pop("hermes_home")
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: 700)
    monkeypatch.setattr(status, "_read_process_cmdline", lambda _pid: None)

    result = status.reconcile_runtime_status(record, expected_home=profile_home)

    assert result.classification == "unknown"
    assert result.reason == "recorded_home_unavailable"
    assert status.get_runtime_status_running_pid(
        record,
        expected_home=profile_home,
    ) is None


def test_unreadable_command_keeps_strong_persisted_identity_fallback(
    tmp_path,
    monkeypatch,
):
    profile_home = tmp_path / "profiles" / "coder"
    record = _runtime_record(profile_home)
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: 700)
    monkeypatch.setattr(status, "_read_process_cmdline", lambda _pid: None)

    result = status.reconcile_runtime_status(record, expected_home=profile_home)

    assert result.classification == "running"
    assert result.reason == "persisted_identity_matches_birth"
