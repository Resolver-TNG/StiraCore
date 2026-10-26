"""ADR 0012 §1 / §7 — the start-up allowance and the clock support check (phase 1)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from stiracore.yolo import runtime
from stiracore.yolo.allowance import (
    ALLOW_ENV,
    CAP_ENV,
    EXTERNAL_CAP_ENV,
    HOST_CONFIRM_ENV,
    YoloSettingError,
    YoloSettings,
    clock_support,
    parse_settings,
    read_clocks,
)

ROOT = Path(__file__).resolve().parents[1]


def test_explicit_off_is_off():
    # ADR 0012 addendum 2026-10-24: unset is ``external`` now; ``off`` says it explicitly.
    assert parse_settings({ALLOW_ENV: "off"}) == YoloSettings()
    assert not parse_settings({ALLOW_ENV: "off"}).enabled
    assert parse_settings({ALLOW_ENV: "off"}).sources["allow"] == "env"
    assert parse_settings({ALLOW_ENV: "off", CAP_ENV: "", EXTERNAL_CAP_ENV: ""}) == YoloSettings()
    # The server's wildcard fallback: the default itself is off.
    assert parse_settings({}, default_allow="off") == YoloSettings()


def test_unset_and_empty_are_the_external_default():
    for env in ({}, {ALLOW_ENV: ""}, {ALLOW_ENV: "", CAP_ENV: "", EXTERNAL_CAP_ENV: ""}):
        settings = parse_settings(env)
        assert settings.enabled and settings.allowance == "external"
        assert settings.sources["allow"] == "default"
        assert (settings.cap, settings.external_cap, settings.exec_cap) == (2000, 200, 200)
        assert settings.host_confirm == "browser" and settings.exec_offered is False


@pytest.mark.parametrize("value", ["internal", "external"])
def test_exact_tiers_are_accepted(value):
    settings = parse_settings({ALLOW_ENV: value})
    assert settings.allowance == value and settings.enabled
    assert (settings.cap, settings.external_cap) == (2000, 200)


@pytest.mark.parametrize(
    "value",
    ["on", "1", "all", "Internal", "EXTERNAL", " internal", "internal ", "true", "OFF", " off"],
)
def test_invalid_allowance_refuses_and_names_the_variable_not_the_value(value):
    with pytest.raises(YoloSettingError) as caught:
        parse_settings({ALLOW_ENV: value})
    assert ALLOW_ENV in str(caught.value)
    assert repr(value) not in str(caught.value)


@pytest.mark.parametrize(
    "env",
    [
        {ALLOW_ENV: "internal", CAP_ENV: "0"},
        {ALLOW_ENV: "internal", CAP_ENV: "10001"},
        {ALLOW_ENV: "internal", CAP_ENV: "-1"},
        {ALLOW_ENV: "internal", CAP_ENV: "1e3"},
        {ALLOW_ENV: "internal", CAP_ENV: " 10"},
        {ALLOW_ENV: "internal", EXTERNAL_CAP_ENV: "x"},
        {ALLOW_ENV: "external", CAP_ENV: "100", EXTERNAL_CAP_ENV: "101"},
        {ALLOW_ENV: "off", CAP_ENV: "100"},  # a cap with the allowance off
        {ALLOW_ENV: "off", EXTERNAL_CAP_ENV: "0"},
    ],
)
def test_invalid_caps_refuse(env):
    with pytest.raises(YoloSettingError):
        parse_settings(env)


def test_caps_in_range():
    settings = parse_settings({ALLOW_ENV: "external", CAP_ENV: "10000", EXTERNAL_CAP_ENV: "0"})
    assert (settings.cap, settings.external_cap) == (10000, 0)
    settings = parse_settings({ALLOW_ENV: "internal", CAP_ENV: "50"})
    assert (settings.cap, settings.external_cap) == (50, 50)  # default sub-cap ≤ cap


@pytest.mark.parametrize("value", ["browser", "auto", "command", "dialog"])
def test_host_confirm_values(value):
    settings = parse_settings({ALLOW_ENV: "internal", HOST_CONFIRM_ENV: value})
    assert settings.host_confirm == value
    # Revision 6: a host channel named → the host is required; browser is the default.
    assert settings.host_required is (value != "browser")
    unset = parse_settings({ALLOW_ENV: "internal", HOST_CONFIRM_ENV: ""})
    assert unset.host_confirm == "browser" and unset.host_required is False
    assert parse_settings({ALLOW_ENV: "internal"}).host_confirm == "browser"


@pytest.mark.parametrize(
    "env",
    [
        {ALLOW_ENV: "internal", HOST_CONFIRM_ENV: "Dialog"},
        {ALLOW_ENV: "internal", HOST_CONFIRM_ENV: "none"},
        {ALLOW_ENV: "internal", HOST_CONFIRM_ENV: " command"},
        {ALLOW_ENV: "off", HOST_CONFIRM_ENV: "command"},  # with the allowance off
    ],
)
def test_invalid_host_confirm_refuses_naming_the_variable_only(env):
    with pytest.raises(YoloSettingError) as caught:
        parse_settings(env)
    assert HOST_CONFIRM_ENV in str(caught.value)
    assert repr(env[HOST_CONFIRM_ENV]) not in str(caught.value)


def test_allows_follows_the_tier_order():
    internal = YoloSettings("internal")
    assert internal.allows("internal") and not internal.allows("external")
    assert YoloSettings("external").allows("external")
    assert not YoloSettings().allows("internal")
    assert not internal.allows("off") and not internal.allows("all")


def test_settings_are_read_once(monkeypatch):
    runtime._reset_for_tests()
    try:
        monkeypatch.setenv(ALLOW_ENV, "internal")
        assert runtime.settings().allowance == "internal"
        monkeypatch.setenv(ALLOW_ENV, "external")  # later os.environ changes: no effect
        assert runtime.settings().allowance == "internal"
        monkeypatch.delenv(ALLOW_ENV)
        assert runtime.enabled()
    finally:
        runtime._reset_for_tests()


# --- clocks (§7) ----------------------------------------------------------------------


def _fake_time(implementation="mach_absolute_time()", monotonic_clock=True):
    return SimpleNamespace(
        CLOCK_MONOTONIC=6,
        clock_gettime=lambda _id: 123.0,
        get_clock_info=lambda _name: SimpleNamespace(
            implementation=implementation, monotonic=monotonic_clock
        ),
        time=lambda: 1000.0,
        monotonic=lambda: 100.0,
    )


def test_measured_platform_with_both_clocks_is_supported():
    support = clock_support("darwin", _fake_time())
    assert support.supported and support.reason is None
    reading = read_clocks("darwin", _fake_time())
    assert (reading.wall, reading.boot, reading.mono) == (1000.0, 123.0, 100.0)


@pytest.mark.parametrize("platform", ["linux", "win32", "cygwin", "freebsd14"])
def test_unmeasured_platforms_are_not_offered(platform):
    support = clock_support(platform, _fake_time())
    assert not support.supported and support.reason == "clock_not_measured"
    with pytest.raises(RuntimeError):
        read_clocks(platform, _fake_time())


def test_missing_or_unexpected_clock_is_not_offered():
    no_clock = _fake_time()
    del no_clock.CLOCK_MONOTONIC
    assert clock_support("darwin", no_clock).reason == "clock_unavailable"

    def broken(_id):
        raise OSError("no clock")

    failing = _fake_time()
    failing.clock_gettime = broken
    assert clock_support("darwin", failing).reason == "clock_unavailable"
    assert clock_support("darwin", _fake_time("clock_gettime(CLOCK_MONOTONIC)")).reason == (
        "clock_unexpected"
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="measured on macOS only (ADR 0012 §7)")
def test_this_mac_matches_the_measurement():
    import time

    assert clock_support().supported
    reading = read_clocks()
    # CLOCK_MONOTONIC counts sleep, time.monotonic does not: B is never behind U.
    assert reading.boot >= reading.mono - 1
    assert time.get_clock_info("monotonic").implementation == "mach_absolute_time()"


def test_status_reports_clock_support_and_offers_nothing(monkeypatch):
    runtime._reset_for_tests(YoloSettings("internal"))
    try:
        from stiracore.yolo import allowance

        monkeypatch.setattr(runtime, "clock_support", lambda: allowance.clock_support("linux"))
        body = runtime.status()
        assert body["allowance"] == "internal"
        assert body["offered"] is False
        assert body["clock_supported"] is False
        assert body["unavailable"][0] == "clock_not_measured"
        # No desk was installed here either (phase 5b: every reason is listed).
        assert "host_channel_unavailable" in body["unavailable"]
        assert body["window"] is None and body["request"] is None
    finally:
        runtime._reset_for_tests()


# --- the server refuses to start ------------------------------------------------------


@pytest.mark.parametrize("value", ["on", "Internal"])
def test_server_refuses_start_with_an_invalid_allowance(tmp_path, value):
    env = {
        key: val
        for key, val in os.environ.items()
        if not key.startswith(("STIRA_", "AWS_", "BEDROCK"))
    }
    env.update(
        {
            ALLOW_ENV: value,
            "STIRA_DATA_DIR": str(tmp_path / "data"),
            "STIRA_PORT": "5751",
            "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}",
        }
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "dashboard" / "stira_server.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 2
    assert f"Stira: {ALLOW_ENV} must be" in result.stderr
    assert repr(value) not in result.stderr and f"={value}" not in result.stderr
    # Nothing started: no data directory, no lock (refused before create_app).
    assert not (tmp_path / "data").exists()


def _server_env(tmp_path, **extra):
    env = {
        key: val
        for key, val in os.environ.items()
        if not key.startswith(("STIRA_", "AWS_", "BEDROCK"))
    }
    env.update(
        {
            "STIRA_DATA_DIR": str(tmp_path / "data"),
            "STIRA_PORT": "5751",
            "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}",
            **extra,
        }
    )
    return env


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_server_refuses_a_wildcard_bind_with_the_allowance(tmp_path, host):
    env = _server_env(
        tmp_path, STIRA_YOLO_ALLOW="internal", STIRA_HOST=host, STIRA_ALLOW_REMOTE="1"
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "dashboard" / "stira_server.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 2
    ours = [line for line in result.stderr.splitlines() if line.startswith("Stira: STIRA_YOLO")]
    assert ours == [
        (
            "Stira: STIRA_YOLO_ALLOW needs a listener on one local address; "
            "set STIRA_HOST to a loopback or a single local address (not a wildcard)"
        )
    ]  # a fixed message: no address (the earlier #185 bind warning is unchanged)
    assert not (tmp_path / "data").exists()


def test_wildcard_bind_without_the_allowance_starts_as_before(tmp_path):
    """ADR 0012 addendum 2026-10-24: unset is the external default, which never refuses a
    wildcard listener — the default is off there (one start-up line), as before."""
    import time

    env = _server_env(tmp_path, STIRA_HOST="0.0.0.0", STIRA_ALLOW_REMOTE="1")
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "dashboard" / "stira_server.py")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 90
        output = []
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            output.append(line)
            if "Running on" in line:
                break
        assert any("Running on" in line for line in output), "".join(output)[-2000:]
        assert process.poll() is None
        assert any("YOLO: not offered on a wildcard listener" in line for line in output)
        assert not any("YOLO allowance:" in line for line in output)
    finally:
        process.terminate()
        process.wait(timeout=30)
