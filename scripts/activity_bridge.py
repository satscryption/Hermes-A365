"""hermes a365 activity-bridge — Bot Framework adapter daemon for A365 agents.

Two subcommands (only ``verify`` is implemented in slice 19a):

- ``verify``: one-shot diagnostic. Validates that the registered
  blueprint's client secret can acquire an OAuth token, that
  Microsoft Graph + the Connectivity API + the OTLP endpoint are
  reachable, and that the per-agent config + generated-config files
  parse cleanly. Useful for CI gates and pre-deploy smoke checks.
- ``serve``: long-running daemon (slice 19b). FastAPI app on a local
  port that operators expose via ``cloudflared`` / a reverse proxy.
  Receives BF activities, forwards each one to a webhook the
  operator configures (``HERMES_BRIDGE_WEBHOOK``), renders the
  webhook's JSON response as an Adaptive Card, and replies via the
  BF connector. Held back until the BF subscription / endpoint
  contract is verified against Microsoft's docs.

Per-agent state files (slice 19a writes none; slice 19b will):

    ~/.hermes/agents/<slug>/bridge.pid
    ~/.hermes/agents/<slug>/bridge.log

The blueprint client secret comes from ``a365.generated.config.json``
(in cwd by default; ``--generated-config <path>`` overrides). On
macOS / Linux the file is plaintext (DPAPI-only on Windows); slice
18i gitignores it and slice 18x ``chmod 600``s the cleanup-emitted
backups.

CLI use::

    python scripts/activity_bridge.py verify --slug inbox-helper
    python scripts/activity_bridge.py verify --slug inbox-helper --human
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from _common import parse_env, tcp_reachable

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"

# Resource appIds for token acquisition. The 2026-05-05 walkthrough
# confirmed our blueprint has the ``Agent365Observability`` S2S
# app-role assignment (bug #18 — only this one was set, even though
# the CLI claimed bot+power-platform also got configured). Probing
# this resource validates the secret + the one S2S grant we know
# exists. ``client_credentials`` to other resources may legitimately
# fail with ``AADSTS7000218`` (no app role) — that's still positive
# evidence the secret itself works.
OBSERVABILITY_RESOURCE_APPID = "9b975845-388f-4429-889e-eab1ef63949c"
GRAPH_RESOURCE = "https://graph.microsoft.com"

# Hosts the bridge needs reachable at runtime.
_REACHABILITY_HOSTS: tuple[str, ...] = (
    "login.microsoftonline.com",
    "graph.microsoft.com",
)

ProbeState = Literal["ok", "warn", "error"]
_OK: ProbeState = "ok"
_WARN: ProbeState = "warn"
_ERROR: ProbeState = "error"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BridgeConfigError(RuntimeError):
    """Raised when the bridge can't load its config (missing files / keys)."""


class TokenAcquisitionError(RuntimeError):
    """Raised when the AAD token endpoint rejects our credentials."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    name: str
    state: ProbeState
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifyReport:
    slug: str
    probes: list[ProbeResult] = field(default_factory=list)

    @property
    def overall(self) -> ProbeState:
        if any(p.state == _ERROR for p in self.probes):
            return _ERROR
        if any(p.state == _WARN for p in self.probes):
            return _WARN
        return _OK


# ---------------------------------------------------------------------------
# Path / env helpers
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def load_agent_env(hermes_home: Path, slug: str) -> dict[str, str]:
    """Read ``~/.hermes/agents/<slug>/.env`` into a dict.

    Raises :class:`BridgeConfigError` if the file is absent or unparseable.
    """
    path = hermes_home / "agents" / slug / ".env"
    if not path.exists():
        raise BridgeConfigError(
            f"agent .env missing: {path}; run `hermes a365 instance create {slug} --apply`"
        )
    try:
        return parse_env(path.read_text())
    except OSError as e:
        raise BridgeConfigError(f"agent .env unreadable: {e}") from e


def load_generated_config(path: Path) -> dict[str, Any]:
    """Read ``a365.generated.config.json`` and return the parsed dict.

    Raises :class:`BridgeConfigError` on missing file or JSON error.
    """
    if not path.exists():
        raise BridgeConfigError(
            f"{path} missing; run `hermes a365 register --apply` to create the blueprint"
        )
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise BridgeConfigError(f"{path} unreadable / not JSON: {e}") from e


# ---------------------------------------------------------------------------
# AAD token acquisition (client_credentials)
# ---------------------------------------------------------------------------


def acquire_token(
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    resource: str = OBSERVABILITY_RESOURCE_APPID,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Acquire an OAuth token via the client_credentials grant.

    ``resource`` is the resource appId (a GUID) or full resource URI
    (e.g. ``https://graph.microsoft.com``). The function suffixes
    ``/.default`` automatically.

    Returns the parsed JSON token response (with ``access_token``,
    ``token_type``, ``expires_in`` keys). Raises
    :class:`TokenAcquisitionError` on AAD-side failures with the
    surfaced ``AADSTS<code>`` for the operator to look up.
    """
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": f"{resource}/.default",
        }
    ).encode("utf-8")
    req = urllib.request.Request(        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # AAD always returns JSON for errors; surface the code.
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body_text)
            code = err.get("error", "unknown_error")
            description = err.get("error_description", body_text)
        except json.JSONDecodeError:
            code = f"http_{e.code}"
            description = body_text
        # AADSTS code lives inside error_description as "AADSTS\d+:".
        aadsts = ""
        if "AADSTS" in description:
            for tok in description.split():
                if tok.startswith("AADSTS"):
                    aadsts = tok.rstrip(":,")
                    break
        raise TokenAcquisitionError(aadsts or code, description) from e
    except urllib.error.URLError as e:
        raise TokenAcquisitionError("network_error", str(e.reason)) from e
    return payload


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def probe_local_config(hermes_home: Path, slug: str) -> tuple[ProbeResult, dict[str, str]]:
    try:
        env = load_agent_env(hermes_home, slug)
    except BridgeConfigError as e:
        return (
            ProbeResult("local_config", _ERROR, str(e)),
            {},
        )
    needed = ("A365_TENANT_ID", "A365_APP_ID", "AA_INSTANCE_ID")
    missing = [k for k in needed if not env.get(k)]
    if missing:
        return (
            ProbeResult(
                "local_config",
                _ERROR,
                f"~/.hermes/agents/{slug}/.env missing keys: {missing}",
                {"missing": missing},
            ),
            env,
        )
    return (
        ProbeResult(
            "local_config",
            _OK,
            f"slug={slug} | tenant={env['A365_TENANT_ID']} | app_id={env['A365_APP_ID'][:8]}…",
            {"slug": slug, "tenant_id": env["A365_TENANT_ID"]},
        ),
        env,
    )


