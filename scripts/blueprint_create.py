"""hermes a365 blueprint create — register / patch an A365 agent blueprint.

Spec: SPEC.md §6.4. Composes :mod:`render_blueprint` (rendering) with
:mod:`reconcile_blueprint` (idempotent diff) and the :class:`Mutator`
protocol (mutation). Default mode is dry-run — pass ``--apply`` to execute.

Apply path (when the plan is non-noop):

1. Render the desired blueprint JSON via :func:`render_blueprint.render_blueprint`.
2. Write it to a tmp file under ``$TMPDIR/hermes-a365/<slug>.blueprint.json``
   (used as the ``--file=`` argument for ``a365 setup blueprint``).
3. Call ``mutator.setup_blueprint(file_path=...)``. The same CLI handles both
   create and update; A365 deduces the action from the rendered slug.
4. Atomically cache the rendered JSON at
   ``~/.hermes/agents/<slug>/blueprint.json`` so future re-runs can reconcile
   locally before round-tripping to the cloud.

A noop plan rewrites the cache file and returns without calling the mutator.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reconcile_blueprint import BlueprintPlan, reconcile_blueprint
from register import AADSTSError, Mutator, get_mutator
from render_blueprint import (
    DEFAULT_DLP,
    DEFAULT_EXTERNAL_ACCESS,
    DEFAULT_LOGGING,
    BlueprintInputs,
    render_blueprint,
)
from status import QuerySource, get_query_source

_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"

# Fields A365 assigns server-side that won't appear in our rendered desired
# payload. Stripped from the actual blueprint before diffing so noop plans
# aren't perturbed by ids and timestamps.
_SERVER_ASSIGNED_FIELDS: frozenset[str] = frozenset(
    {
        "blueprintId",
        "blueprint_id",
        "id",
        "createdAt",
        "lastPatched",
        "last_patched",
        "etag",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BlueprintCreateError(RuntimeError):
    """Raised when blueprint create's apply path can't proceed."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def _agent_blueprint_cache_path(hermes_home: Path, slug: str) -> Path:
    return hermes_home / "agents" / slug / "blueprint.json"


def _tmp_render_path(slug: str) -> Path:
    """Path to the tmp file we hand to ``a365 setup blueprint --file=...``."""
    base = Path(tempfile.gettempdir()) / "hermes-a365"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{slug}.blueprint.json"


# ---------------------------------------------------------------------------
# Atomic JSON write
# ---------------------------------------------------------------------------


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as canonical JSON via tmp + rename. Creates parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class BlueprintCreateResult:
    """Outcome of executing a blueprint plan."""

    slug: str
    plan: BlueprintPlan
    blueprint_id: str | None = None
    rendered_path: Path | None = None  # tmp file used for the CLI call
    cache_path: Path | None = None  # ~/.hermes/agents/<slug>/blueprint.json
    messages: list[str] = field(default_factory=list)
    mutated: bool = False


