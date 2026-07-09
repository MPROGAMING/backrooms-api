"""Focused v21 regression tests for security boundaries and Atlas persistence.

These tests deliberately use mock transports and temporary SQLite files only.
They must not contact a live wiki, Render, GitHub, or Wikimedia Commons.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient


# Keep the standalone test runnable while matching the test key already used by
# the existing security test file.  Production secrets are never read or logged.
TEST_ACTION_KEY = os.getenv("BACKROOMSGPT_ACTION_API_KEY") or "test-action-key"
os.environ["BACKROOMSGPT_ACTION_API_KEY"] = TEST_ACTION_KEY
os.environ.setdefault("ATLAS_DATA_DIR", tempfile.mkdtemp(prefix="backroomsgpt-v21-tests-"))

import main
import atlas.storage as storage_module
from atlas.storage import AtlasStore
from security import URLSafetyError, is_unsafe_ip, validate_discovery_target


ALLOWED_SUFFIXES = (".fandom.com", ".wikidot.com")


class V21NetworkSecurityTests(unittest.TestCase):
    def test_shared_client_requests_identity_encoding(self):
        client = main.ResilientHTTPClient()
        try:
            self.assertEqual(client._headers()["Accept-Encoding"], "identity")
            self.assertEqual(client._headers(json_preferred=True)["Accept-Encoding"], "identity")
        finally:
            asyncio.run(client.close())

    def test_special_and_local_addresses_are_rejected(self):
        for address in (
            "127.0.0.1",
            "10.0.0.7",
            "169.254.169.254",
            "100.64.0.1",
            "::1",
            "fe80::1",
        ):
            with self.subTest(address=address):
                self.assertTrue(is_unsafe_ip(address))

        async def resolver(host, port):
            return [(None, None, None, None, ("100.64.0.1", port))]

        with self.assertRaises(URLSafetyError):
            asyncio.run(
                validate_discovery_target(
                    "https://safe.fandom.com/wiki/test",
                    explicit_hosts=(),
                    allowed_suffixes=ALLOWED_SUFFIXES,
                    resolver=resolver,
                )
            )

    def test_generic_client_validates_redirect_target_before_requesting_it(self):
        async def run_test():
            requested = []
            validated = []

            async def validator(url):
                validated.append(url)
                if urlparse(url).hostname == "169.254.169.254":
                    raise main.UnsafeTarget("redirect target is unsafe")
                return url

            def handler(request):
                requested.append(str(request.url))
                if request.url.host == "safe.fandom.com":
                    return httpx.Response(
                        302,
                        headers={"location": "http://169.254.169.254/latest/meta-data/"},
                        request=request,
                    )
                return httpx.Response(200, content=b"must not be reached", request=request)

            client = main.ResilientHTTPClient()
            await client.client.aclose()
            client.client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                follow_redirects=False,
                trust_env=False,
            )
            client.set_target_validator(validator)
            try:
                with self.assertRaises(main.UnsafeTarget):
                    await client.request("GET", "https://safe.fandom.com/wiki/test", retries=0)
            finally:
                await client.close()

            self.assertEqual(requested, ["https://safe.fandom.com/wiki/test"])
            self.assertEqual(
                validated,
                [
                    "https://safe.fandom.com/wiki/test",
                    "http://169.254.169.254/latest/meta-data/",
                ],
            )

        asyncio.run(run_test())


class V21ActionBoundaryTests(unittest.TestCase):
    @property
    def auth_headers(self):
        return {"Authorization": f"Bearer {TEST_ACTION_KEY}"}

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], code)

    def test_body_limit_and_protected_route_validation_use_safe_envelopes(self):
        oversized = b"x" * (main.MAX_REQUEST_BODY_BYTES + 1)
        with TestClient(main.app) as client:
            response = client.post(
                "/atlas/projects",
                content=oversized,
                headers={"content-type": "application/json"},
            )
            self.assert_error(response, 413, "request_too_large")

            response = client.post("/atlas/projects", json={"name": "test"})
            self.assert_error(response, 401, "auth_required")

            response = client.post("/atlas/projects", json={}, headers=self.auth_headers)
            self.assert_error(response, 422, "validation_failure")

            response = client.post("/atlas/discovery/scan", json={}, headers=self.auth_headers)
            self.assert_error(response, 422, "validation_failure")

    def test_project_capability_is_required_and_is_not_returned_in_project_data(self):
        project_id = None
        access_token = None
        with TestClient(main.app) as client:
            created = client.post(
                "/atlas/projects",
                json={"name": "v21 isolated capability test", "brief": "temporary test project"},
                headers=self.auth_headers,
            )
            self.assertEqual(created.status_code, 200, created.text)
            created_payload = created.json()
            project_id = created_payload["project"]["project_id"]
            access_token = created_payload["project_access_token"]
            self.assertGreaterEqual(len(access_token), 32)
            self.assertNotIn("access_token_hash", created_payload["project"])

            missing_capability = client.get(
                f"/atlas/projects/{project_id}", headers=self.auth_headers
            )
            self.assert_error(missing_capability, 422, "validation_failure")

            wrong_capability = client.get(
                f"/atlas/projects/{project_id}",
                headers={
                    **self.auth_headers,
                    "X-Project-Access-Token": "x" * 48,
                },
            )
            self.assert_error(wrong_capability, 404, "not_found")

            fetched = client.get(
                f"/atlas/projects/{project_id}",
                headers={
                    **self.auth_headers,
                    "X-Project-Access-Token": access_token,
                },
            )
            self.assertEqual(fetched.status_code, 200, fetched.text)
            self.assertNotIn("access_token_hash", fetched.json()["project"])

            updated = client.patch(
                f"/atlas/projects/{project_id}",
                json={
                    "project_access_token": access_token,
                    "patch": {"core_concept": "bounded test concept"},
                    "stage": "rules",
                },
                headers=self.auth_headers,
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(updated.json()["project"]["state"]["core_concept"], "bounded test concept")

            deleted = client.delete(
                f"/atlas/projects/{project_id}",
                headers={
                    **self.auth_headers,
                    "X-Project-Access-Token": access_token,
                },
            )
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertTrue(deleted.json()["deleted"])


class V21AtlasStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="backroomsgpt-v21-storage-")
        self.store = AtlasStore(os.path.join(self.tmpdir.name, "atlas.sqlite3"))

    def tearDown(self):
        connection = getattr(self.store._local, "conn", None)
        if connection is not None:
            connection.close()
        self.tmpdir.cleanup()

    @staticmethod
    def expires_in(days):
        return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    def create_project(self, name="test project"):
        return self.store.create_project(
            name,
            "level",
            None,
            "independent",
            {"brief": "test"},
            "a" * 64,
            self.expires_in(1),
        )

    def test_runtime_connection_enforces_foreign_keys_and_project_delete_cascades(self):
        foreign_keys = self.store._conn().execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(foreign_keys, 1)

        project = self.create_project()
        project_id = project["project_id"]
        self.assertEqual(len(self.store.project_events(project_id)), 1)
        self.assertTrue(self.store.delete_project(project_id))
        self.assertIsNone(self.store.get_project(project_id))
        self.assertEqual(self.store.project_events(project_id), [])

    def test_project_create_and_update_roll_back_when_event_write_path_fails(self):
        original_prune = self.store._prune_project_events

        def fail_prune(*_args):
            raise RuntimeError("forced event-path failure")

        self.store._prune_project_events = fail_prune
        with self.assertRaises(RuntimeError):
            self.create_project("must roll back")
        self.assertEqual(self.store.query("SELECT * FROM projects"), [])

        self.store._prune_project_events = original_prune
        project = self.create_project("stable")
        project_id = project["project_id"]
        before = self.store.get_project(project_id)
        before_events = self.store.project_events(project_id)

        self.store._prune_project_events = fail_prune
        with self.assertRaises(RuntimeError):
            self.store.update_project(project_id, "rules", {"core_concept": "must not persist"})
        after = self.store.get_project(project_id)
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(self.store.project_events(project_id), before_events)

    def test_telemetry_and_expired_projects_are_retained_with_bounds(self):
        original_max = storage_module.MAX_TELEMETRY_ROWS
        storage_module.MAX_TELEMETRY_ROWS = 3
        try:
            for index in range(5):
                self.store.telemetry(f"/test/{index}", 200, index)
            count = self.store.one("SELECT COUNT(*) AS count FROM telemetry")["count"]
            self.assertEqual(count, 3)
        finally:
            storage_module.MAX_TELEMETRY_ROWS = original_max

        expired = self.store.create_project(
            "expired",
            "level",
            None,
            "independent",
            {"brief": "expired"},
            "b" * 64,
            "2000-01-01T00:00:00+00:00",
        )
        self.assertEqual(self.store.purge_expired_projects(), 1)
        self.assertIsNone(self.store.get_project(expired["project_id"]))
        self.assertEqual(self.store.project_events(expired["project_id"]), [])


if __name__ == "__main__":
    unittest.main()
