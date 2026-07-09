# BackroomsGPT v21 Acceptance Hotfix

This hotfix addresses the concrete issues found by the final acceptance run:

1. The omni resolver can no longer promote a fuzzy candidate such as
   `Baby Partygoers` for the query `Baby Food`.
2. Atlas graph extraction filters image/file links and generic false-positive
   signals such as `Level is`, `Level whose`, and `Entity count`.
3. Wikimedia Commons no longer overrides the gateway's safe
   `Accept-Encoding: identity` policy with gzip. It also retries conservatively,
   handles API errors, and keeps image-only relevance filtering.
4. Offline regression tests cover all three changes.

Files to replace/add:

- atlas/__init__.py
- atlas/indexer.py
- atlas/media.py
- TESTS/test_v21_acceptance_hotfix.py

No secret, Render setting, OpenAPI schema, or GPT Knowledge change is included.

The GitHub connector in this chat returned HTTP 403 for repository write
operations, so these files were prepared as a ready-to-apply replacement
package rather than falsely claiming the repository was updated.