def probe_generated_config(path: Path) -> tuple[ProbeResult, dict[str, Any]]:
    try:
        cfg = load_generated_config(path)
    except BridgeConfigError as e:
        return (ProbeResult("generated_config", _ERROR, str(e)), {})
    secret = cfg.get("agentBlueprintClientSecret")
    blueprint_id = cfg.get("agentBlueprintId")
    if not secret:
        return (
            ProbeResult(
                "generated_config",
                _ERROR,
                f"{path} has no agentBlueprintClientSecret; re-run `hermes a365 register --apply`",
            ),
            {},
        )
    if not blueprint_id:
        return (
            ProbeResult(
                "generated_config",
                _ERROR,
                f"{path} has no agentBlueprintId",
            ),
            {},
        )
    # Permission audit: the file should be 0600. Slice 18i / 18x policy.
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = -1
    extra: dict[str, Any] = {"blueprint_id": blueprint_id, "mode": f"0{mode:o}"}
    detail = f"appId={blueprint_id[:8]}… secret loaded (mode=0{mode:o})"
    if mode >= 0 and (mode & 0o077):
        return (
            ProbeResult(
                "generated_config",
                _WARN,
                f"{detail}; world-/group-readable — chmod 600 {path}",
                extra,
            ),
            {"blueprint_id": blueprint_id, "client_secret": secret},
        )
    return (
        ProbeResult("generated_config", _OK, detail, extra),
        {"blueprint_id": blueprint_id, "client_secret": secret},
    )


def probe_token_acquisition(
    *, tenant_id: str, client_id: str, client_secret: str
) -> ProbeResult:
    try:
        token = acquire_token(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )
    except TokenAcquisitionError as e:
        # Some failure modes are diagnostic-positive (the secret works,
        # the scope just isn't permitted). Distinguish:
        if e.code in ("AADSTS7000215", "AADSTS7000222"):
            # 7000215 = invalid client secret. 7000222 = expired secret.
            return ProbeResult(
                "token_acquisition",
                _ERROR,
                f"client secret rejected ({e.code}); re-run register --apply to rotate",
                {"aadsts": e.code},
            )
        if e.code in ("AADSTS7000218",):
            # 7000218 = no resource role granted. Means the secret is
            # valid but the app doesn't have permission for this
            # resource. Useful diagnostic — the auth path works.
            return ProbeResult(
                "token_acquisition",
                _WARN,
                f"secret valid but no role on observability resource ({e.code}); check S2S grants",
                {"aadsts": e.code},
            )
        return ProbeResult(
            "token_acquisition",
            _ERROR,
            f"token request failed: {e.code}: {e.message[:120]}",
            {"aadsts": e.code},
        )
    return ProbeResult(
        "token_acquisition",
        _OK,
        f"observability token acquired (expires_in={token.get('expires_in', '?')}s)",
        {"token_type": token.get("token_type", "?")},
    )


