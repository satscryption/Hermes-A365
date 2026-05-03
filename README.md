# Hermes-A365

Integrate Hermes agents into the Microsoft 365 ecosystem using Microsoft Agent 365 (A365).

## Status

**Design stage.** No implementation yet. The current artefact is [`SPEC.md`](SPEC.md) — a v1 draft of the Hermes-side skill that will reproduce, for Hermes-driven agents, the integration surface that OpenClaw exposes for A365 today.

The spec was produced 2026-05-03, two days after A365 reached general availability (2026-05-01).

## Repo layout

```
.
├── SPEC.md         # Authoritative spec for the hermes-a365 skill
├── README.md       # This file
├── LICENSE         # MIT
├── .gitignore
├── references/     # Microsoft Learn pointers, BF activity shapes, blueprint property reference
├── scripts/        # Prototype helpers (doctor, blueprint render, status, activity bridge, …)
└── templates/      # Blueprint JSON, per-agent .env, Adaptive Card payloads
```

## Repo split

This repo holds the **design artefacts** — spec, references, scripts, templates. The eventual Hermes `SKILL.md` is **contributed upstream** into the Hermes Agent harness at `hermes-agent/optional-skills/cloud-platforms/hermes-a365/SKILL.md`, pulling these artefacts in at contribution time. See [`SPEC.md` §3.1 and §13](SPEC.md) for the full rationale.

## What is A365?

[**Microsoft Agent 365**](https://learn.microsoft.com/en-us/microsoft-agent-365/developer/) is a governance / identity / observability control plane for AI agents that GA'd 2026-05-01. It is **not** an agent framework — it bolts on top of whichever agent stack you use (Microsoft Agent Framework, Microsoft 365 Agents SDK, OpenAI Agents SDK, OpenClaw, Claude Code SDK, etc.) and adds:

- Entra-backed agent identity (delegated permissions only)
- Tenant licensing (\$15/user/mo add-on, or M365 E7 \$99/user/mo)
- Agent blueprints, registered via `a365 setup blueprint`
- MCP-mediated access to Microsoft 365 data (Mail, Calendar, SharePoint, Teams) — "Work IQ tools"
- Bot Framework Activity protocol for notifications and Adaptive Card invokes
- OpenTelemetry observability surfaced in admin center
- Teams / Outlook / Microsoft 365 Copilot channel adapters

`hermes-a365` is the Hermes-side skill that drives these from inside the Hermes harness.

## Open questions

See [`SPEC.md` §10](SPEC.md). Highest-priority: the Hermes IPC contract that the Activity bridge will use to invoke the local agent.

## Status meta

- **2026-05-03:** repo created, `SPEC.md` v1 draft committed.
- Implementation has **not** started. No subcommand of `hermes a365 …` is wired yet.

## License

MIT — see [`LICENSE`](LICENSE).
