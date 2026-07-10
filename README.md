# BackroomsGPT v21 Wikidot Full-URL Hotfix

Replace/add exactly:

- atlas/__init__.py
- TESTS/test_v21_wikidot_full_url.py

The fix:
- preserves the existing resolver identity guard;
- accepts full Wikidot URLs only when the hostname exactly matches the requested registered source;
- extracts and decodes the source-relative path before the existing slug matrix;
- leaves title and slug behavior unchanged;
- preserves namespace paths such as system: and component:;
- rejects cross-source hosts before any network request;
- does not change version, build, schema, Actions, Atlas, Commons, Liminal behavior, or Writer Projects.

The GitHub connector in this chat returned HTTP 403 for both Contents API writes
and Git Data blob creation, so the repository itself could not be changed from
the connector. This package is the exact minimal replacement hotfix.
