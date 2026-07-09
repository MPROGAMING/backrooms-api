# Codex Start Here

You are continuing the BackroomsGPT FINAL v21 project from an already working
and locally tested codebase.

## Your first job

Do not immediately rewrite the architecture.

First:
1. read `AGENTS.md`;
2. inspect the current code;
3. run all local tests;
4. read the Preview test history;
5. confirm whether v21 has been deployed to Render;
6. if deployed, run or help run the v21 production acceptance checks;
7. only then begin new feature work.

## Current architecture in one sentence

BackroomsGPT is a live, canon-aware Backrooms retrieval gateway with an optional local Atlas research layer for hybrid search, graph relationships, snapshots, diffs, writer projects, media research, source discovery, evals, and telemetry.

## Key separation

There are two truth layers:

### Live source layer
Used for current factual lore:
- Fandom/MediaWiki;
- Wikidot;
- Liminal Archives;
- Freewriting;
- Kane Pixels community documentation;
- international candidates;
- restricted dynamic Fandom/Wikidot.

### Atlas layer
Used for research support:
- indexed documents;
- hybrid ranking;
- similarity discovery;
- snapshots;
- diffs;
- graph edges;
- project overlap research.

Atlas may be incomplete or stale. It must not silently override live source facts.

## What Codex must preserve

- source provenance;
- canon separation;
- explicit archive labeling;
- strict wrong-archive-match rejection;
- compact compare payloads;
- stable operation IDs;
- safe dynamic URL policy;
- local test suite;
- no-AI-publication visual policy for the user's Backrooms wiki work.

## Immediate production validation target

Run these after v21 deployment:
1. Baby Food live Liminal retrieval.
2. Liminal Level 0 returns no unsafe substitute.
3. Atlas targeted ingestion returns success and stores document + edges atomically.
4. Acceptance eval suite returns 4/4.
5. Commons search either returns relevant photo candidates or an honest no-result/source-unavailable state without irrelevant padding.

The local candidate reports `21.0.0`, `BACKROOMSGPT-FINAL-21`, 53 routes, 33
Actions, and 44 passing local tests. Render now serves the v21 runtime, but
production is not release-clean: Atlas reports ephemeral storage and protected
writes remain disabled until the Render Action secret is configured. See
`CONTEXT/RELEASE_GATES.md` and `CONTEXT/DEPLOYMENT_AND_CONFIG.md` for the exact
disk/secret setup and post-deploy checks.
