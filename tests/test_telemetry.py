"""Tests for scripts/telemetry.py."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from telemetry import (
    TelemetryError,
    collect_telemetry,
    overall_to_exit_code,
    render_human,
    render_json,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeQuerySource:
    available: bool = True
    telemetry: dict[str, dict[str, Any]] = field(default_factory=dict)

    def query_license(self) -> dict[str, Any] | None:
        return None

    def query_app_by_id(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_app_by_name(self, *, name: str) -> dict[str, Any] | None:
        return None

    def query_consent(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_blueprint(self, *, slug: str) -> dict[str, Any] | None:
        return None

    def query_instance(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_telemetry(self, *, instance_id: str) -> dict[str, Any] | None:
        return self.telemetry.get(instance_id)

    def query_fic(self, *, app_id: str) -> dict[str, Any] | None:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_INSTANCE_ID = "550e8400-e29b-41d4-a716-446655440000"
_OTLP = "https://contoso.otel.agent365.microsoft.com"


def _seed_agent_env(
    tmp_path: Path,
    *,
    slug: str = "inbox-helper",
    instance_id: str | None = _INSTANCE_ID,
    otlp_endpoint: str | None = _OTLP,
) -> Path:
    path = tmp_path / "agents" / slug / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if instance_id is not None:
        lines.append(f"AA_INSTANCE_ID={instance_id}")
    if otlp_endpoint is not None:
        lines.append(f"HERMES_OTLP_ENDPOINT={otlp_endpoint}")
    path.write_text("\n".join(lines) + "\n" if lines else "")
    return path


def _by_name(checks: list, name: str):  # type: ignore[no-untyped-def]
    return next(c for c in checks if c.name == name)


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


class TestPreconditions:
    def test_missing_agent_env_fails_clean(self, tmp_path: Path) -> None:
        with pytest.raises(TelemetryError, match="instance create"):
            collect_telemetry("inbox-helper", hermes_home=tmp_path, query_source=FakeQuerySource())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_all_ok_when_endpoint_id_and_span_present(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(
            telemetry={_INSTANCE_ID: {"last_span": "2026-05-03T14:22Z", "sampler": "parent_based"}}
        )
        report = collect_telemetry("inbox-helper", hermes_home=tmp_path, query_source=qs)
        assert report.overall == "ok"
        assert _by_name(report.checks, "otlp_endpoint").state == "ok"
        assert _by_name(report.checks, "aa_instance_id").state == "ok"
        last_span = _by_name(report.checks, "last_span")
        assert last_span.state == "ok"
        assert "parent_based" in last_span.detail


# ---------------------------------------------------------------------------
# Warning paths
# ---------------------------------------------------------------------------


class TestWarnings:
    def test_no_spans_yet_is_warn(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(telemetry={_INSTANCE_ID: {"sampler": "parent_based"}})
        report = collect_telemetry("inbox-helper", hermes_home=tmp_path, query_source=qs)
        assert report.overall == "partial"
        last_span = _by_name(report.checks, "last_span")
        assert last_span.state == "warn"
        assert "no spans seen yet" in last_span.detail

    def test_no_telemetry_payload_at_all_is_warn(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        # Empty telemetry dict → query returns None for our instance.
        qs = FakeQuerySource()
        report = collect_telemetry("inbox-helper", hermes_home=tmp_path, query_source=qs)
        assert report.overall == "partial"
        assert _by_name(report.checks, "last_span").state == "warn"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrors:
    def test_missing_otlp_endpoint_is_error(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path, otlp_endpoint=None)
        report = collect_telemetry(
            "inbox-helper", hermes_home=tmp_path, query_source=FakeQuerySource()
        )
        assert report.overall == "broken"
        assert _by_name(report.checks, "otlp_endpoint").state == "error"

    def test_missing_aa_instance_id_is_error(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path, instance_id=None)
        report = collect_telemetry(
            "inbox-helper", hermes_home=tmp_path, query_source=FakeQuerySource()
        )
        assert _by_name(report.checks, "aa_instance_id").state == "error"
        # Without an instance id, the span check is skipped (not error).
        assert _by_name(report.checks, "last_span").state == "skipped"


# ---------------------------------------------------------------------------
# Skipped paths
# ---------------------------------------------------------------------------


class TestSkipped:
    def test_unavailable_query_source_skips_last_span(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        report = collect_telemetry(
            "inbox-helper",
            hermes_home=tmp_path,
            query_source=FakeQuerySource(available=False),
        )
        # Local checks are still ok; cloud check is skipped → overall ok.
        assert report.overall == "ok"
        assert _by_name(report.checks, "last_span").state == "skipped"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_human_includes_overall(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(
            telemetry={_INSTANCE_ID: {"last_span": "2026-05-03T14:22Z", "sampler": "x"}}
        )
        report = collect_telemetry("inbox-helper", hermes_home=tmp_path, query_source=qs)
        text = render_human(report)
        assert "hermes a365 telemetry — inbox-helper" in text
        assert "overall: ok" in text

    def test_json_round_trip(self, tmp_path: Path) -> None:
        _seed_agent_env(tmp_path)
        qs = FakeQuerySource(telemetry={_INSTANCE_ID: {"last_span": "T", "sampler": "x"}})
        report = collect_telemetry("inbox-helper", hermes_home=tmp_path, query_source=qs)
        payload = json.loads(render_json(report))
        assert payload["slug"] == "inbox-helper"
        assert payload["aa_instance_id"] == _INSTANCE_ID
        assert payload["otlp_endpoint"] == _OTLP
        assert payload["overall"] == "ok"
        names = [c["name"] for c in payload["checks"]]
        assert names == ["otlp_endpoint", "aa_instance_id", "last_span"]


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


class TestExitCodes:
    @pytest.mark.parametrize(
        "overall,code",
        [("ok", 0), ("partial", 1), ("broken", 2)],
    )
    def test_mapping(self, overall: str, code: int) -> None:
        assert overall_to_exit_code(overall) == code  # type: ignore[arg-type]
