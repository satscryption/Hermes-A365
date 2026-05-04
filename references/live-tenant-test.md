# Live tenant integration test — Hermes-A365 v0.2

End-to-end runbook for verifying the v0.2 skill against a real Microsoft
Agent 365 tenant. Walk top-to-bottom on first run; expect ~30–45 minutes
including the M365 Admin Centre approval step.

**Snapshot:** 2026-05-04. Pinned against `e8c4282` (Slice 18g).

## What you need before starting

- A Microsoft 365 tenant where you hold **Global Administrator** or
  **Agent Administrator**, enrolled in Microsoft's **Frontier Preview
  Program** (Agent 365 is gated on this; status visible in the M365
  Admin Centre under Settings → Org settings → Agent 365).
- An A365 license already assigned to your test user account: either
  **Agent 365 add-on** ($15/user/month) or **Microsoft 365 E7**
  ($99/user/month). Do **not** rely on `register` for license
  propagation — it retries `AADSTS500011` 3× with 30 s backoff, but if
  your assignment is fresh (< 5 min old) you'll thrash through retries.
  Wait, then re-run.
- The custom Entra client app **`Agent 365 CLI`** registered in the
  tenant. Microsoft's bootstrap docs walk through this; doctor verifies
  discoverability.
- Local prereqs: `a365` CLI ≥ 1.0.0, `az` CLI ≥ 2.55.0 signed in to the
  target tenant (`az login --tenant <tenant>`), `pwsh` 7+ on PATH, an
  OS keychain backend (macOS Security or Linux libsecret).
- A test mailbox / Teams account in the same tenant to drive the test
  message at the end.

Record these once, used throughout:

| Var | Example | Meaning |
|---|---|---|
| `<tenant>` | `contoso.onmicrosoft.com` | M365 tenant domain |
| `<tenant-id>` | `<guid>` | Entra tenant id |
| `<display-name>` | `Hermes Inbox Helper` | CLI `--agent-name`; the CLI derives `<display-name> Identity` and `<display-name> Blueprint` from this |
| `<slug>` | `inbox-helper` | local slug used for `~/.hermes/agents/<slug>/` |
| `<owner-email>` | `sadiq@contoso.com` | test user / agent owner |
| `<owner-aad-id>` | `<guid>` | their Entra object id (`az ad signed-in-user show --query id -o tsv`) |

## 0. Local bootstrap

Create `~/.hermes/.env` if it doesn't exist:

```
A365_TENANT_ID=<tenant-id>
A365_APP_ID=                                         # filled by register --apply
HERMES_OTLP_ENDPOINT=https://<tenant>.otel.agent365.microsoft.com
A365_LICENSE_MODEL=                                  # filled by license
```

In the repo root, you'll also want a working `a365.config.json` —
register populates derived display names there on `--apply`:

```bash
cd /Users/sadiqjaffer/satscryption/Hermes-A365
echo '{}' > a365.config.json
```

Throughout this runbook, every `uv run` command is run from the repo
root.

## 1. doctor — environment readiness

```bash
uv run python scripts/doctor.py --human
echo "exit=$?"
```

**Pass criterion:** `exit=0`. Every probe should show `ok`. Common
amber paths:

- `pwsh` missing → `brew install --cask powershell` (mac) or follow
  Microsoft's PowerShell install docs.
- `Agent 365 CLI` client app not discoverable → register it (Microsoft
  bootstrap docs); re-run doctor.
- Network probe failing → corporate proxy. Doctor honours `HTTPS_PROXY`.
- `~/.hermes/.env` missing → step 0 above.

- [ ] `doctor --human` exits 0 against the live tenant.

## 2. license — recommendation only (no purchase)

```bash
uv run python scripts/license.py --users 5 --agents 1 --plan E5
```

**Pass criterion:** prints a recommendation and writes
`A365_LICENSE_MODEL` into `~/.hermes/.env`. This step never calls the
tenant; it's a sanity check that the local config is wired up.

- [ ] `license` recommendation rendered; `A365_LICENSE_MODEL` set in
      `~/.hermes/.env`.

## 3. register — `setup blueprint` + `setup permissions {mcp,bot}`

Dry-run first to inspect the plan:

```bash
uv run python scripts/register.py --agent-name "<display-name>" --tenant-id <tenant-id>
```

You should see three steps: `blueprint`, `permissions-mcp`,
`permissions-bot`, each with the exact `argv` that would be invoked. No
mutations.

Apply:

```bash
uv run python scripts/register.py --agent-name "<display-name>" --tenant-id <tenant-id> --apply
```

**Pass criterion:** the three steps complete with exit 0.
`a365.config.json` now contains the derived display names.
`~/.hermes/.env`'s `A365_APP_ID` is populated.

Failure modes to watch:

- **AADSTS500011** (license not yet propagated) — the wrapper retries
  3× / 30 s. If exhausted, wait a few minutes and re-run; license
  propagation can lag up to 30 min after assignment.
- **AADSTS90094** (admin consent required) — surfaced as
  `deferred — run hermes a365 consent`. The blueprint apps were still
  created; proceed to step 4.
- **`pwsh` not found** — the CLI itself errors out with `setup
  requirements`. Fix the prereq and re-run.

- [ ] `register --apply` exits 0 (or AADSTS90094 deferred).
- [ ] Blueprint app + service principal visible in Entra Portal under
      App Registrations.
- [ ] `a365.config.json` shows the derived `<display-name> Blueprint`
      and `<display-name> Identity` names.

