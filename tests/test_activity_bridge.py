"""Tests for scripts/activity_bridge.py — slice 19a (verify mode).

Covers config loading, the AAD token request shape (mocked), the
probes individually, and the verify orchestration end-to-end.
``serve`` mode lands in slice 19b after Microsoft's BF subscription
contract is validated against documentation.
"""

from __future__ import annotations

import json
import os
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from activity_bridge import (
    GRAPH_RESOURCE,
    OBSERVABILITY_RESOURCE_APPID,
    BridgeConfigError,
    TokenAcquisitionError,
    VerifyReport,
    acquire_token,
    load_agent_env,
    load_generated_config,
    main,
    probe_generated_config,
    probe_local_config,
    probe_otlp_endpoint,
    probe_token_acquisition,
    render_human,
    render_json,
    run_verify,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_agent_env(home: Path, slug: str = "inbox-helper", **overrides: str) -> Path:
    base: dict[str, str] = {
        "AGENT_IDENTITY": slug,
        "OWNER": "sadiq@contoso.com",
        "OWNER_AAD_ID": "00000000-0000-0000-0000-000000000001",
        "A365_APP_ID": "8b563a20-2fac-4210-8210-df139c61e8b7",
        "A365_TENANT_ID": "2699fca3-dac6-40a2-bcea-62ce05e2ee9b",
        "AA_INSTANCE_ID": "550e8400-e29b-41d4-a716-446655440000",
        "HERMES_OTLP_ENDPOINT": "https://contoso.otel.agent365.microsoft.com",
    }
    base.update(overrides)
    agent_dir = home / "agents" / slug
    agent_dir.mkdir(parents=True, exist_ok=True)
    env_path = agent_dir / ".env"
    env_path.write_text("".join(f"{k}={v}\n" for k, v in base.items()))
    return env_path


def _seed_generated_config(
    cwd: Path,
    *,
    secret: str = "test-client-secret-redacted",
    blueprint_id: str = "8b563a20-2fac-4210-8210-df139c61e8b7",
    mode: int = 0o600,
) -> Path:
    path = cwd / "a365.generated.config.json"
    path.write_text(
        json.dumps(
            {
                "agentBlueprintId": blueprint_id,
                "agentBlueprintClientSecret": secret,
                "agentBlueprintObjectId": "obj-id",
                "agentBlueprintServicePrincipalObjectId": "sp-id",
            }
        )
    )
    os.chmod(path, mode)
    return path


def _aad_http_error(status: int, body: dict[str, Any]) -> urllib.error.HTTPError:
    """Build a stub HTTPError that behaves like one returned by AAD."""
    return urllib.error.HTTPError(
        url="https://login.microsoftonline.com/x/oauth2/v2.0/token",
        code=status,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(json.dumps(body).encode("utf-8")),
    )


# ---------------------------------------------------------------------------
# load_agent_env
# ---------------------------------------------------------------------------


class TestLoadAgentEnv:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(BridgeConfigError, match="instance create"):
            load_agent_env(tmp_path, "ghost")

    def test_happy_path(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        env = load_agent_env(tmp_path, "inbox-helper")
        assert env["A365_APP_ID"] == "8b563a20-2fac-4210-8210-df139c61e8b7"
        assert env["AA_INSTANCE_ID"] == "550e8400-e29b-41d4-a716-446655440000"


# ---------------------------------------------------------------------------
# load_generated_config
# ---------------------------------------------------------------------------


class TestLoadGeneratedConfig:
    def test_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(BridgeConfigError, match="register --apply"):
            load_generated_config(tmp_path / "a365.generated.config.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "a365.generated.config.json"
        path.write_text("not json {{")
        with pytest.raises(BridgeConfigError, match="not JSON"):
            load_generated_config(path)

    def test_happy_path(self, tmp_path: Path) -> None:
        path = _seed_generated_config(tmp_path)
        cfg = load_generated_config(path)
        assert cfg["agentBlueprintClientSecret"]
        assert cfg["agentBlueprintId"]


# ---------------------------------------------------------------------------
# acquire_token
# ---------------------------------------------------------------------------


class TestAcquireToken:
    def test_success_returns_payload(self) -> None:
        sample = {
            "token_type": "Bearer",
            "expires_in": 3599,
            "access_token": "eyJ0eXAi…",
        }
        # Stub urlopen to return our payload.
        with patch("activity_bridge.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
                sample
            ).encode("utf-8")
            out = acquire_token(
                tenant_id="t", client_id="c", client_secret="s"
            )
        assert out["access_token"] == "eyJ0eXAi…"
        assert out["expires_in"] == 3599

    def test_request_body_shape(self) -> None:
        captured: dict[str, Any] = {}

        def fake_urlopen(req: Any, timeout: float = 0) -> Any:
            captured["url"] = req.full_url
            captured["body"] = req.data.decode("utf-8")
            captured["method"] = req.get_method()
            captured["content_type"] = req.headers.get("Content-type")

            class _Ctx:
                def __enter__(self_inner) -> Any:
                    return _Ctx()

                def __exit__(self_inner, *_: Any) -> None:
                    pass

                def read(self_inner) -> bytes:
                    return json.dumps({"access_token": "x", "expires_in": 60}).encode("utf-8")

            return _Ctx()

        with patch("activity_bridge.urllib.request.urlopen", side_effect=fake_urlopen):
            acquire_token(
                tenant_id="t-123",
                client_id="appid-abc",
                client_secret="sek/ret with+chars",
                resource=GRAPH_RESOURCE,
            )
        assert captured["url"] == "https://login.microsoftonline.com/t-123/oauth2/v2.0/token"
        assert captured["method"] == "POST"
        assert captured["content_type"] == "application/x-www-form-urlencoded"
        # Body should be form-urlencoded with proper quoting.
        body = captured["body"]
        assert "grant_type=client_credentials" in body
        assert "client_id=appid-abc" in body
        # Spaces and `+` and `/` get percent-encoded; `+` becomes `%2B`.
        assert "client_secret=sek%2Fret+with%2Bchars" in body
        # urlencode percent-encodes `:` and `/` in the value too.
        assert "scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in body

    def test_aadsts_code_extracted_from_error(self) -> None:
        body = {
            "error": "invalid_client",
            "error_description": (
                "AADSTS7000222: The provided client secret keys "
                "for app are expired"
            ),
        }
        with patch(
            "activity_bridge.urllib.request.urlopen",
            side_effect=_aad_http_error(401, body),
        ), pytest.raises(TokenAcquisitionError) as excinfo:
            acquire_token(tenant_id="t", client_id="c", client_secret="s")
        assert excinfo.value.code == "AADSTS7000222"
        assert "expired" in excinfo.value.message

    def test_non_aadsts_error_falls_back_to_error_field(self) -> None:
        with patch(
            "activity_bridge.urllib.request.urlopen",
            side_effect=_aad_http_error(400, {"error": "bad_request", "error_description": "bad"}),
        ), pytest.raises(TokenAcquisitionError) as excinfo:
            acquire_token(tenant_id="t", client_id="c", client_secret="s")
        assert excinfo.value.code == "bad_request"

    def test_url_error_surfaces_as_network_error(self) -> None:
        with patch(
            "activity_bridge.urllib.request.urlopen",
            side_effect=urllib.error.URLError("name resolution failed"),
        ), pytest.raises(TokenAcquisitionError) as excinfo:
            acquire_token(tenant_id="t", client_id="c", client_secret="s")
        assert excinfo.value.code == "network_error"


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


class TestProbeLocalConfig:
    def test_missing_env_yields_error(self, tmp_path: Path) -> None:
        probe, env = probe_local_config(tmp_path, "ghost")
        assert probe.state == "error"
        assert env == {}

    def test_missing_required_keys_yields_error(self, tmp_path: Path) -> None:
        # AGENT_IDENTITY is fine but A365_APP_ID is required.
        agent_dir = tmp_path / "agents" / "ghost"
        agent_dir.mkdir(parents=True)
        (agent_dir / ".env").write_text("AGENT_IDENTITY=ghost\n")
        probe, _env = probe_local_config(tmp_path, "ghost")
        assert probe.state == "error"
        assert "missing keys" in probe.detail

    def test_happy_path(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        probe, env = probe_local_config(tmp_path, "inbox-helper")
        assert probe.state == "ok"
        assert env["A365_APP_ID"]


class TestProbeGeneratedConfig:
    def test_missing_yields_error(self, tmp_path: Path) -> None:
        probe, data = probe_generated_config(tmp_path / "a365.generated.config.json")
        assert probe.state == "error"
        assert data == {}

    def test_warns_on_world_readable_perms(self, tmp_path: Path) -> None:
        # Slice 18x policy: secret-bearing files must be 0600. Verify
        # the bridge surfaces a warning if the operator's filesystem
        # left them looser.
        path = _seed_generated_config(tmp_path, mode=0o644)
        probe, data = probe_generated_config(path)
        assert probe.state == "warn"
        assert "chmod 600" in probe.detail
        # Secret was still extracted — the probe is a warning, not a hard error.
        assert data["client_secret"]

    def test_happy_path_at_0600(self, tmp_path: Path) -> None:
        path = _seed_generated_config(tmp_path, mode=0o600)
        probe, data = probe_generated_config(path)
        assert probe.state == "ok"
        assert data["blueprint_id"]
        assert data["client_secret"]


class TestProbeTokenAcquisition:
    def test_ok_on_successful_token(self) -> None:
        with patch(
            "activity_bridge.acquire_token",
            return_value={"token_type": "Bearer", "expires_in": 3599, "access_token": "x"},
        ):
            r = probe_token_acquisition(
                tenant_id="t", client_id="c", client_secret="s"
            )
        assert r.state == "ok"
        assert "expires_in=3599" in r.detail

    def test_invalid_secret_yields_error(self) -> None:
        with patch(
            "activity_bridge.acquire_token",
            side_effect=TokenAcquisitionError("AADSTS7000215", "invalid secret"),
        ):
            r = probe_token_acquisition(
                tenant_id="t", client_id="c", client_secret="s"
            )
        assert r.state == "error"
        assert "rotate" in r.detail

    def test_no_role_grants_yields_warn_not_error(self) -> None:
        # AADSTS7000218 is "the app has no permissions on this resource"
        # — diagnostic-positive: the secret WORKS, just the scope is wrong.
        with patch(
            "activity_bridge.acquire_token",
            side_effect=TokenAcquisitionError("AADSTS7000218", "no role"),
        ):
            r = probe_token_acquisition(
                tenant_id="t", client_id="c", client_secret="s"
            )
        assert r.state == "warn"
        assert "secret valid but no role" in r.detail


class TestProbeOtlpEndpoint:
    def test_unset_yields_warn(self) -> None:
        r = probe_otlp_endpoint(None)
        assert r.state == "warn"

    def test_no_host_yields_error(self) -> None:
        # urlparse is lenient — "htp://" still yields a parseable URL
        # with no host. The probe should report this as an error since
        # there's nothing to DNS-resolve.
        r = probe_otlp_endpoint("htp:///no-scheme-no-host")
        assert r.state == "error"
        assert "no host" in r.detail

    def test_dns_lookup_failure_yields_warn(self) -> None:
        with patch("activity_bridge.socket.gethostbyname", side_effect=OSError("no DNS")):
            r = probe_otlp_endpoint("https://otel.example.invalid/")
        assert r.state == "warn"
        assert "DNS" in r.detail

    def test_dns_resolves_yields_ok(self) -> None:
        with patch("activity_bridge.socket.gethostbyname", return_value="1.2.3.4"):
            r = probe_otlp_endpoint("https://otel.example.com/")
        assert r.state == "ok"


# ---------------------------------------------------------------------------
# run_verify orchestration
# ---------------------------------------------------------------------------


class TestRunVerify:
    def test_skips_token_when_local_config_missing(self, tmp_path: Path) -> None:
        # No agent .env. Token probe should be skipped (not crashed).
        report = run_verify(slug="ghost", hermes_home=tmp_path)
        token_probes = [p for p in report.probes if p.name == "token_acquisition"]
        assert len(token_probes) == 1
        assert token_probes[0].state == "warn"
        assert "skipped" in token_probes[0].detail

    def test_full_happy_path(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        gen_path = _seed_generated_config(tmp_path)
        with (
            patch(
                "activity_bridge.acquire_token",
                return_value={"token_type": "Bearer", "expires_in": 3599, "access_token": "x"},
            ),
            patch("activity_bridge.tcp_reachable", return_value=True),
            patch("activity_bridge.socket.gethostbyname", return_value="1.2.3.4"),
        ):
            report = run_verify(
                slug="inbox-helper",
                hermes_home=tmp_path,
                generated_config_path=gen_path,
            )
        assert report.overall == "ok"
        names = [p.name for p in report.probes]
        assert names == [
            "local_config",
            "generated_config",
            "token_acquisition",
            "reachability",
            "otlp_endpoint",
        ]


# ---------------------------------------------------------------------------
# Rendering + CLI
# ---------------------------------------------------------------------------


class TestRender:
    def _green_report(self) -> VerifyReport:
        from activity_bridge import ProbeResult

        return VerifyReport(
            slug="x",
            probes=[
                ProbeResult("local_config", "ok", "ok"),
                ProbeResult("token_acquisition", "ok", "ok"),
            ],
        )

    def test_human_includes_slug_and_overall(self) -> None:
        text = render_human(self._green_report())
        assert "verify — x" in text
        assert "overall: ok" in text

    def test_json_is_parseable(self) -> None:
        out = render_json(self._green_report())
        parsed = json.loads(out)
        assert parsed["slug"] == "x"
        assert parsed["overall"] == "ok"
        assert len(parsed["probes"]) == 2


class TestCli:
    def test_verify_exit_codes_track_overall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_agent_env(tmp_path)
        gen_path = _seed_generated_config(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with (
            patch(
                "activity_bridge.acquire_token",
                return_value={"token_type": "Bearer", "expires_in": 3599, "access_token": "x"},
            ),
            patch("activity_bridge.tcp_reachable", return_value=True),
            patch("activity_bridge.socket.gethostbyname", return_value="1.2.3.4"),
        ):
            rc = main(
                [
                    "verify",
                    "--slug",
                    "inbox-helper",
                    "--generated-config",
                    str(gen_path),
                    "--human",
                ]
            )
        assert rc == 0
        out = capsys.readouterr().out
        assert "overall: ok" in out

    def test_verify_returns_2_when_secret_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_agent_env(tmp_path)
        # No generated-config file → probe error → overall=error → exit 2.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        rc = main(
            [
                "verify",
                "--slug",
                "inbox-helper",
                "--generated-config",
                str(tmp_path / "missing.json"),
            ]
        )
        assert rc == 2


# ---------------------------------------------------------------------------
# Pinned constants
# ---------------------------------------------------------------------------


def test_observability_resource_pinned() -> None:
    # Verified in the 2026-05-05 round-2 walkthrough as the
    # `Agent365Observability` resource appId. Pin so a future
    # refactor surfaces the change here first.
    assert OBSERVABILITY_RESOURCE_APPID == "9b975845-388f-4429-889e-eab1ef63949c"
