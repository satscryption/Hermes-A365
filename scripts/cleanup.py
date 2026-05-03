"""hermes a365 cleanup — destructive teardown of a single agent.

Spec: SPEC.md §6.13. Order matters per spec: ``deployment → instance →
blueprint``. We deliberately stop short of the apps (T1/T2) because they
are tenant-wide infrastructure shared across every agent in the skill —
deleting them is a separate, more destructive operation that v0.1 does
not provide.

Safety: ``--confirm`` is required and must be the literal agent slug. The
plan output is always printed (regardless of ``--apply``) so the user can
audit what would be removed.

Local artefacts removed on success (apply only):
- ``~/.hermes/agents/<slug>/.env``
- ``~/.hermes/agents/<slug>/blueprint.json``
- ``~/.hermes/agents/<slug>/`` directory (only if empty after the above)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from _common import parse_env
from register import AADSTSError, Mutator, get_mutator
from status import QuerySource, get_query_source

CleanupKind = Literal["deployment", "instance", "blueprint"]

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CleanupError(RuntimeError):
    """Raised when cleanup can't proceed (missing confirm, agent not found)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def _agent_dir(hermes_home: Path, slug: str) -> Path:
    return hermes_home / "agents" / slug


def _agent_env_path(hermes_home: Path, slug: str) -> Path:
    return _agent_dir(hermes_home, slug) / ".env"


def _blueprint_cache_path(hermes_home: Path, slug: str) -> Path:
    return _agent_dir(hermes_home, slug) / "blueprint.json"


def _resolve_aa_instance_id(hermes_home: Path, slug: str) -> str | None:
    """Pull AA_INSTANCE_ID from the agent .env, or None if unrecorded."""
    env_path = _agent_env_path(hermes_home, slug)
    if not env_path.exists():
        return None
    return parse_env(env_path.read_text()).get("AA_INSTANCE_ID", "").strip() or None


def _instance_has_channels(payload: dict[str, Any] | None) -> bool:
    """True if the instance currently has any channels bound (state ``ok``)."""
    if not payload:
        return False
    channels = payload.get("channels") or {}
    if not isinstance(channels, dict):
        return False
    return any(state == "ok" for state in channels.values())


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class CleanupStep:
    """One ordered step the cleanup will (try to) execute."""

    kind: CleanupKind
    identifier: str
    detail: str
    skip_reason: str | None = None  # set when the step is a noop


@dataclass
class CleanupPlan:
    slug: str
    aa_instance_id: str | None
    steps: list[CleanupStep]
    local_paths: list[Path] = field(default_factory=list)

    def render_human(self) -> str:
        lines = [f"[plan] hermes a365 cleanup {self.slug}"]
        if self.aa_instance_id:
            lines.append(f"  AA_INSTANCE_ID: {self.aa_instance_id}")
        else:
            lines.append("  AA_INSTANCE_ID: (none recorded)")
        lines.append("  cloud steps (in order):")
        if not self.steps:
            lines.append("    (none — no cloud state to remove)")
        else:
            for s in self.steps:
                marker = "skip" if s.skip_reason else "would run"
                lines.append(f"    - {s.kind:<10}  {marker}: {s.detail}")
        lines.append("  local files to remove:")
        if not self.local_paths:
            lines.append("    (none)")
        else:
            for p in self.local_paths:
                lines.append(f"    - {p}")
        return "\n".join(lines)


