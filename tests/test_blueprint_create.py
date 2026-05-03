"""Tests for scripts/blueprint_create.py.

Every test uses an in-memory FakeMutator + FakeQuerySource and a tmp_path-
based ``HERMES_HOME``; nothing here ever calls the real ``a365`` CLI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from blueprint_create import (
    BlueprintCreateError,
    BlueprintCreateResult,
    PlanContext,
    apply_blueprint_plan,
    build_blueprint_plan,
    render_plan_human,
    write_json_atomic,
)
from register import AADSTSError
from render_blueprint import BlueprintInputs

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeQuerySource:
    """Minimal QuerySource — only ``query_blueprint`` matters for this command."""

    available: bool = True
    blueprints: dict[str, dict[str, Any]] = field(default_factory=dict)

    def query_license(self) -> dict[str, Any] | None:
        return None

    def query_app_by_id(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_app_by_name(self, *, name: str) -> dict[str, Any] | None:
        return None

    def query_consent(self, *, app_id: str) -> dict[str, Any] | None:
        return None

    def query_blueprint(self, *, slug: str) -> dict[str, Any] | None:
        return self.blueprints.get(slug)

    def query_instance(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_telemetry(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_fic(self, *, app_id: str) -> dict[str, Any] | None:
        return None


@dataclass
class FakeMutator:
    """Records calls; returns scripted setup_blueprint responses or raises."""

    available: bool = True
    setup_blueprint_response: dict[str, Any] = field(
        default_factory=lambda: {"blueprintId": "bp-7c1d"}
    )
    setup_blueprint_error: Exception | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    # Stubs for the rest of the Mutator protocol — never called by blueprint_create.
    def setup_app(self, *, tier: int, name: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def fic_configure(self, *, app_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def setup_blueprint(self, *, file_path: Path) -> dict[str, Any]:
        self.calls.append(("setup_blueprint", {"file_path": file_path}))
        if self.setup_blueprint_error is not None:
            err, self.setup_blueprint_error = self.setup_blueprint_error, None
            raise err
        return self.setup_blueprint_response


# ---------------------------------------------------------------------------
# Inputs helper
# ---------------------------------------------------------------------------


def _inputs(**overrides: Any) -> BlueprintInputs:
    base = {
        "slug": "inbox-helper",
        "description": "Summarises unread mail",
        "purpose": "productivity",
        "workiq_tools": ["mail", "calendar"],
    }
    base.update(overrides)
    return BlueprintInputs(**base)


# ---------------------------------------------------------------------------
# write_json_atomic
# ---------------------------------------------------------------------------


class TestWriteJsonAtomic:
    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deep" / "blueprint.json"
        write_json_atomic(target, {"foo": "bar"})
        assert target.exists()
        assert json.loads(target.read_text()) == {"foo": "bar"}

    def test_no_partial_write_artifact(self, tmp_path: Path) -> None:
        target = tmp_path / "blueprint.json"
        write_json_atomic(target, {"a": 1})
        assert not (tmp_path / "blueprint.json.tmp").exists()

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "blueprint.json"
        write_json_atomic(target, {"v": 1})
        write_json_atomic(target, {"v": 2})
        assert json.loads(target.read_text()) == {"v": 2}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class TestBuildBlueprintPlan:
    def test_create_when_no_actual(self) -> None:
        ctx = build_blueprint_plan(_inputs(), query_source=FakeQuerySource())
        assert isinstance(ctx, PlanContext)
        assert ctx.plan.action == "create"
        assert ctx.rendered["agentIdentity"]["slug"] == "inbox-helper"
        assert ctx.rendered["workIqTools"] == ["mail", "calendar"]
        assert ctx.existing_blueprint_id is None

    def test_noop_when_actual_matches_rendered(self) -> None:
        # Render the desired then plant it as the actual (with a server id).
        rendered = build_blueprint_plan(_inputs(), query_source=FakeQuerySource()).rendered
        actual_with_id = {**rendered, "blueprintId": "bp-existing"}
        qs = FakeQuerySource(blueprints={"inbox-helper": actual_with_id})
        ctx = build_blueprint_plan(_inputs(), query_source=qs)
        assert ctx.plan.action == "noop"
        # Server-assigned id is captured even though it's stripped from the diff.
        assert ctx.existing_blueprint_id == "bp-existing"

    def test_patch_when_dlp_differs(self) -> None:
        rendered = build_blueprint_plan(_inputs(), query_source=FakeQuerySource()).rendered
        actual = {**rendered, "blueprintId": "bp-old"}
        actual["policies"] = {**rendered["policies"], "dlp": "default-strict"}
        qs = FakeQuerySource(blueprints={"inbox-helper": actual})
        ctx = build_blueprint_plan(_inputs(), query_source=qs)
        assert ctx.plan.action == "patch"
        assert "policies/dlp" in ctx.plan.diff
        assert ctx.existing_blueprint_id == "bp-old"

    def test_server_fields_do_not_perturb_noop(self) -> None:
        # Server-assigned timestamps shouldn't trigger a phantom patch.
        rendered = build_blueprint_plan(_inputs(), query_source=FakeQuerySource()).rendered
        actual = {**rendered, "blueprintId": "x", "lastPatched": "2026-05-02", "etag": "W/abc"}
        qs = FakeQuerySource(blueprints={"inbox-helper": actual})
        ctx = build_blueprint_plan(_inputs(), query_source=qs)
        assert ctx.plan.action == "noop"

    def test_abort_on_slug_mismatch(self) -> None:
        bogus_actual = {
            "agentIdentity": {"slug": "different"},
            "displayName": "different",
        }
        qs = FakeQuerySource(blueprints={"inbox-helper": bogus_actual})
        ctx = build_blueprint_plan(_inputs(), query_source=qs)
        assert ctx.plan.action == "abort"

    def test_unavailable_query_source_assumes_create(self) -> None:
        ctx = build_blueprint_plan(_inputs(), query_source=FakeQuerySource(available=False))
        assert ctx.plan.action == "create"


# ---------------------------------------------------------------------------
# Plan rendering
# ---------------------------------------------------------------------------


class TestRenderPlanHuman:
    def test_create_renders_expected_fields(self, tmp_path: Path) -> None:
        ctx = build_blueprint_plan(_inputs(), query_source=FakeQuerySource())
        rendered_path = tmp_path / "inbox-helper.blueprint.json"
        write_json_atomic(rendered_path, ctx.rendered)
        text = render_plan_human(_inputs(), ctx.plan, rendered_path)
        assert "[plan] blueprint inbox-helper" in text
        assert "rendered →" in text
        assert "actual:  not registered" in text
        assert "delta:   create" in text
        assert "DLP policy:" in text
        assert "Work IQ tools requested:  mail, calendar" in text

    def test_patch_includes_diff_block(self, tmp_path: Path) -> None:
        rendered = build_blueprint_plan(_inputs(), query_source=FakeQuerySource()).rendered
        actual = {**rendered, "policies": {**rendered["policies"], "dlp": "default-strict"}}
        qs = FakeQuerySource(blueprints={"inbox-helper": actual})
        ctx = build_blueprint_plan(_inputs(), query_source=qs)
        rendered_path = tmp_path / "x.json"
        write_json_atomic(rendered_path, ctx.rendered)
        text = render_plan_human(_inputs(), ctx.plan, rendered_path)
        assert "Diff (actual → desired):" in text
        assert "policies/dlp" in text


# ---------------------------------------------------------------------------
# apply_blueprint_plan
# ---------------------------------------------------------------------------


class TestApplyCreate:
    def test_create_calls_mutator_and_writes_cache(self, tmp_path: Path) -> None:
        ctx = build_blueprint_plan(_inputs(), query_source=FakeQuerySource())
        mutator = FakeMutator(setup_blueprint_response={"blueprintId": "bp-new"})
        rendered_path = tmp_path / "tmp.blueprint.json"
        result = apply_blueprint_plan(
            _inputs(),
            ctx,
            mutator=mutator,
            hermes_home=tmp_path,
            rendered_path=rendered_path,
        )
        assert isinstance(result, BlueprintCreateResult)
        assert result.blueprint_id == "bp-new"
        assert result.mutated is True
        # Mutator received the tmp file path.
        assert mutator.calls == [("setup_blueprint", {"file_path": rendered_path})]
        # Tmp file was written before the call.
        assert rendered_path.exists()
        assert json.loads(rendered_path.read_text())["agentIdentity"]["slug"] == "inbox-helper"
        # Cache was written under the agent home.
        cache = tmp_path / "agents" / "inbox-helper" / "blueprint.json"
        assert cache.exists()
        assert json.loads(cache.read_text()) == ctx.rendered

    def test_create_missing_blueprintid_raises(self, tmp_path: Path) -> None:
        ctx = build_blueprint_plan(_inputs(), query_source=FakeQuerySource())
        mutator = FakeMutator(setup_blueprint_response={})
        with pytest.raises(BlueprintCreateError, match="no blueprintId"):
            apply_blueprint_plan(
                _inputs(),
                ctx,
                mutator=mutator,
                hermes_home=tmp_path,
            )


class TestApplyNoop:
    def test_noop_does_not_call_mutator_but_refreshes_cache(self, tmp_path: Path) -> None:
        rendered = build_blueprint_plan(_inputs(), query_source=FakeQuerySource()).rendered
        actual_with_id = {**rendered, "blueprintId": "bp-existing"}
        qs = FakeQuerySource(blueprints={"inbox-helper": actual_with_id})
        ctx = build_blueprint_plan(_inputs(), query_source=qs)
        assert ctx.plan.action == "noop"

        mutator = FakeMutator()
        result = apply_blueprint_plan(_inputs(), ctx, mutator=mutator, hermes_home=tmp_path)
        assert mutator.calls == []
        assert result.mutated is False
        assert result.blueprint_id == "bp-existing"
        cache = tmp_path / "agents" / "inbox-helper" / "blueprint.json"
        assert cache.exists()


class TestApplyPatch:
    def test_patch_invokes_mutator_with_diff_count(self, tmp_path: Path) -> None:
        rendered = build_blueprint_plan(_inputs(), query_source=FakeQuerySource()).rendered
        actual = {
            **rendered,
            "blueprintId": "bp-old",
            "policies": {**rendered["policies"], "dlp": "default-strict"},
        }
        qs = FakeQuerySource(blueprints={"inbox-helper": actual})
        ctx = build_blueprint_plan(_inputs(), query_source=qs)
        assert ctx.plan.action == "patch"

        mutator = FakeMutator(setup_blueprint_response={"blueprintId": "bp-old"})
        result = apply_blueprint_plan(_inputs(), ctx, mutator=mutator, hermes_home=tmp_path)
        assert result.mutated is True
        assert result.blueprint_id == "bp-old"
        assert any("PATCH" in m for m in result.messages)
        assert mutator.calls and mutator.calls[0][0] == "setup_blueprint"


class TestApplyAbort:
    def test_abort_raises_before_calling_mutator(self, tmp_path: Path) -> None:
        bogus_actual = {
            "agentIdentity": {"slug": "different"},
            "displayName": "different",
        }
        qs = FakeQuerySource(blueprints={"inbox-helper": bogus_actual})
        ctx = build_blueprint_plan(_inputs(), query_source=qs)
        assert ctx.plan.action == "abort"

        mutator = FakeMutator()
        with pytest.raises(BlueprintCreateError, match="refusing to apply"):
            apply_blueprint_plan(_inputs(), ctx, mutator=mutator, hermes_home=tmp_path)
        assert mutator.calls == []
        # And no cache file should have been left behind.
        assert not (tmp_path / "agents" / "inbox-helper" / "blueprint.json").exists()


class TestApplyAADSTSPropagation:
    def test_aadsts_error_propagates(self, tmp_path: Path) -> None:
        ctx = build_blueprint_plan(_inputs(), query_source=FakeQuerySource())
        mutator = FakeMutator(setup_blueprint_error=AADSTSError("AADSTS50034", "user not found"))
        with pytest.raises(AADSTSError) as excinfo:
            apply_blueprint_plan(_inputs(), ctx, mutator=mutator, hermes_home=tmp_path)
        assert excinfo.value.code == "AADSTS50034"
        # Cache must NOT be written when the mutator failed.
        assert not (tmp_path / "agents" / "inbox-helper" / "blueprint.json").exists()
