"""agent365 plugin — Microsoft Agent 365 gateway adapter for Hermes.

Registers two surfaces with the Hermes plugin context:

1. The ``agent365`` platform adapter (BF-shaped activities in/out;
   AAD-v2 inbound JWTs; agentic three-stage user-FIC outbound chain).
   Wired via :func:`adapter.register`.

2. The ``hermes a365 <verb>`` CLI surface (doctor / license / register
   / consent / instance / publish / status / cleanup / activity-bridge),
   wired via :func:`cli.register_cli`. Each verb delegates to the
   corresponding ``scripts/<x>.py`` module's ``build_parser`` + ``run``
   pair, so flags and behaviour stay identical to running the scripts
   directly.
"""

from __future__ import annotations

from .adapter import register as _register_adapter
from .cli import a365_command, register_cli


def register(ctx) -> None:
    """Plugin entry point — invoked once by the Hermes plugin loader."""
    _register_adapter(ctx)

    ctx.register_cli_command(
        name="a365",
        help="Microsoft Agent 365 wrapper (setup, status, cleanup, bridge)",
        setup_fn=register_cli,
        handler_fn=a365_command,
        description=(
            "Wraps Microsoft.Agents.A365.DevTools.Cli for Hermes operators "
            "and runs the Bot Framework activity bridge that backs the "
            "agent365 gateway platform. See: hermes a365 doctor"
        ),
    )


__all__ = ["register"]
