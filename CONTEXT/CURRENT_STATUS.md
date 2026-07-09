# Current Status — BackroomsGPT FINAL v21

## Canonical local candidate

**BackroomsGPT FINAL v21.0.0 (`BACKROOMSGPT-FINAL-21`)** is the canonical
candidate in this handoff folder. Do not replace it with archived v14 or
pre-hotfix v20 material.

## Verified local state

- Python 3.12.13 clean virtual environment with project dependencies.
- Compilation: pass.
- Full local suite: **44 tests pass**.
- Smoke test: **`SMOKE PASS 21.0.0 53 routes`**.
- Custom GPT Action schema: **33 operations**; 29 legacy operation IDs remain
  stable and four justified v21 operations were added for Writer Project state
  and candidate probing.
- Knowledge files: 20.

The local candidate includes the former v20 hotfixes plus v21 finalization:

- strict live and archive page identity, including Liminal Baby Food and Level
  0 regressions;
- failure taxonomy that distinguishes no-match/not-found from source failure;
- centralized HTTPS-only allowlist/DNS/redirect validation for server fetches;
- body, query, fan-out, response, graph, sync, and Action payload bounds;
- protected write/admin endpoints, rate/concurrency limits, and a fail-closed
  missing-secret state;
- atomic Atlas document/snapshot/edge and Writer Project writes;
- project capability, expiry, deletion, retention, and honest durability guard;
- compact typed v21 Action responses and current Knowledge routing;
- durable Render disk blueprint and privacy/auth configuration documentation.

## Verified production state after v21 deployment

The current GitHub `main` branch contains the v21 runtime, Atlas modules,
security helpers, tests, deployment configuration, documentation, and
`GPT_ACTIONS/openapi-actions-v21.json`. Render is serving `21.0.0` /
`BACKROOMSGPT-FINAL-21`, with `/health`, `/openapi.json`, `/privacy`, and Atlas
routes available.

Production smoke checks established two release blockers:

- `/atlas/stats` reports `storage_mode: ephemeral`; a durable Render disk is
  not attached or not active yet.
- Protected Atlas writes return the intentional fail-closed `503` because
  `BACKROOMSGPT_ACTION_API_KEY` is not configured.

Liminal Archives is currently intermittently unavailable upstream. The v21
runtime reports that as `503 source_unavailable` instead of converting it into
a cached false `404 page_not_found`.

## Production release gate still pending

Before calling v21 production-clean, the owner must:

1. In Render, attach/confirm the persistent disk at `/var/data` and configure
   `BACKROOMSGPT_ACTION_API_KEY` as a secret.
2. Configure the same secret as Custom GPT Action API-key/Bearer auth and use
   `GPT_ACTIONS/openapi-actions-v21.json`.
3. Confirm production reports `storage_mode: durable` under `/var/data`.
4. Run authorized isolated-write checks: ingest, repeat/idempotency,
   rollback/edges, and the complete acceptance eval suite.
5. Re-run the read-only Preview flow after the upstream sources are reachable.

Local tests do not prove external source availability, Render disk attachment,
or Custom GPT builder configuration.
# Current Status — BackroomsGPT FINAL v21

## Canonical local candidate

**BackroomsGPT FINAL v21.0.0 (`BACKROOMSGPT-FINAL-21`)** is the canonical
candidate in this handoff folder. Do not replace it with archived v14 or
pre-hotfix v20 material.

## Verified local state

- Python 3.12.13 clean virtual environment with project dependencies.
- Compilation: pass.
- Full local suite: **44 tests pass**.
- Smoke test: **`SMOKE PASS 21.0.0 53 routes`**.
- Custom GPT Action schema: **33 operations**; 29 legacy operation IDs remain
  stable and four justified v21 operations were added for Writer Project state
  and candidate probing.
- Knowledge files: 20.

The local candidate includes the former v20 hotfixes plus v21 finalization:

- strict live and archive page identity, including Liminal Baby Food and Level
  0 regressions;
- failure taxonomy that distinguishes no-match/not-found from source failure;
- centralized HTTPS-only allowlist/DNS/redirect validation for server fetches;
- body, query, fan-out, response, graph, sync, and Action payload bounds;
- protected write/admin endpoints, rate/concurrency limits, and a fail-closed
  missing-secret state;
- atomic Atlas document/snapshot/edge and Writer Project writes;
- project capability, expiry, deletion, retention, and honest durability guard;
- compact typed v21 Action responses and current Knowledge routing;
- durable Render disk blueprint and privacy/auth configuration documentation.

## Verified production baseline before v21 publication

At the beginning of the v21 work, the real GitHub `main` branch and Render API
were still on **v20.0.0 / `ATLAS-OMNI-V20`**. They did not yet contain the v21
security, schema, or deployment changes. That fact is the current production
baseline, not a defect in the local candidate.

## Production release gate still pending

Before calling v21 production-clean, the following must happen:

1. Publish the root-layout runtime files and `render.yaml` to
   `MPROGAMING/backrooms-api`.
2. In Render, attach/confirm the persistent disk at `/var/data` and configure
   `BACKROOMSGPT_ACTION_API_KEY` as a secret.
3. Configure the same secret as Custom GPT Action API-key/Bearer auth and use
   `GPT_ACTIONS/openapi-actions-v21.json`.
4. Confirm production reports `21.0.0` / `BACKROOMSGPT-FINAL-21` and durable
   Atlas storage.
5. Run the read-only and authorized isolated-write production acceptance flow in
   `CONTEXT/RELEASE_GATES.md` / `TESTS/PRE_PUBLISH_ACCEPTANCE_TEST.txt`.

Local tests do not prove external source availability, Render disk attachment,
or Custom GPT builder configuration.
