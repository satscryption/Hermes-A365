"""Tests for scripts/doctor.py.

Probes shell out and touch the network; tests monkeypatch the underlying
primitives so they're hermetic and fast.
"""

from __future__ import annotations

import json
from pathlib import Path

import doctor
import pytest
from doctor import (
    ProbeResult,
    aggregate_state,
    collect_probes,
    detect_a365_variant,
    probe_a365_cli,
    probe_az_cli,
    probe_hermes_harness,
    probe_keychain,
    probe_local_config,
    probe_network,
    render_human,
    render_json,
)

# ---------------------------------------------------------------------------
# Pure-function tests (no monkeypatching needed)
# ---------------------------------------------------------------------------


class TestDetectVariant:
    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A365_CLI_VARIANT", "atk-npm")
        assert detect_a365_variant("/anywhere", "anything") == "atk-npm"

    def test_env_override_ignored_if_unknown_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A365_CLI_VARIANT", "garbage")
        # Falls through to next heuristic
        assert detect_a365_variant("/usr/bin/a365", "atk 1.0.0") == "atk-npm"

    @pytest.mark.parametrize(
        "version_output,expected",
        [
            ("atk 1.2.3", "atk-npm"),
            ("Microsoft (R) .NET host for Agent 365 1.0.0", "a365-dotnet"),
            ("dotnet runtime 8", "a365-dotnet"),
            ("node v20.0.0 — atk wrapper", "atk-npm"),
        ],
    )
    def test_version_signature(
        self, version_output: str, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("A365_CLI_VARIANT", raising=False)
        assert detect_a365_variant("/somewhere/a365", version_output) == expected

    @pytest.mark.parametrize(
        "binary_path,expected",
        [
            ("/Users/x/.npm/bin/a365", "atk-npm"),
            ("/usr/local/lib/node_modules/atk/a365", "atk-npm"),
            ("/Users/x/.dotnet/tools/a365", "a365-dotnet"),
            ("/opt/dotnet/tools/a365", "a365-dotnet"),
        ],
    )
    def test_path_heuristic_when_version_unhelpful(
        self,
        binary_path: str,
        expected: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("A365_CLI_VARIANT", raising=False)
        # Generic version output that doesn't match either signature
        assert detect_a365_variant(binary_path, "1.0.0") == expected

    def test_unknown_when_no_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("A365_CLI_VARIANT", raising=False)
        assert detect_a365_variant("/usr/bin/a365", "1.0.0") == "unknown"


class TestAggregateState:
    def _r(self, state: str) -> ProbeResult:
        return ProbeResult(name="x", state=state, detail="")  # type: ignore[arg-type]

    def test_all_ok(self) -> None:
        probes = [self._r("ok"), self._r("ok")]
        assert aggregate_state(probes) == ("ok", 0)

    def test_any_warn_no_error(self) -> None:
        probes = [self._r("ok"), self._r("warn"), self._r("ok")]
        assert aggregate_state(probes) == ("warn", 1)

    def test_any_error_dominates(self) -> None:
        probes = [self._r("ok"), self._r("warn"), self._r("error")]
        assert aggregate_state(probes) == ("error", 2)

    def test_empty(self) -> None:
        assert aggregate_state([]) == ("ok", 0)


class TestRendering:
    def _probes(self) -> list[ProbeResult]:
        return [
            ProbeResult("a365_cli", "ok", "present", {"variant": "atk-npm"}),
            ProbeResult("network", "warn", "1/2 unreachable"),
        ]

    def test_render_json_is_valid_json(self) -> None:
        text = render_json(self._probes())
        payload = json.loads(text)
        assert payload["overall"] == "warn"
        assert len(payload["probes"]) == 2
        assert payload["probes"][0]["name"] == "a365_cli"
        assert text.endswith("\n")

    def test_render_human_includes_overall_line(self) -> None:
        text = render_human(self._probes())
        assert "hermes a365 doctor" in text
        assert "[ ok ]" in text
        assert "[warn]" in text
        assert "overall: warn" in text


# ---------------------------------------------------------------------------
# Probe tests (monkeypatched primitives)
# ---------------------------------------------------------------------------


class TestProbeA365Cli:
    def test_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
        result = probe_a365_cli()
        assert result.state == "error"
        assert "not found" in result.detail

    def test_present_atk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            doctor.shutil,
            "which",
            lambda binary: "/usr/local/bin/a365" if binary == "a365" else None,
        )
        monkeypatch.setattr(doctor, "safe_run", lambda *a, **kw: "atk 1.0.0")
        monkeypatch.delenv("A365_CLI_VARIANT", raising=False)
        result = probe_a365_cli()
        assert result.state == "ok"
        assert result.data["variant"] == "atk-npm"
        assert result.data["version"] == "atk 1.0.0"

    def test_present_unknown_variant_is_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            doctor.shutil,
            "which",
            lambda binary: "/usr/bin/a365" if binary == "a365" else None,
        )
        monkeypatch.setattr(doctor, "safe_run", lambda *a, **kw: "1.0.0")
        monkeypatch.delenv("A365_CLI_VARIANT", raising=False)
        result = probe_a365_cli()
        assert result.state == "warn"
        assert result.data["variant"] == "unknown"


