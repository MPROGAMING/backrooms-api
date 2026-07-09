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
