"""hermes a365 telemetry — verify OTLP plumbing for an A365 agent.

Spec: SPEC.md §6.8. Read-only diagnostic; never mutates. Three checks:

1. Per-agent ``.env`` has ``HERMES_OTLP_ENDPOINT`` set.
2. ``AA_INSTANCE_ID`` is recorded.
3. ``a365 query-entra --telemetry --instance=<id>`` surfaces span data.

Span injection from Hermes itself is the activity bridge's job (§6.7); this
command only verifies that the cloud has seen spans for the instance and
that the local config is ready to emit more.

Output: JSON to stdout by default, ``--human`` for a markdown-style table.
Exit codes mirror ``hermes a365 status``:

- ``0`` — all checks ``ok``
- ``1`` — at least one ``warn`` (e.g. no spans yet)
- ``2`` — at least one ``error`` (missing config, unreachable)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from _common import parse_env
from status import QuerySource, get_query_source

CheckState = Literal["ok", "warn", "error", "skipped"]
OverallState = Literal["ok", "partial", "broken"]

_OK = "ok"
_WARN = "warn"
_ERROR = "error"
_SKIPPED = "skipped"

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TelemetryError(RuntimeError):
    """Raised when telemetry can't proceed (e.g. missing agent .env)."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TelemetryCheck:
    name: str
    state: CheckState
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TelemetryReport:
    slug: str
    aa_instance_id: str | None
    otlp_endpoint: str | None
    checks: list[TelemetryCheck]

    @property
    def overall(self) -> OverallState:
        if any(c.state == _ERROR for c in self.checks):
            return "broken"
        if any(c.state == _WARN for c in self.checks):
            return "partial"
        return "ok"


def overall_to_exit_code(o: OverallState) -> int:
    return {"ok": 0, "partial": 1, "broken": 2}[o]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def _agent_env_path(hermes_home: Path, slug: str) -> Path:
    return hermes_home / "agents" / slug / ".env"


# ---------------------------------------------------------------------------
# Gather
# ---------------------------------------------------------------------------


def collect_telemetry(
    slug: str,
    *,
    hermes_home: Path | None = None,
    query_source: QuerySource | None = None,
) -> TelemetryReport:
    """Build the telemetry report for a single agent."""
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()
    qs = query_source or get_query_source()

    env_path = _agent_env_path(hermes_home, slug)
    if not env_path.exists():
        raise TelemetryError(
            f"{env_path} does not exist; run `hermes a365 instance create {slug}` first"
        )

    env = parse_env(env_path.read_text())
    aa_instance_id = env.get("AA_INSTANCE_ID", "").strip() or None
    otlp_endpoint = env.get("HERMES_OTLP_ENDPOINT", "").strip() or None

    checks: list[TelemetryCheck] = []

    # 1. OTLP endpoint configured?
    if otlp_endpoint:
        checks.append(
            TelemetryCheck(
                name="otlp_endpoint",
                state=_OK,
                detail=otlp_endpoint,
                data={"endpoint": otlp_endpoint},
            )
        )
    else:
        checks.append(
            TelemetryCheck(
                name="otlp_endpoint",
                state=_ERROR,
                detail="HERMES_OTLP_ENDPOINT not set in agent .env",
            )
        )

    # 2. Instance id recorded?
    if aa_instance_id:
        checks.append(
            TelemetryCheck(
                name="aa_instance_id",
                state=_OK,
                detail=aa_instance_id,
                data={"aa_instance_id": aa_instance_id},
            )
        )
    else:
        checks.append(
            TelemetryCheck(
                name="aa_instance_id",
                state=_ERROR,
                detail="AA_INSTANCE_ID not set in agent .env",
            )
        )

    # 3. Cloud span verification — only if we have an instance id and a CLI.
    if not aa_instance_id:
        checks.append(
            TelemetryCheck(
                name="last_span",
                state=_SKIPPED,
                detail="no AA_INSTANCE_ID; cannot query telemetry",
            )
        )
    elif not qs.available:
        checks.append(
            TelemetryCheck(
                name="last_span",
                state=_SKIPPED,
                detail="a365 CLI unavailable; install to query telemetry",
            )
        )
    else:
        payload = qs.query_telemetry(instance_id=aa_instance_id)
        checks.append(_check_last_span(payload))

    return TelemetryReport(
        slug=slug,
        aa_instance_id=aa_instance_id,
        otlp_endpoint=otlp_endpoint,
        checks=checks,
    )


def _check_last_span(payload: dict[str, Any] | None) -> TelemetryCheck:
    """Build the ``last_span`` check from the telemetry query result."""
    if payload is None:
        return TelemetryCheck(
            name="last_span",
            state=_WARN,
            detail="no telemetry data available",
        )
    last_span = payload.get("last_span") or payload.get("lastSpan")
    sampler = payload.get("sampler") or ""
    if not last_span:
        detail = f"no spans seen yet (sampler={sampler or 'unknown'})"
        return TelemetryCheck(name="last_span", state=_WARN, detail=detail, data=payload)
    detail = f"last span {last_span}"
    if sampler:
        detail += f", sampler={sampler}"
    return TelemetryCheck(name="last_span", state=_OK, detail=detail, data=payload)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_human(report: TelemetryReport) -> str:
    name_w = max((len(c.name) for c in report.checks), default=len("Check"))
    state_w = max((len(c.state) for c in report.checks), default=len("State"))
    name_w = max(name_w, len("Check"))
    state_w = max(state_w, len("State"))

    lines: list[str] = [f"hermes a365 telemetry — {report.slug}"]
    lines.append("-" * 60)
    lines.append(f"{'Check':<{name_w}}  {'State':<{state_w}}  Detail")
    lines.append(f"{'-' * name_w}  {'-' * state_w}  ------")
    for c in report.checks:
        lines.append(f"{c.name:<{name_w}}  {c.state:<{state_w}}  {c.detail}")
    lines.append("")
    lines.append(f"overall: {report.overall}")
    return "\n".join(lines) + "\n"


def render_json(report: TelemetryReport) -> str:
    payload = {
        "slug": report.slug,
        "aa_instance_id": report.aa_instance_id,
        "otlp_endpoint": report.otlp_endpoint,
        "overall": report.overall,
        "checks": [asdict(c) for c in report.checks],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 telemetry — verify OTLP / span pipeline for an agent.",
    )
    parser.add_argument("slug", help="agent slug")
    parser.add_argument("--human", action="store_true", help="markdown-aligned table output")
    args = parser.parse_args(argv)

    try:
        report = collect_telemetry(args.slug)
    except TelemetryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(render_human(report) if args.human else render_json(report))
    return overall_to_exit_code(report.overall)


if __name__ == "__main__":
    raise SystemExit(main())