def build_cleanup_plan(
    slug: str,
    *,
    hermes_home: Path | None = None,
    query_source: QuerySource | None = None,
) -> CleanupPlan:
    """Compose the cleanup plan from local + cloud state.

    The plan is built defensively: each step is included only when the
    underlying state appears to exist, and steps for missing state get a
    ``skip_reason``. ``apply_cleanup_plan`` honours those skips.
    """
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()
    qs = query_source or get_query_source()

    aa_instance_id = _resolve_aa_instance_id(hermes_home, slug)
    steps: list[CleanupStep] = []

    # 1. Deployment — only if there's an instance id and any channels are bound.
    if aa_instance_id:
        instance_payload = qs.query_instance(instance_id=aa_instance_id) if qs.available else None
        if _instance_has_channels(instance_payload):
            steps.append(
                CleanupStep(
                    kind="deployment",
                    identifier=aa_instance_id,
                    detail=f"unbind all channels for instance {aa_instance_id[:8]}…",
                )
            )
        else:
            steps.append(
                CleanupStep(
                    kind="deployment",
                    identifier=aa_instance_id,
                    detail=f"channels for {aa_instance_id[:8]}…",
                    skip_reason="no channels bound",
                )
            )
    # 2. Instance — only if there's an instance id (cloud presence not strictly required).
    if aa_instance_id:
        steps.append(
            CleanupStep(
                kind="instance",
                identifier=aa_instance_id,
                detail=f"delete instance {aa_instance_id[:8]}…",
            )
        )

    # 3. Blueprint — keyed by slug; cloud presence not strictly required.
    steps.append(
        CleanupStep(
            kind="blueprint",
            identifier=slug,
            detail=f"delete blueprint {slug!r}",
        )
    )

    # Local files we'll remove if they exist.
    local_paths: list[Path] = []
    for p in (
        _agent_env_path(hermes_home, slug),
        _blueprint_cache_path(hermes_home, slug),
    ):
        if p.exists():
            local_paths.append(p)

    return CleanupPlan(
        slug=slug,
        aa_instance_id=aa_instance_id,
        steps=steps,
        local_paths=local_paths,
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class CleanupResult:
    slug: str
    cloud_steps_run: list[str] = field(default_factory=list)
    cloud_steps_skipped: list[str] = field(default_factory=list)
    local_paths_removed: list[Path] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def apply_cleanup_plan(
    plan: CleanupPlan,
    *,
    mutator: Mutator,
    hermes_home: Path,
) -> CleanupResult:
    """Execute the plan: cloud steps in order, then local file removal."""
    result = CleanupResult(slug=plan.slug)

    for step in plan.steps:
        label = f"{step.kind} {step.identifier}"
        if step.skip_reason:
            result.cloud_steps_skipped.append(label)
            result.messages.append(f"[apply] skip {step.kind}: {step.skip_reason}")
            continue
        if step.kind == "deployment":
            # Unbind by setting the channel set to empty.
            mutator.deploy(instance_id=step.identifier, channels=[])
        else:
            mutator.cleanup(kind=step.kind, identifier=step.identifier)
        result.cloud_steps_run.append(label)
        result.messages.append(f"[apply] {step.kind}: {step.detail}")

    # Local artefact removal.
    for path in plan.local_paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        result.local_paths_removed.append(path)
        result.messages.append(f"[apply] removed {path}")

    # Best-effort agent dir cleanup (only if empty after file removal).
    agent_dir = _agent_dir(hermes_home, plan.slug)
    if agent_dir.exists() and not any(agent_dir.iterdir()):
        agent_dir.rmdir()
        result.messages.append(f"[apply] removed empty dir {agent_dir}")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _validate_confirm(slug: str, confirm: str | None) -> None:
    if confirm is None:
        raise CleanupError(
            f"--confirm is required and must be the agent slug literal (e.g. --confirm={slug})"
        )
    if confirm != slug:
        raise CleanupError(
            f"--confirm value {confirm!r} does not match slug {slug!r}; refusing to proceed"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 cleanup — destructive per-agent teardown.",
    )
    parser.add_argument("slug", help="agent slug to tear down")
    parser.add_argument(
        "--confirm",
        help="must equal the agent slug for the apply path to proceed",
    )
    parser.add_argument("--apply", action="store_true", help="execute the plan; default is dry-run")
    parser.add_argument(
        "--json", action="store_true", help="emit the plan as JSON instead of human-readable text"
    )
    args = parser.parse_args(argv)

    plan = build_cleanup_plan(args.slug)

    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "slug": plan.slug,
                    "aa_instance_id": plan.aa_instance_id,
                    "steps": [
                        {
                            "kind": s.kind,
                            "identifier": s.identifier,
                            "detail": s.detail,
                            "skip_reason": s.skip_reason,
                        }
                        for s in plan.steps
                    ],
                    "local_paths": [str(p) for p in plan.local_paths],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    else:
        sys.stdout.write(plan.render_human() + "\n")

    if not args.apply:
        sys.stdout.write(
            f"\nNo mutations. Re-run with --apply --confirm={args.slug} to tear down.\n"
        )
        return 0

    try:
        _validate_confirm(args.slug, args.confirm)
    except CleanupError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        result = apply_cleanup_plan(plan, mutator=get_mutator(), hermes_home=_resolve_hermes_home())
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2

    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
