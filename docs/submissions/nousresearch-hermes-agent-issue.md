# NousResearch `hermes-agent` issue submission

**Target**: <https://github.com/NousResearch/hermes-agent/issues/new>
**Status**: submitted 2026-05-05.
**Issue**: <https://github.com/NousResearch/hermes-agent/issues/20133>

---

## Title

Optional skill proposal: `hermes-a365` — Microsoft Agent 365 governance + bridge

## Body

### Summary

I'd like to propose adding **`hermes-a365`** as an official optional
skill under `optional-skills/cloud-platforms/hermes-a365/`. The skill
lets Hermes agents register with [Microsoft Agent 365](https://learn.microsoft.com/en-us/microsoft-agent-365/developer/)
(GA 2026-05-01), the governance / identity / observability control
plane, and answer messages in Teams / Outlook / M365 Copilot via a
Bot Framework webhook adapter.

Repo: <https://github.com/satscryption/Hermes-A365>

### Why this fits `optional-skills/`

Per [CONTRIBUTING.md](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md#should-the-skill-be-bundled),
optional skills are "useful but not universally needed". This skill
hits both qualifying flags:

- **Niche paid integration**: needs a Microsoft 365 tenant, Frontier
  Preview Program enrollment, and a `MICROSOFT_AGENT_365_TIER_3`
  license ($15/user/month) or `MICROSOFT_365_E7` ($99/user/month).
- **Heavyweight dependencies**: the GA `a365` CLI (1.1.171, .NET tool
  via NuGet), `pwsh` 7+, `az` CLI 2.55+, an OS keychain backend, and
  the optional [`bridge` extras](https://github.com/satscryption/Hermes-A365/blob/main/pyproject.toml)
  (`fastapi`, `uvicorn`, `httpx`, `pyjwt`) for the runtime adapter.

It's clearly not universally needed but is genuinely useful for any
operator running Hermes inside an M365-governed tenant.

### What's there

Validator-compliant `SKILL.md` (≈14.9k chars, under the 15k SPEC §3.2
ceiling), 444 tests passing (pytest + ruff clean), full spec, dated
reference snapshots, and an end-to-end live-tenant runbook completed
twice on a real Frontier-Preview tenant.

| Layer | Status |
|---|---|
| Doctor / status / license recommender | shipped |
| Setup orchestrator (`register`) — drives `a365 setup blueprint` + `setup permissions {mcp,bot}` with line-streamed output | shipped |
| Per-agent runtime config (`instance create`) — local-only `.env` writer | shipped |
| Manifest publish — branches blueprint-only (Graph instance registration) vs AI Teammate (zip) | shipped |
| Cleanup — drives `cleanup azure → instance → blueprint`, hardens secret-bearing backups | shipped |
| Activity bridge: `verify` (config + auth + reachability diagnostic) | shipped |
| Activity bridge: `serve` (BF webhook adapter — JWT-validated, replies via `serviceUrl`, forwards to `HERMES_BRIDGE_WEBHOOK`) | shipped (MVP — synchronous `message` + `invoke`) |
| Streaming responses + proactive long-running pattern | deferred (documented) |
| One known external defect (Microsoft CLI bug, [filed separately](https://github.com/microsoft/Agent365-devTools/issues/402)) | tracked |

The Hermes agent (= operator's responder) sits behind a stable JSON
contract documented at
[`references/webhook-contract.md`](https://github.com/satscryption/Hermes-A365/blob/main/references/webhook-contract.md).
The bridge handles BF wire concerns (JWT validation, reply via
`serviceUrl`, Adaptive Card framing); the operator's responder owns
*what* to say back.

### What I'm asking for

Two options, depending on what works for you:

1. **Light-touch link.** Add a stub `optional-skills/cloud-platforms/hermes-a365/`
   directory with a single `SKILL.md` that points at the upstream
   repo (`See: https://github.com/satscryption/Hermes-A365`). Hermes'
   `skills browse` would then surface it; users `skills install` and
   the install path follows the link. Smallest commitment for you;
   I'm happy to PR it.
2. **Full contribution.** Vendor the `SKILL.md` + supporting
   `templates/` + `scripts/` (or whichever subset you want) into the
   harness directly under
   `optional-skills/cloud-platforms/hermes-a365/`. More integration
   work, but the skill becomes part of the harness's official
   inventory.

I'll send a PR for whichever shape you prefer once you point me at
the right way in. Happy to iterate on packaging — e.g. if you'd
rather the skill carry only the prompt-side `SKILL.md` and the
operator runs the apply-side scripts from the Hermes-A365 repo
separately, that's also a clean model.

### Other notes

- License: MIT (matches the upstream).
- Maintenance: I'll continue maintaining the upstream Hermes-A365
  repo and keep the skill in lock-step with whatever lands here.
- Testing: 444 unit tests + a documented live-tenant runbook (manual
  for now since it requires a Frontier-Preview tenant). Happy to add
  a CI matrix or a `--mock` mode if that helps.
- Architecture: the skill is fully decoupled from any specific LLM
  provider — the bridge forwards to a webhook the operator controls,
  and the operator wires it to whichever responder they want
  (Hermes is the obvious one, but the webhook contract is generic).

Cheers — happy to chat in the [Nous Research Discord](https://discord.gg/NousResearch)
or here.