## 4. consent — admin grant

```bash
uv run python scripts/consent.py
```

This opens the admin-consent URL in your default browser. Sign in as a
Global Admin, accept the delegated permissions. The script polls
`a365 query-entra blueprint-scopes` every 5 s and exits 0 when grant is
detected (timeout 5 min).

If you're driving this headlessly, use:

```bash
uv run python scripts/consent.py --print-url-only       # emit URL only
# … grant in the browser elsewhere …
uv run python scripts/consent.py                        # poll until detected
```

- [ ] Consent granted; `consent` exits 0.

## 5. instance create — local runtime .env

Dry-run:

```bash
uv run python scripts/instance_create.py <slug> \
    --owner <owner-email> \
    --owner-aad-id <owner-aad-id>
```

Apply:

```bash
uv run python scripts/instance_create.py <slug> \
    --owner <owner-email> \
    --owner-aad-id <owner-aad-id> \
    --apply
```

This is purely local — no cloud calls. It writes
`~/.hermes/agents/<slug>/.env` with `AA_INSTANCE_ID` (preserved across
re-runs), owner metadata, and inherited `A365_APP_ID` /
`A365_TENANT_ID` / `HERMES_OTLP_ENDPOINT`.

- [ ] `~/.hermes/agents/<slug>/.env` exists, parseable, contains
      `AA_INSTANCE_ID`.
- [ ] T2 / blueprint client secret is **not** in the file (verify with
      `grep -i secret ~/.hermes/agents/<slug>/.env` → no matches).

## 6. publish — manifest packaging

```bash
uv run python scripts/publish.py --agent-name "<display-name>"          # plan
uv run python scripts/publish.py --agent-name "<display-name>" --apply  # produce zip
```

`a365 publish` produces a manifest zip; the wrapper prints the resulting
path and the M365 Admin Centre URL hint.

- [ ] Manifest zip produced; path printed by the script.

## 7. Operator step — upload + approve in M365 Admin Centre

This is the only step the skill cannot drive. Channel deployment in
v0.2 is admin-centre-side.

1. Sign in to the M365 Admin Centre as Global Admin.
2. Navigate to Settings → Integrated apps → Upload custom apps.
3. Upload the zip from step 6.
4. Approve the agent for users in your test DLP scope (your test
   account must be in scope).
5. Wait 1–5 min for propagation.

- [ ] Zip uploaded and approved in the Admin Centre.
- [ ] Agent visible in Teams app catalog for the test user.

## 8. End-to-end activity — test message

The activity bridge is still TODO (SPEC §10 Q1), so this step verifies
the **A365 governance plane** is wired up, not the Hermes runtime
roundtrip. Until activity-bridge ships, expect either:

- A `default` Microsoft response card (governance OK, no Hermes
  runtime), or
- An empty / loading card if the bot endpoint isn't yet bound — that's
  acceptable for this test.

Drive a test message:

1. In Teams, open a 1:1 chat with the agent (search for `<display-name>`).
2. Send a plain `hello`.
3. Open the M365 Admin Centre → Agent 365 → Telemetry within ~5 min.

- [ ] OTLP trace appears for the test message in the admin-centre
      telemetry view (or in your tenant's connected backend if you've
      configured one; the OTLP endpoint is in `~/.hermes/.env`).

When activity-bridge ships, this step gets a second checkbox: an
Adaptive Card response from the Hermes agent.

## 9. status — sanity check against `query-entra`

```bash
uv run python scripts/status.py <slug> --human
echo "exit=$?"
```

**Pass criterion:** `exit=0`. Components reported: `local_config`,
`blueprint_scopes`, `instance_scopes`, `activity_bridge` (the last is
expected `missing` until the bridge ships — that's fine; status returns
`partial`/exit 1 if you want strict).

- [ ] `local_config: ok`
- [ ] `blueprint_scopes: ok`
- [ ] `instance_scopes: ok`

## 10. cleanup — leave the tenant clean

Dry-run:

```bash
uv run python scripts/cleanup.py --agent-name "<display-name>"
```

You'll see three planned steps: `azure`, `instance`, `blueprint` (in
that order — safe → unsafe). If your test agent had no Azure App
Service, the `azure` step will be a recorded skip.

Apply:

```bash
uv run python scripts/cleanup.py --agent-name "<display-name>" \
    --apply --confirm "<display-name>"
```

After the cloud steps succeed, the local
`~/.hermes/agents/<slug>/` directory is removed.

If you only want to remove the agent identity and blueprint (Azure was
provisioned out-of-band):

```bash
uv run python scripts/cleanup.py --agent-name "<display-name>" \
    --kinds=instance,blueprint --apply --confirm "<display-name>"
```

- [ ] `cleanup --apply` exits 0.
- [ ] Blueprint app + service principal removed from Entra Portal.
- [ ] `~/.hermes/agents/<slug>/` removed locally.
- [ ] Tenant-wide infra (`Agent 365 CLI` client app, license, Frontier
      Preview enrollment) is **untouched** — verify in the Admin Centre.

## Roll-up

If every checkbox above is ticked, the v0.2 skill is verified
end-to-end against your tenant. Open issues observed during the run
(unexpected error codes, CLI behaviour mismatches with
`references/a365-cli-reference.md`, retry counts that needed bumping)
should land in the repo as a follow-up slice — these are the highest-
signal inputs we can get pre-activity-bridge.

If a step fails, **do not** skip ahead — most downstream steps depend on
the prior step's tenant state. Fix in place or run `cleanup` and start
over.
