# Test History

## v21 final local consolidation

Environment: Python 3.12.13 with `SERVER/requirements.txt` installed in a clean
virtual environment.

```text
Compilation: PASS
Full unittest discovery: 44 tests PASS
Smoke: SMOKE PASS 21.0.0 53 routes
```

Coverage added for v21 includes SSRF/DNS/redirect/body limits, protected write
errors, Writer Project capability/privacy, SQLite foreign keys and atomicity,
Atlas repeat ingest/snapshot/diff/sync behavior, strict Liminal/live/archive
identity, resolver/compare taxonomy, bounded Action payloads, and static Action
schema/route/Knowledge contract checks.

Production validation remains separate and is required after deployment.

## v14 release gate

Result:
PASS.

Key successful behaviors:
- Baby Food found live in Liminal Archives.
- Liminal Level 0 did not resolve to Level 10.1.
- Wikidot and Fandom Level 0 remained separate.
- bus overlap research distinguished structure from shared object.
- critique was specific.
- Wikidot and Fandom syntax were not mixed.
- image policy was respected.
- failures were reported honestly.

## v20 pre-hotfix Preview

Result:
NOT CLEAN.

### Passed
- Baby Food live retrieval.
- no archive fallback for Baby Food.
- Liminal Level 0 no unrelated substitute.
- canon comparison.
- bus research.
- writer project creation.
- overlap research.

### Partial/problematic
- Atlas ingest returned 500, but two documents appeared indexed.
- Commons first query returned 403.
- Commons retry returned irrelevant PDFs/books.
- acceptance eval suite: 3/4.

## Root causes fixed

### Atlas storage
Fixed edge insert mismatch and transactional consistency.

### Baby Food eval
Fixed identity logic to use canonical URL path and require a live non-archived fetch.

### Commons
Added:
- User-Agent;
- serial behavior;
- maxlag;
- MIME filtering;
- relevance ranking;
- query variants.

## Post-hotfix local validation

Latest local command result:

```text
......
----------------------------------------------------------------------
Ran 6 tests in 0.035s

OK
SMOKE PASS 20.0.0 52 routes
```

## Required next test

Full production Preview after deployment.

Do not mark final release clean until the production-focused acceptance checks pass.