class TestProbeAzCli:
    def test_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
        result = probe_az_cli()
        assert result.state == "warn"  # az is recommended, not required

    def test_present_signed_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            doctor.shutil,
            "which",
            lambda binary: "/usr/local/bin/az" if binary == "az" else None,
        )
        monkeypatch.setattr(doctor, "safe_run", lambda *a, **kw: '{"id":"…"}')
        result = probe_az_cli()
        assert result.state == "ok"
        assert result.data["signed_in"] is True

    def test_present_signed_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            doctor.shutil,
            "which",
            lambda binary: "/usr/local/bin/az" if binary == "az" else None,
        )
        monkeypatch.setattr(doctor, "safe_run", lambda *a, **kw: None)
        result = probe_az_cli()
        assert result.state == "warn"
        assert result.data["signed_in"] is False


class TestProbeNetwork:
    def test_all_reachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor, "tcp_reachable", lambda host, **kw: True)
        result = probe_network()
        assert result.state == "ok"
        assert result.data["unreachable"] == []

    def test_partial_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        unreachable = {"login.microsoftonline.com"}
        monkeypatch.setattr(doctor, "tcp_reachable", lambda host, **kw: host not in unreachable)
        result = probe_network()
        assert result.state == "warn"
        assert "login.microsoftonline.com" in result.data["unreachable"]

    def test_all_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor, "tcp_reachable", lambda host, **kw: False)
        result = probe_network()
        assert result.state == "error"

    def test_tenant_hint_adds_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def _fake(host: str, **_kw: object) -> bool:
            seen.append(host)
            return True

        monkeypatch.setattr(doctor, "tcp_reachable", _fake)
        probe_network(tenant_hint="contoso.onmicrosoft.com")
        assert "contoso.api.agent365.microsoft.com" in seen


class TestProbeKeychain:
    def test_macos_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(doctor, "safe_run", lambda *a, **kw: "/Users/x/Library/...")
        result = probe_keychain()
        assert result.state == "ok"
        assert result.data["backend"] == "macos-security"

    def test_macos_security_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(doctor, "safe_run", lambda *a, **kw: None)
        result = probe_keychain()
        assert result.state == "error"

    def test_linux_with_secret_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor.sys, "platform", "linux")
        monkeypatch.setattr(
            doctor.shutil,
            "which",
            lambda binary: "/usr/bin/secret-tool" if binary == "secret-tool" else None,
        )
        result = probe_keychain()
        assert result.state == "ok"
        assert result.data["backend"] == "libsecret"

    def test_linux_missing_secret_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor.sys, "platform", "linux")
        monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
        result = probe_keychain()
        assert result.state == "error"

    def test_windows_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor.sys, "platform", "win32")
        result = probe_keychain()
        assert result.state == "warn"
        assert result.data["backend"] is None


class TestProbeLocalConfig:
    def test_missing_home_is_warn(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        missing = tmp_path / "no-such"
        monkeypatch.setenv("HERMES_HOME", str(missing))
        result = probe_local_config()
        assert result.state == "warn"
        assert result.data["bootstrapped"] is False

    def test_empty_home_is_ok(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        result = probe_local_config()
        assert result.state == "ok"
        assert result.data["env_keys"] == []
        assert result.data["config_yaml_present"] is False
        assert result.data["tenant_hint"] is None

    def test_env_with_tenant_hint(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / ".env").write_text("A365_TENANT_ID=contoso.onmicrosoft.com\nA365_APP_ID=abc\n")
        (tmp_path / "config.yaml").write_text("foo: bar\n")
        result = probe_local_config()
        assert result.state == "ok"
        assert result.data["tenant_hint"] == "contoso.onmicrosoft.com"
        assert result.data["config_yaml_present"] is True
        assert sorted(result.data["env_keys"]) == ["A365_APP_ID", "A365_TENANT_ID"]


class TestProbeHermesHarness:
    def test_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
        result = probe_hermes_harness()
        assert result.state == "warn"
        assert result.data["binary"] is None

    def test_present_responsive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            doctor.shutil,
            "which",
            lambda binary: "/usr/local/bin/hermes" if binary == "hermes" else None,
        )
        monkeypatch.setattr(doctor, "safe_run", lambda *a, **kw: "hermes 0.12.0")
        result = probe_hermes_harness()
        assert result.state == "ok"
        assert result.data["version"] == "hermes 0.12.0"

    def test_present_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            doctor.shutil,
            "which",
            lambda binary: "/usr/local/bin/hermes" if binary == "hermes" else None,
        )
        monkeypatch.setattr(doctor, "safe_run", lambda *a, **kw: None)
        result = probe_hermes_harness()
        assert result.state == "warn"


# ---------------------------------------------------------------------------
# End-to-end orchestration test
# ---------------------------------------------------------------------------


def test_collect_probes_threads_tenant_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """probe_local_config's tenant_hint should be passed to probe_network."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("A365_TENANT_ID=acme.onmicrosoft.com\n")

    # Stub everything network/system-touching.
    seen_hosts: list[str] = []

    def _fake_tcp(host: str, **_kw: object) -> bool:
        seen_hosts.append(host)
        return True

    monkeypatch.setattr(doctor, "tcp_reachable", _fake_tcp)
    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(doctor, "safe_run", lambda *a, **kw: None)
    monkeypatch.setattr(doctor.sys, "platform", "linux")

    probes = collect_probes()
    assert any("acme.api.agent365.microsoft.com" in h for h in seen_hosts)
    # Aggregation must still produce a sensible result.
    overall, code = aggregate_state(probes)
    assert overall in {"ok", "warn", "error"}
    assert code in {0, 1, 2}


def test_collect_probes_skip_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(doctor, "safe_run", lambda *a, **kw: None)
    monkeypatch.setattr(doctor.sys, "platform", "darwin")

    probes = collect_probes(skip_network=True)
    names = [p.name for p in probes]
    assert "network" not in names
