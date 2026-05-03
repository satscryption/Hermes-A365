"""Tests for scripts/cleanup.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from cleanup import (
    CleanupError,
    CleanupResult,
    _validate_confirm,
    apply_cleanup_plan,
    build_cleanup_plan,
)
from register import AADSTSError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeQuerySource:
    available: bool = True
    instances: dict[str, dict[str, Any]] = field(default_factory=dict)

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
        return self.instances.get(instance_id)

    def query_telemetry(self, *, instance_id: str) -> dict[str, Any] | None:
        return None

    def query_fic(self, *, app_id: str) -> dict[str, Any] | None:
        return None


@dataclass
class FakeMutator:
    available: bool = True
    cleanup_error: Exception | None = None
    deploy_error: Exception | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def setup_app(self, *, tier: int, name: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def fic_configure(self, *, app_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def fic_rotate(self, *, app_id: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def setup_blueprint(self, *, file_path: Path) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def create_instance(  # pragma: no cover
        self, *, blueprint_slug: str, instance_id: str
    ) -> dict[str, Any]:
        raise NotImplementedError

    def deploy(self, *, instance_id: str, channels: list[str]) -> dict[str, Any]:
        self.calls.append(("deploy", {"instance_id": instance_id, "channels": list(channels)}))
        if self.deploy_error is not None:
            err, self.deploy_error = self.deploy_error, None
            raise err
        return {"channels": {}}

    def cleanup(self, *, kind: str, identifier: str) -> None:
        self.calls.append(("cleanup", {"kind": kind, "identifier": identifier}))
        if self.cleanup_error is not None:
            err, self.cleanup_error = self.cleanup_error, None
            raise err


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_INSTANCE_ID = "550e8400-e29b-41d4-a716-446655440000"


def _seed_agent(
    tmp_path: Path,
    *,
    slug: str = "inbox-helper",
    instance_id: str | None = _INSTANCE_ID,
    blueprint_cache: bool = True,
) -> None:
    agent_dir = tmp_path / "agents" / slug
    agent_dir.mkdir(parents=True, exist_ok=True)
    if instance_id is not None:
        (agent_dir / ".env").write_text(f"AA_INSTANCE_ID={instance_id}\n")
    if blueprint_cache:
        (agent_dir / "blueprint.json").write_text('{"agentIdentity":{"slug":"x"}}\n')


# ---------------------------------------------------------------------------
# --confirm validation
# ---------------------------------------------------------------------------


class TestConfirm:
    def test_missing_confirm_rejected(self) -> None:
        with pytest.raises(CleanupError, match="--confirm is required"):
            _validate_confirm("inbox-helper", None)

    def test_mismatched_confirm_rejected(self) -> None:
        with pytest.raises(CleanupError, match="does not match"):
            _validate_confirm("inbox-helper", "wrong")

    def test_matching_confirm_accepted(self) -> None:
        _validate_confirm("inbox-helper", "inbox-helper")  # no exception


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class TestBuildCleanupPlan:
    def test_full_plan_for_deployed_agent(self, tmp_path: Path) -> None:
        _seed_agent(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {"teams": "ok"}}})
        plan = build_cleanup_plan("inbox-helper", hermes_home=tmp_path, query_source=qs)
        kinds = [s.kind for s in plan.steps]
        # Order matters per spec.
        assert kinds == ["deployment", "instance", "blueprint"]
        # No skip on deployment because channels are bound.
        assert plan.steps[0].skip_reason is None
        # Local paths picked up.
        assert any(p.name == ".env" for p in plan.local_paths)
        assert any(p.name == "blueprint.json" for p in plan.local_paths)

    def test_deployment_skipped_when_no_channels_bound(self, tmp_path: Path) -> None:
        _seed_agent(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {}}})
        plan = build_cleanup_plan("inbox-helper", hermes_home=tmp_path, query_source=qs)
        deployment = next(s for s in plan.steps if s.kind == "deployment")
        assert deployment.skip_reason == "no channels bound"

    def test_no_instance_id_skips_deployment_and_instance_steps(self, tmp_path: Path) -> None:
        # Agent dir exists but no .env (and so no AA_INSTANCE_ID).
        agent_dir = tmp_path / "agents" / "inbox-helper"
        agent_dir.mkdir(parents=True)
        (agent_dir / "blueprint.json").write_text("{}")
        plan = build_cleanup_plan(
            "inbox-helper", hermes_home=tmp_path, query_source=FakeQuerySource()
        )
        kinds = [s.kind for s in plan.steps]
        assert kinds == ["blueprint"]
        # blueprint.json shows up; .env does not.
        assert any(p.name == "blueprint.json" for p in plan.local_paths)
        assert all(p.name != ".env" for p in plan.local_paths)

    def test_unavailable_query_source_treats_no_channels(self, tmp_path: Path) -> None:
        _seed_agent(tmp_path)
        plan = build_cleanup_plan(
            "inbox-helper",
            hermes_home=tmp_path,
            query_source=FakeQuerySource(available=False),
        )
        deployment = next(s for s in plan.steps if s.kind == "deployment")
        assert deployment.skip_reason == "no channels bound"

    def test_no_local_files_when_agent_dir_missing(self, tmp_path: Path) -> None:
        plan = build_cleanup_plan("ghost", hermes_home=tmp_path, query_source=FakeQuerySource())
        assert plan.aa_instance_id is None
        assert plan.local_paths == []


class TestPlanRender:
    def test_human_lists_steps_and_local_paths(self, tmp_path: Path) -> None:
        _seed_agent(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {"teams": "ok"}}})
        plan = build_cleanup_plan("inbox-helper", hermes_home=tmp_path, query_source=qs)
        text = plan.render_human()
        assert "[plan] hermes a365 cleanup inbox-helper" in text
        assert "deployment" in text
        assert "blueprint" in text
        assert "would run" in text
        assert "blueprint.json" in text


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


class TestApplyCleanup:
    def test_full_apply_runs_steps_in_order_and_removes_local(self, tmp_path: Path) -> None:
        _seed_agent(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {"teams": "ok"}}})
        plan = build_cleanup_plan("inbox-helper", hermes_home=tmp_path, query_source=qs)

        mutator = FakeMutator()
        result = apply_cleanup_plan(plan, mutator=mutator, hermes_home=tmp_path)

        assert isinstance(result, CleanupResult)
        # Mutator call order: deploy(empty), cleanup instance, cleanup blueprint.
        assert [c[0] for c in mutator.calls] == ["deploy", "cleanup", "cleanup"]
        assert mutator.calls[0][1] == {"instance_id": _INSTANCE_ID, "channels": []}
        assert mutator.calls[1][1] == {"kind": "instance", "identifier": _INSTANCE_ID}
        assert mutator.calls[2][1] == {"kind": "blueprint", "identifier": "inbox-helper"}
        # Local files removed.
        assert not (tmp_path / "agents" / "inbox-helper" / ".env").exists()
        assert not (tmp_path / "agents" / "inbox-helper" / "blueprint.json").exists()
        # Empty agent dir removed too.
        assert not (tmp_path / "agents" / "inbox-helper").exists()

    def test_skipped_deployment_does_not_call_deploy(self, tmp_path: Path) -> None:
        _seed_agent(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {}}})
        plan = build_cleanup_plan("inbox-helper", hermes_home=tmp_path, query_source=qs)
        mutator = FakeMutator()
        apply_cleanup_plan(plan, mutator=mutator, hermes_home=tmp_path)
        # deploy is NOT called; instance and blueprint cleanup are.
        ops = [c[0] for c in mutator.calls]
        assert "deploy" not in ops
        assert ops == ["cleanup", "cleanup"]

    def test_apps_are_never_touched(self, tmp_path: Path) -> None:
        _seed_agent(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {"teams": "ok"}}})
        plan = build_cleanup_plan("inbox-helper", hermes_home=tmp_path, query_source=qs)
        mutator = FakeMutator()
        apply_cleanup_plan(plan, mutator=mutator, hermes_home=tmp_path)
        # No `app` cleanup ever appears among the recorded calls.
        for op, kwargs in mutator.calls:
            if op == "cleanup":
                assert kwargs["kind"] != "app"

    def test_aadsts_error_propagates(self, tmp_path: Path) -> None:
        _seed_agent(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {"teams": "ok"}}})
        plan = build_cleanup_plan("inbox-helper", hermes_home=tmp_path, query_source=qs)
        mutator = FakeMutator(deploy_error=AADSTSError("AADSTS65001", "no perms"))
        with pytest.raises(AADSTSError) as excinfo:
            apply_cleanup_plan(plan, mutator=mutator, hermes_home=tmp_path)
        assert excinfo.value.code == "AADSTS65001"
        # Local files must remain when the cloud step failed.
        assert (tmp_path / "agents" / "inbox-helper" / ".env").exists()

    def test_no_local_files_means_no_local_removal(self, tmp_path: Path) -> None:
        plan = build_cleanup_plan("ghost", hermes_home=tmp_path, query_source=FakeQuerySource())
        mutator = FakeMutator()
        result = apply_cleanup_plan(plan, mutator=mutator, hermes_home=tmp_path)
        assert result.local_paths_removed == []

    def test_idempotent_rerun_after_full_cleanup(self, tmp_path: Path) -> None:
        _seed_agent(tmp_path)
        qs = FakeQuerySource(instances={_INSTANCE_ID: {"channels": {"teams": "ok"}}})
        plan = build_cleanup_plan("inbox-helper", hermes_home=tmp_path, query_source=qs)
        apply_cleanup_plan(plan, mutator=FakeMutator(), hermes_home=tmp_path)

        # Second run — agent dir is gone; plan is now blueprint-only with no local files.
        plan2 = build_cleanup_plan(
            "inbox-helper",
            hermes_home=tmp_path,
            query_source=FakeQuerySource(),
        )
        assert plan2.aa_instance_id is None
        assert [s.kind for s in plan2.steps] == ["blueprint"]
        assert plan2.local_paths == []
