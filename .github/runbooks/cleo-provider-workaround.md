# CLEO LLM Provider Workaround (OpenRouter Transport Defect)

CLEO 2026.8.9's native `openrouter` transport is defective upstream: it
misroutes requests to `api.openai.com`, ignores credential `--base-url`
overrides, and ignores the `llm.providers.<id>.baseUrl` config key, so
OpenRouter calls fail with an OpenAI-key 401. Filed upstream as
`kryptobaseddev/cleo#1216`. This runbook tracks the local workaround
(enabled on 2026-09-04) and the checklist for retiring it once the fix
ships. Background: brain observations `O-mtn8k38i-0` and `O-mtn92j4t-0`
(supersede both at retirement).

## The Defect

- Native `openrouter` transport hits `api.openai.com` instead of
  `https://openrouter.ai/api/v1` (misroute → 401 with OpenAI key messaging).
- Credential `--base-url` overrides are ignored; the global config key
  `llm.providers.<id>.baseUrl` is inert.
- A local fork fix exists (see "Fork State" below) but stock releases must
  be re-verified after upgrading past 2026.8.9.

## Current Workaround (local machine state)

The OpenRouter key is stored as a CLEO credential under the `openai`
provider slot with label `openrouter-glm` (secret values live only in the
user-local credential store; never copy them into this runbook or any
tracked file). Configuration locations:

| Item | Location | Value |
| --- | --- | --- |
| Credential | `%LOCALAPPDATA%\cleo\Data\llm-credentials.json` → `credentials.openai`, label `openrouter-glm` | provider `openai`, OpenRouter key (secret) |
| Base-URL redirect | User environment variable `OPENAI_BASE_URL` | `https://openrouter.ai/api/v1` |
| Inert marker key | `%USERPROFILE%\.cleo\config.json` → `llm.providers.openai.baseUrl` | `https://openrouter.ai/api/v1` (not honored by 2026.8.9) |
| Global default | `%USERPROFILE%\.cleo\config.json` → `llm.default` | `openai` / `z-ai/glm-5.3-flash` |
| BRAIN roles | `%USERPROFILE%\.cleo\config.json` → `llm.roles` | all five (`extraction`, `consolidation`, `derivation`, `hygiene`, `judgement`) pinned to `openai` / `z-ai/glm-5.3-flash` |
| Transport plugin | `%USERPROFILE%\.cleo\plugins\model-providers\openai-openrouter-redirect.mjs` | redirects openai-compatible transport to OpenRouter |

Verified present 2026-09-05: credential file, plugin file, user env var,
config keys, and `.github/scripts/backfill-brain-embeddings.mjs` (retirement
candidate, tracked as T205).

## Detection Method

Watch for a CLEO release newer than 2026.8.9 that closes
`kryptobaseddev/cleo#1216`, then confirm behaviorally — the reported
version string alone is not proof (the local fork build also reports
`@cleocode/cleo 2026.8.9`):

```powershell
cleo --version
# With OPENAI_BASE_URL temporarily cleared for the session:
$env:OPENAI_BASE_URL = $null
cleo llm test openrouter    # ping must succeed
cleo llm stream openrouter  # native transport must stream
```

If both succeed natively (no env var, no plugin, no `openai` slot), the fix
has shipped and the retirement checklist can run.

## Fork State

Phase 1 (2026-09-04): forked `kryptobaseddev/cleo` to `Carson-Fricke/cleo`,
branch `fix/subsystem-defects-2026.8.9` (commit `310c4e986`) repairing
#1216/#1217/#1218/#1219; installed globally (install timestamp matches
2026-09-04 17:49 local). Follow-up commit `899c247e5` (2026-09-05): nexus
parse-loop partitioning (TS/JS worker pool vs Python/Go/Rust inline) and
graph search pattern-arg repair; FCA re-indexed to 5042 nodes / 17432
relations with working impact analysis. Revert path:
`npm i -g @cleocode/cleo@2026.8.9`.

## Retirement Checklist

1. Upgrade CLEO past 2026.8.9 with #1216 fixed (upstream release, or keep
   the verified fork build), then confirm `cleo llm test openrouter` and
   `cleo llm stream openrouter` succeed without any workaround.
2. Migrate the global default and all five BRAIN roles back to the native
   `openrouter` provider, retaining `z-ai/glm-5.3-flash` as the model.
3. Migrate or re-enter the OpenRouter credential under the native
   `openrouter` provider, then remove the `openai/openrouter-glm` entry.
4. Remove the `OPENAI_BASE_URL` user environment variable (footgun: it
   silently redirects every OpenAI-compatible client on the machine).
5. Remove the inert config key `llm.providers.openai.baseUrl` from
   `%USERPROFILE%\.cleo\config.json`.
6. Remove the `%USERPROFILE%\.cleo\plugins\model-providers\openai-openrouter-redirect.mjs`
   plugin.
7. After the full #1218 fix ships upstream, verify nexus symbol counts
   match the fork's (5042 nodes / 17432 relations on FCA) before closing.
8. Optionally retire `.github/scripts/backfill-brain-embeddings.mjs`
   (decision tracked as T205).
9. Confirm the global `cleo.db` journal no longer spams brain_attention
   "table already exists" reconciliation warnings (journal was repaired
   2026-09-04 with backup `cleo-global-before-journal-repair.db`).
10. Supersede brain observations `O-mtn8k38i-0` and `O-mtn92j4t-0` with a
    new observation recording the restored native configuration.
11. Update this runbook to record the retirement (keep it as historical
    reference or delete it), then close T189.