def _strip_server_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` minus server-assigned fields (top-level only)."""
    return {k: v for k, v in payload.items() if k not in _SERVER_ASSIGNED_FIELDS}


def _existing_id_of(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    for key in ("blueprintId", "blueprint_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


@dataclass
class PlanContext:
    """Bundle returned by :func:`build_blueprint_plan` — plan + supporting data."""

    plan: BlueprintPlan
    rendered: dict[str, Any]
    existing_blueprint_id: str | None = None


def build_blueprint_plan(
    inputs: BlueprintInputs,
    *,
    query_source: QuerySource | None = None,
) -> PlanContext:
    """Render the desired blueprint, query actual, return the plan context.

    Server-assigned fields on the actual payload are stripped before diffing,
    so a freshly-registered blueprint reads as a clean noop on the second
    run. Their values (specifically ``blueprintId``) are preserved on the
    returned context so the apply path can surface them on a noop.
    """
    qs = query_source or get_query_source()
    desired = render_blueprint(inputs)

    actual: dict[str, Any] | None = None
    existing_id: str | None = None
    if qs.available:
        raw = qs.query_blueprint(slug=inputs.slug)
        if raw is not None:
            existing_id = _existing_id_of(raw)
            actual = _strip_server_fields(raw)

    return PlanContext(
        plan=reconcile_blueprint(desired, actual),
        rendered=desired,
        existing_blueprint_id=existing_id,
    )


def render_plan_human(inputs: BlueprintInputs, plan: BlueprintPlan, rendered_path: Path) -> str:
    """Format the dry-run plan output per SPEC §6.4 examples."""
    size = rendered_path.stat().st_size if rendered_path.exists() else 0
    lines: list[str] = [
        f"[plan] blueprint {plan.slug}",
        f"  rendered → {rendered_path} ({_format_size(size)})",
        f"  actual:  {'not registered' if plan.actual is None else 'registered'}",
        f"  delta:   {plan.action}",
        "",
        f"DLP policy:               {inputs.dlp_policy}",
        f"External access:          {inputs.external_access_policy}",
        f"Logging policy:           {inputs.logging_policy}",
        f"Work IQ tools requested:  {', '.join(inputs.workiq_tools) or '(none)'}",
        f"App roles:                {', '.join(inputs.app_roles)}",
        f"Optional claims:          {', '.join(inputs.optional_claims)}",
    ]
    if plan.action == "patch" and plan.diff:
        lines.append("")
        lines.append("Diff (actual → desired):")
        for path in sorted(plan.diff):
            actual, desired = plan.diff[path]
            lines.append(f"  {path}  {actual!r} -> {desired!r}")
    if plan.action == "abort":
        lines.append("")
        lines.append(f"ABORT: {plan.abort_reason}")
    return "\n".join(lines)


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.1f} KB"


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def apply_blueprint_plan(
    inputs: BlueprintInputs,
    ctx: PlanContext,
    *,
    mutator: Mutator,
    hermes_home: Path,
    rendered_path: Path | None = None,
) -> BlueprintCreateResult:
    """Execute a blueprint plan, returning the captured state.

    - ``abort`` → raises :class:`BlueprintCreateError`.
    - ``noop`` → mutator untouched; cache file still rewritten.
    - ``create``/``patch`` → tmp render is handed to ``mutator.setup_blueprint``
      and the captured ``blueprintId`` is returned. Cache file is rewritten on
      success only.
    """
    plan = ctx.plan
    rendered_payload = ctx.rendered

    if plan.action == "abort":
        raise BlueprintCreateError(f"refusing to apply: {plan.abort_reason}")

    cache_path = _agent_blueprint_cache_path(hermes_home, inputs.slug)

    if plan.action == "noop":
        write_json_atomic(cache_path, rendered_payload)
        return BlueprintCreateResult(
            slug=inputs.slug,
            plan=plan,
            blueprint_id=ctx.existing_blueprint_id,
            cache_path=cache_path,
            messages=[
                f"[apply] blueprint {inputs.slug!r} already matches — no change",
                f"[apply] cache refreshed: {cache_path}",
            ],
        )

    # create or patch: render to tmp file, hand to mutator
    if rendered_path is None:
        rendered_path = _tmp_render_path(inputs.slug)
    write_json_atomic(rendered_path, rendered_payload)

    response = mutator.setup_blueprint(file_path=rendered_path)

    blueprint_id = _existing_id_of(response)
    if blueprint_id is None:
        raise BlueprintCreateError(
            f"setup blueprint succeeded but returned no blueprintId; got {response!r}"
        )

    write_json_atomic(cache_path, rendered_payload)

    verb = "registered" if plan.action == "create" else f"PATCH ({len(plan.diff)} field(s))"
    messages = [
        f"[apply] a365 setup blueprint --file={rendered_path}",
        f"[apply] {verb}: blueprint_id={blueprint_id}",
        f"[apply] cached: {cache_path}",
    ]
    return BlueprintCreateResult(
        slug=inputs.slug,
        plan=plan,
        blueprint_id=blueprint_id,
        rendered_path=rendered_path,
        cache_path=cache_path,
        messages=messages,
        mutated=True,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 blueprint create — register or patch an A365 agent blueprint.",
    )
    parser.add_argument("slug", help="agent slug (also the blueprint identifier)")
    parser.add_argument("--description", required=True)
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--display-name")
    parser.add_argument("--workiq", default="", help="comma-separated Work IQ tools")
    parser.add_argument("--functions", default="", help="comma-separated function names")
    parser.add_argument("--dlp", default=DEFAULT_DLP)
    parser.add_argument("--external-access", default=DEFAULT_EXTERNAL_ACCESS)
    parser.add_argument("--logging", default=DEFAULT_LOGGING)
    parser.add_argument("--apply", action="store_true", help="execute the plan; default is dry-run")
    args = parser.parse_args(argv)

    try:
        inputs = BlueprintInputs(
            slug=args.slug,
            description=args.description,
            purpose=args.purpose,
            display_name=args.display_name,
            workiq_tools=_split_csv(args.workiq),
            functions=_split_csv(args.functions),
            dlp_policy=args.dlp,
            external_access_policy=args.external_access,
            logging_policy=args.logging,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    ctx = build_blueprint_plan(inputs)

    # Always render to tmp so the dry-run output can show the path/size.
    rendered_path = _tmp_render_path(inputs.slug)
    write_json_atomic(rendered_path, ctx.rendered)

    sys.stdout.write(render_plan_human(inputs, ctx.plan, rendered_path) + "\n")

    if ctx.plan.action == "abort":
        return 2

    if not args.apply:
        sys.stdout.write("\nNo mutations. Re-run with --apply to register.\n")
        return 0

    try:
        result = apply_blueprint_plan(
            inputs,
            ctx,
            mutator=get_mutator(),
            hermes_home=_resolve_hermes_home(),
            rendered_path=rendered_path,
        )
    except BlueprintCreateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except AADSTSError as e:
        print(f"ERROR {e.code}: {e.message}", file=sys.stderr)
        return 2

    sys.stdout.write("\n" + "\n".join(result.messages) + "\ndone.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
