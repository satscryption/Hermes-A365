"""hermes a365 doctor — read-only environment probe.

Spec: SPEC.md §6.12. Pure diagnostic; never mutates. Emits structured JSON to
stdout by default; pass ``--human`` for a coloured-marker rendering.

Probes (in order):
1. ``a365`` CLI — present, variant (``atk-npm`` vs ``a365-dotnet``), version
2. ``az`` CLI — present, ``az account show`` succeeds
3. Network reachability — ``login.microsoftonline.com``, ``graph.microsoft.com``,
   and the tenant-specific A365 host if a tenant id is recorded locally
4. OS keychain backend — macOS Security framework or Linux ``secret-tool``
5. Local config — ``~/.hermes/.env`` and ``~/.hermes/config.yaml`` parseable
6. Hermes harness — ``hermes --version`` responds

Exit codes (matching ``hermes a365 status``):
- 0 — all probes ``ok``
- 1 — at least one ``warn``, no ``error``
- 2 — at least one ``error``
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from _common import parse_env, safe_run, tcp_reachable

ProbeState = Literal["ok", "warn", "error"]

_OK = "ok"
_WARN = "warn"
_ERROR = "error"

_VARIANT_NPM = "atk-npm"
_VARIANT_DOTNET = "a365-dotnet"
_VARIANT_UNKNOWN = "unknown"

_DEFAULT_NETWORK_HOSTS = ("login.microsoftonline.com", "graph.microsoft.com")
_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"


@dataclass
class ProbeResult:
    name: str
    state: ProbeState
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Variant detection (§10 Q7)
# ---------------------------------------------------------------------------


def detect_a365_variant(binary_path: str | None, version_output: str | None) -> str:
    """Detect whether the resolved ``a365`` is the npm ``atk`` or .NET variant.

    Layered heuristic:
    1. ``A365_CLI_VARIANT`` env var override (operator opt-out for any host)
    2. ``--version`` output signature (most reliable when both ship)
    3. Resolved binary path (last-ditch — varies by package manager)
    4. ``unknown``
    """
    forced = os.environ.get("A365_CLI_VARIANT")
    if forced in (_VARIANT_NPM, _VARIANT_DOTNET):
        return forced

    if version_output:
        lower = version_output.lower()
        if "atk" in lower or "node" in lower or "npm" in lower:
            return _VARIANT_NPM
        if "dotnet" in lower or ".net" in lower:
            return _VARIANT_DOTNET

    if binary_path:
        path = binary_path.lower()
        if "node_modules" in path or "/.npm/" in path or "npm/" in path:
            return _VARIANT_NPM
        if "dotnet" in path or "/.dotnet/" in path:
            return _VARIANT_DOTNET

    return _VARIANT_UNKNOWN


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def probe_a365_cli() -> ProbeResult:
    binary = shutil.which("a365")
    if not binary:
        return ProbeResult(
            "a365_cli",
            _ERROR,
            "a365 not found on PATH (install the A365 CLI — atk npm or .NET variant)",
        )
    version = safe_run(["a365", "--version"]) or "unknown"
    variant = detect_a365_variant(binary, version)
    state: ProbeState = _OK if variant != _VARIANT_UNKNOWN else _WARN
    return ProbeResult(
        "a365_cli",
        state,
        f"{variant} {version} at {binary}",
        {"binary": binary, "variant": variant, "version": version},
    )


def probe_az_cli() -> ProbeResult:
    binary = shutil.which("az")
    if not binary:
        return ProbeResult(
            "az_cli",
            _WARN,
            "az not found on PATH (recommended for Entra reads beyond a365 query-entra)",
        )
    account = safe_run(["az", "account", "show"], timeout=10.0)
    if account:
        # Extract tenant + name from JSON output without requiring json parse.
        # The actual output is JSON; we just confirm non-empty success here.
        return ProbeResult(
            "az_cli",
            _OK,
            f"present and signed in ({binary})",
            {"binary": binary, "signed_in": True},
        )
    return ProbeResult(
        "az_cli",
        _WARN,
        f"present but not signed in (run `az login`) at {binary}",
        {"binary": binary, "signed_in": False},
    )


def probe_network(*, tenant_hint: str | None = None) -> ProbeResult:
    """Probe TCP reachability of the public A365 / Graph endpoints.

    ``tenant_hint`` is the tenant id or domain (e.g. ``contoso.onmicrosoft.com``)
    used to construct the per-tenant A365 host; if absent, only the global hosts
    are probed.
    """
    hosts = list(_DEFAULT_NETWORK_HOSTS)
    if tenant_hint:
        prefix = tenant_hint.split(".", 1)[0]
        if prefix:
            hosts.append(f"{prefix}.api.agent365.microsoft.com")

    unreachable = [h for h in hosts if not tcp_reachable(h)]
    if not unreachable:
        return ProbeResult(
            "network",
            _OK,
            f"{len(hosts)} hosts reachable",
            {"hosts": hosts, "unreachable": []},
        )
    if len(unreachable) == len(hosts):
        return ProbeResult(
            "network",
            _ERROR,
            f"none of {len(hosts)} hosts reachable: {unreachable}",
            {"hosts": hosts, "unreachable": unreachable},
        )
    return ProbeResult(
        "network",
        _WARN,
        f"{len(unreachable)}/{len(hosts)} unreachable: {unreachable}",
        {"hosts": hosts, "unreachable": unreachable},
    )


def probe_keychain() -> ProbeResult:
    if sys.platform == "darwin":
        result = safe_run(["security", "list-keychains"], timeout=3.0)
        if result:
            return ProbeResult(
                "keychain",
                _OK,
                "macOS Security framework available",
                {"backend": "macos-security"},
            )
        return ProbeResult(
            "keychain",
            _ERROR,
            "macOS `security` command not responding",
            {"backend": "macos-security"},
        )
    if sys.platform.startswith("linux"):
        binary = shutil.which("secret-tool")
        if not binary:
            return ProbeResult(
                "keychain",
                _ERROR,
                "secret-tool not found (install libsecret-tools / libsecret-1-0)",
                {"backend": "libsecret"},
            )
        return ProbeResult(
            "keychain",
            _OK,
            f"libsecret available at {binary}",
            {"backend": "libsecret", "binary": binary},
        )
    return ProbeResult(
        "keychain",
        _WARN,
        f"unsupported platform: {sys.platform} (v0.1 supports macOS + Linux only)",
        {"backend": None},
    )


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def probe_local_config() -> ProbeResult:
    home = _resolve_hermes_home()
    if not home.exists():
        return ProbeResult(
            "local_config",
            _WARN,
            f"{home} does not exist (skill not yet bootstrapped)",
            {"hermes_home": str(home), "bootstrapped": False},
        )

    issues: list[str] = []
    env_keys: list[str] = []
    tenant_hint: str | None = None
    config_present = False

    env_file = home / ".env"
    if env_file.exists():
        try:
            parsed = parse_env(env_file.read_text())
            env_keys = sorted(parsed.keys())
            tenant_hint = parsed.get("A365_TENANT_ID")
        except OSError as e:
            issues.append(f".env unreadable: {e}")

    config_file = home / "config.yaml"
    if config_file.exists():
        # We don't import a yaml parser as a hard dep; presence + readability
        # is enough for v0.1.
        try:
            config_file.read_text()
            config_present = True
        except OSError as e:
            issues.append(f"config.yaml unreadable: {e}")

    if issues:
        return ProbeResult(
            "local_config",
            _ERROR,
            "; ".join(issues),
            {"hermes_home": str(home)},
        )

    detail_parts = [str(home)]
    if env_keys:
        detail_parts.append(f".env: {len(env_keys)} keys")
    else:
        detail_parts.append(".env: absent or empty")
    detail_parts.append(f"config.yaml: {'present' if config_present else 'absent'}")
    return ProbeResult(
        "local_config",
        _OK,
        " | ".join(detail_parts),
        {
            "hermes_home": str(home),
            "env_keys": env_keys,
            "config_yaml_present": config_present,
            "tenant_hint": tenant_hint,
        },
    )


def probe_hermes_harness() -> ProbeResult:
    binary = shutil.which("hermes")
    if not binary:
        return ProbeResult(
            "hermes_harness",
            _WARN,
            "`hermes` not on PATH (skill may be invoked through a different entry point)",
            {"binary": None},
        )
    version = safe_run(["hermes", "--version"], timeout=5.0)
    if not version:
        return ProbeResult(
            "hermes_harness",
            _WARN,
            f"`hermes --version` did not respond at {binary}",
            {"binary": binary, "version": None},
        )
    # Some `--version` implementations print multi-line preambles; the first line
    # is reliably the version banner. Keep the full output in `data` for callers
    # that want it.
    headline = version.splitlines()[0].strip()
    return ProbeResult(
        "hermes_harness",
        _OK,
        f"{headline} ({binary})",
        {"binary": binary, "version": version, "headline": headline},
    )


# ---------------------------------------------------------------------------
# Aggregation + rendering
# ---------------------------------------------------------------------------


def collect_probes(*, skip_network: bool = False) -> list[ProbeResult]:
    """Run every probe and return the results in display order."""
    results: list[ProbeResult] = []
    results.append(probe_a365_cli())
    results.append(probe_az_cli())
    config_result = probe_local_config()
    tenant_hint = config_result.data.get("tenant_hint")
    if not skip_network:
        results.append(probe_network(tenant_hint=tenant_hint))
    results.append(probe_keychain())
    results.append(config_result)
    results.append(probe_hermes_harness())
    return results


def aggregate_state(probes: list[ProbeResult]) -> tuple[ProbeState, int]:
    """Collapse probe results into an overall state + exit code."""
    if any(p.state == _ERROR for p in probes):
        return _ERROR, 2
    if any(p.state == _WARN for p in probes):
        return _WARN, 1
    return _OK, 0


def render_json(probes: list[ProbeResult]) -> str:
    overall, _ = aggregate_state(probes)
    payload = {
        "overall": overall,
        "probes": [asdict(p) for p in probes],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_human(probes: list[ProbeResult]) -> str:
    """Render a compact, ASCII-only operator-friendly report."""
    markers = {_OK: "[ ok ]", _WARN: "[warn]", _ERROR: "[FAIL]"}
    lines = ["hermes a365 doctor", "-" * 30]
    for p in probes:
        lines.append(f"  {markers[p.state]}  {p.name:18s}  {p.detail}")
    overall, _ = aggregate_state(probes)
    lines.extend(["", f"overall: {overall}"])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 doctor — read-only environment probe."
    )
    parser.add_argument("--human", action="store_true", help="human-readable rendering")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="skip TCP reachability probes (offline diagnostic)",
    )
    args = parser.parse_args(argv)

    probes = collect_probes(skip_network=args.no_network)
    if args.human:
        sys.stdout.write(render_human(probes))
    else:
        sys.stdout.write(render_json(probes))
    _, code = aggregate_state(probes)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