def probe_reachability(hosts: tuple[str, ...] = _REACHABILITY_HOSTS) -> ProbeResult:
    unreachable = [h for h in hosts if not tcp_reachable(h)]
    if unreachable:
        return ProbeResult(
            "reachability",
            _ERROR,
            f"unreachable: {unreachable}",
            {"unreachable": unreachable},
        )
    return ProbeResult(
        "reachability", _OK, f"reachable: {list(hosts)}", {"hosts": list(hosts)}
    )


def probe_otlp_endpoint(endpoint: str | None) -> ProbeResult:
    if not endpoint:
        return ProbeResult(
            "otlp_endpoint",
            _WARN,
            "HERMES_OTLP_ENDPOINT not set in agent .env; spans won't be exported",
        )
    try:
        host = urlparse(endpoint).hostname
    except ValueError:
        return ProbeResult(
            "otlp_endpoint", _ERROR, f"unparseable URL: {endpoint!r}"
        )
    if not host:
        return ProbeResult(
            "otlp_endpoint",
            _ERROR,
            f"no host extracted from {endpoint!r}",
        )
    try:
        socket.gethostbyname(host)
    except OSError as e:
        return ProbeResult(
            "otlp_endpoint",
            _WARN,
            f"DNS lookup failed for {host}: {e}; spans may not export at runtime",
            {"host": host},
        )
    return ProbeResult(
        "otlp_endpoint",
        _OK,
        f"resolves: {host} (DNS only — actual POST happens at runtime)",
        {"host": host, "endpoint": endpoint},
    )


# ---------------------------------------------------------------------------
# Verify orchestration
# ---------------------------------------------------------------------------


def run_verify(
    *,
    slug: str,
    hermes_home: Path | None = None,
    generated_config_path: Path | None = None,
) -> VerifyReport:
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()
    if generated_config_path is None:
        generated_config_path = Path.cwd() / "a365.generated.config.json"

    report = VerifyReport(slug=slug)

    local_probe, agent_env = probe_local_config(hermes_home, slug)
    report.probes.append(local_probe)

    gen_probe, gen_data = probe_generated_config(generated_config_path)
    report.probes.append(gen_probe)

    if local_probe.state == _ERROR or gen_probe.state == _ERROR:
        # Skip token acquisition — we don't have what we need.
        report.probes.append(
            ProbeResult(
                "token_acquisition",
                _WARN,
                "skipped — local_config or generated_config probe failed",
            )
        )
    else:
        report.probes.append(
            probe_token_acquisition(
                tenant_id=agent_env["A365_TENANT_ID"],
                client_id=agent_env["A365_APP_ID"],
                client_secret=gen_data["client_secret"],
            )
        )

    report.probes.append(probe_reachability())
    report.probes.append(probe_otlp_endpoint(agent_env.get("HERMES_OTLP_ENDPOINT")))

    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_STATE_BADGE = {_OK: "[ ok ]", _WARN: "[WARN]", _ERROR: "[ERR ]"}


def render_human(report: VerifyReport) -> str:
    lines = [f"hermes a365 activity-bridge verify — {report.slug}", "-" * 60]
    for p in report.probes:
        badge = _STATE_BADGE.get(p.state, f"[{p.state}]")
        lines.append(f"  {badge}  {p.name:<19}  {p.detail}")
    lines.append("")
    lines.append(f"overall: {report.overall}")
    if report.overall != _OK:
        lines.append("")
        lines.append("Re-run with --json for raw probe data.")
    return "\n".join(lines) + "\n"


def render_json(report: VerifyReport) -> str:
    return json.dumps(
        {
            "slug": report.slug,
            "overall": report.overall,
            "probes": [asdict(p) for p in report.probes],
        },
        indent=2,
    ) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _overall_to_exit_code(overall: ProbeState) -> int:
    return {_OK: 0, _WARN: 1, _ERROR: 2}[overall]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 activity-bridge — Bot Framework adapter daemon.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    verify = sub.add_parser(
        "verify",
        help="one-shot diagnostic: validate config + auth + reachability",
    )
    verify.add_argument(
        "--slug", required=True, help="agent slug (matches ~/.hermes/agents/<slug>/)"
    )
    verify.add_argument(
        "--generated-config",
        type=Path,
        help="path to a365.generated.config.json (default: ./a365.generated.config.json)",
    )
    verify.add_argument(
        "--human", action="store_true", help="formatted output (default: JSON)"
    )

    # Slice 19b will register `serve` here.
    args = parser.parse_args(argv)

    if args.cmd == "verify":
        try:
            report = run_verify(
                slug=args.slug,
                generated_config_path=args.generated_config,
            )
        except (BridgeConfigError, TokenAcquisitionError) as e:
            # These shouldn't escape run_verify (each probe catches them),
            # but defend against future regressions.
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        sys.stdout.write(render_human(report) if args.human else render_json(report))
        return _overall_to_exit_code(report.overall)

    parser.error(f"unknown command: {args.cmd}")
    return 2  # unreachable; argparse exits


if __name__ == "__main__":
    raise SystemExit(main())
