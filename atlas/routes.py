from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

from fastapi import Header, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from security import ActionAuth, FixedWindowRateLimiter

from .dashboard import render_dashboard
from .diffing import DiffEngine
from .discovery import DiscoveryRejected, SourceDiscovery
from .evals import EvalSuite
from .graph import KnowledgeGraph
from .indexer import AtlasIndexer
from .media import MediaResearch
from .projects import STAGES, WriterProjects
from .search import HybridSearchEngine
from .storage import AtlasStore


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IngestRequest(StrictRequestModel):
    query: str = Field(min_length=1, max_length=500)
    scope: Literal["core", "international", "extended", "all"] = "core"
    source_ids: Optional[List[str]] = Field(default=None, max_length=12)
    allow_archive_fallback: bool = False


class IngestPageRequest(StrictRequestModel):
    source_id: str = Field(min_length=1, max_length=120)
    page: str = Field(min_length=1, max_length=500)
    allow_archive_fallback: bool = False


class SyncRecentRequest(StrictRequestModel):
    scope: Literal["core", "international", "extended", "all"] = "core"
    per_source_limit: int = Field(default=5, ge=1, le=10)
    total_limit: int = Field(default=25, ge=1, le=50)


class DiscoveryRequest(StrictRequestModel):
    url: str = Field(min_length=8, max_length=2048)
    max_links: int = Field(default=50, ge=1, le=100)


class CandidateStatusRequest(StrictRequestModel):
    status: Literal["pending", "approved", "rejected"]


class ProjectCreateRequest(StrictRequestModel):
    name: str = Field(min_length=1, max_length=120)
    project_type: str = Field(default="level", min_length=1, max_length=60)
    target_platform: Optional[str] = Field(default=None, max_length=80)
    canon_scope: Optional[str] = Field(default=None, max_length=120)
    brief: str = Field(default="", max_length=8000)


class ProjectUpdateRequest(StrictRequestModel):
    project_access_token: str = Field(min_length=32, max_length=256)
    patch: Dict[str, Any] = Field(default_factory=dict)
    stage: Optional[Literal[
        "concept", "overlap_research", "rules", "environment", "narrative",
        "outline", "draft", "critique", "revision", "visuals", "technical",
        "publication_check",
    ]] = None


class ProjectAdvanceRequest(StrictRequestModel):
    project_access_token: str = Field(min_length=32, max_length=256)


class ProjectOverlapRequest(StrictRequestModel):
    project_access_token: str = Field(min_length=32, max_length=256)
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=12, ge=1, le=20)


class EvalRequest(StrictRequestModel):
    live: bool = True


class AtlasPlatform:
    def __init__(self, store, search, indexer, graph, diff, discovery, media, projects, evals):
        self.store = store
        self.search = search
        self.indexer = indexer
        self.graph = graph
        self.diff = diff
        self.discovery = discovery
        self.media = media
        self.projects = projects
        self.evals = evals


def install_atlas_platform(*, app, registry, omni, adapter_for, network, utc_now_iso, validate_discovery_url):
    store = AtlasStore()
    search = HybridSearchEngine(store)
    indexer = AtlasIndexer(store, omni, registry, adapter_for)
    graph = KnowledgeGraph(store)
    diff = DiffEngine(store)
    discovery = SourceDiscovery(store, network, validate_discovery_url)
    media = MediaResearch(network)
    projects = WriterProjects(store, search)
    evals = EvalSuite(store, omni, registry, adapter_for)
    platform = AtlasPlatform(store, search, indexer, graph, diff, discovery, media, projects, evals)

    action_auth = ActionAuth()
    write_limiter = FixedWindowRateLimiter()
    operation_gates = {
        "atlas-index": asyncio.Semaphore(2),
        "atlas-discovery": asyncio.Semaphore(1),
        "atlas-evals": asyncio.Semaphore(1),
        "atlas-projects": asyncio.Semaphore(4),
    }

    def protected_path(request: Request) -> bool:
        """Identify stateful/admin paths before FastAPI reads a JSON body."""
        path = request.url.path
        method = request.method.upper()
        if path.startswith("/atlas/projects"):
            return True
        if path.startswith("/atlas/index/") and method == "POST":
            return True
        if path.startswith("/atlas/discovery/") and method == "POST":
            return True
        return path == "/atlas/evals/acceptance" and method == "POST"

    @app.middleware("http")
    async def early_write_auth(request: Request, call_next):
        """Fail closed before request-body parsing for every write/admin route.

        Pydantic validation occurs inside FastAPI after it has read a request
        body.  This small middleware checks only a header, so anonymous callers
        cannot force protected endpoints to parse a body up to the global cap.
        The route-level check remains responsible for rate/concurrency limits.
        """
        if protected_path(request):
            if not action_auth.configured:
                return JSONResponse(
                    status_code=503,
                    content={
                        "ok": False,
                        "error": {
                            "code": "service_unavailable",
                            "message": "Protected Atlas operations are disabled until BACKROOMSGPT_ACTION_API_KEY is configured.",
                            "retryable": False,
                        },
                    },
                )
            if not action_auth.authorized(request.headers.get("authorization")):
                return JSONResponse(
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                    content={
                        "ok": False,
                        "error": {
                            "code": "auth_required",
                            "message": "A valid Bearer token is required for this operation.",
                            "retryable": False,
                        },
                    },
                )
        return await call_next(request)

    def require_protected_action(request: Request, action: str, limit: int = 20):
        """Authenticate a shared GPT service key without claiming user identity."""
        if not action_auth.configured:
            raise HTTPException(
                503,
                "Protected Atlas operations are disabled until BACKROOMSGPT_ACTION_API_KEY is configured.",
            )
        if not action_auth.authorized(request.headers.get("authorization")):
            raise HTTPException(
                401,
                "A valid Bearer token is required for this operation.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        client = request.client.host if request.client else "unknown"
        # ChatGPT Actions use one service key, so client IP is only a best-effort
        # signal. Apply both a global operation ceiling and a client bucket.
        if not write_limiter.allow(f"{action}:global", limit=max(3, limit * 4), window_seconds=60):
            raise HTTPException(429, "Protected operation is temporarily busy.", headers={"Retry-After": "60"})
        if not write_limiter.allow(f"{action}:client:{client}", limit=limit, window_seconds=60):
            raise HTTPException(429, "Protected operation rate limit exceeded. Try again shortly.", headers={"Retry-After": "60"})

    @asynccontextmanager
    async def limited_operation(name: str):
        gate = operation_gates[name]
        try:
            await asyncio.wait_for(gate.acquire(), timeout=0.05)
        except TimeoutError as exc:
            raise HTTPException(429, "Operation concurrency limit reached. Try again shortly.", headers={"Retry-After": "5"}) from exc
        try:
            yield
        finally:
            gate.release()

    def require_project_access(request: Request, project_id: str, project_access_token: str):
        require_protected_action(request, "atlas-projects", limit=20)
        if not projects.authorized(project_id, project_access_token):
            # Do not disclose whether a project ID exists without its capability.
            raise HTTPException(404, "Project not found or access capability is invalid.")

    def require_durable_project_storage() -> None:
        """Do not accept new drafts when the deployment explicitly requires durability.

        Local development keeps this opt-in so the test suite can use temporary
        SQLite.  The production Render blueprint enables the guard and mounts a
        disk; if that disk is not actually configured, persistent writer-project
        writes fail closed rather than silently disappearing on redeploy.
        """
        requires_durable = os.getenv("ATLAS_REQUIRE_DURABLE_PROJECT_STORAGE", "false").casefold() == "true"
        storage_mode = os.getenv("ATLAS_PERSISTENCE_MODE", "ephemeral").casefold()
        if requires_durable and storage_mode != "durable":
            raise HTTPException(
                503,
                "Writer Projects are disabled until durable Atlas storage is configured.",
            )

    def ensure_patch_size(patch: Dict[str, Any]) -> None:
        if len(json.dumps(patch, ensure_ascii=False).encode("utf-8")) > 32_768:
            raise HTTPException(422, "Project patch exceeds the 32 KiB limit.")

    @app.middleware("http")
    async def atlas_telemetry(request: Request, call_next):
        started = time.monotonic()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            # Telemetry is bounded and written off the async request path. It
            # contains endpoint/status/latency only, never headers or bodies.
            if request.url.path not in {"/health", "/docs", "/openapi.json"}:
                status = getattr(response, "status_code", 500)
                elapsed = int((time.monotonic() - started) * 1000)
                async def save_telemetry():
                    try:
                        await asyncio.to_thread(store.telemetry, request.url.path, status, elapsed)
                    except Exception:
                        pass
                asyncio.create_task(save_telemetry())

    @app.get("/privacy", response_class=HTMLResponse, tags=["Atlas System"])
    async def privacy():
        return '''<html><body><h1>BackroomsGPT API Privacy Policy</h1><p>The API processes query parameters and public wiki URLs to retrieve public Backrooms lore. It does not require user accounts and does not intentionally collect ChatGPT conversation text. Operational telemetry stores endpoint path, status code, latency, and timestamp; it does not store request bodies, Authorization headers, or Action secrets.</p><p>Atlas indexing stores selected public wiki content, snapshots, and graph data. Writer Projects are protected by the service Action credential and a per-project access capability. They are not user accounts: anyone who receives a project capability can access that project. Project data is retained for the configured period and can be deleted with its capability. Do not submit confidential material. Durable storage depends on the deployed Render persistent-disk configuration; when no disk is attached, Atlas state may reset on restart or redeploy.</p><p>External source requests contact the relevant public wiki or Wikimedia service to retrieve requested public material. Archive results are labeled as archived. Contact the Builder Profile for privacy questions.</p><p>Last updated: 2026-07-09.</p></body></html>'''

    @app.get("/atlas/stats", tags=["Atlas Index"])
    async def atlas_stats():
        return {"ok": True, "version": "21.0.0", "stats": await asyncio.to_thread(store.stats)}

    @app.get("/atlas/dashboard", response_class=HTMLResponse, tags=["Atlas System"])
    async def dashboard():
        return render_dashboard(await asyncio.to_thread(store.stats))

    @app.get("/atlas/index/search", tags=["Atlas Index"])
    async def index_search(
        q: str = Query(min_length=1, max_length=500),
        source_ids: Optional[List[str]] = Query(default=None, max_length=12),
        limit: int = Query(default=20, ge=1, le=50),
    ):
        return await asyncio.to_thread(search.search, q, source_ids, limit)

    @app.post("/atlas/index/ingest", tags=["Atlas Index"])
    async def index_ingest(request: Request, payload: IngestRequest):
        require_protected_action(request, "atlas-index", limit=10)
        async with limited_operation("atlas-index"):
            return await indexer.ingest(
                payload.query,
                scope=payload.scope,
                source_ids=payload.source_ids,
                allow_archive_fallback=payload.allow_archive_fallback,
            )

    @app.post("/atlas/index/ingest-page", tags=["Atlas Index"])
    async def index_ingest_page(request: Request, payload: IngestPageRequest):
        require_protected_action(request, "atlas-index", limit=10)
        async with limited_operation("atlas-index"):
            return await indexer.ingest_source_page(
                payload.source_id,
                payload.page,
                allow_archive_fallback=payload.allow_archive_fallback,
            )

    @app.post("/atlas/index/sync-recent", tags=["Atlas Index"])
    async def sync_recent(request: Request, payload: SyncRecentRequest):
        require_protected_action(request, "atlas-index", limit=3)
        async with limited_operation("atlas-index"):
            return await indexer.sync_recent(
                scope=payload.scope,
                per_source_limit=payload.per_source_limit,
                total_limit=payload.total_limit,
            )

    @app.get("/atlas/diff/{doc_id}", tags=["Atlas Diff"])
    async def diff_latest(
        doc_id: str = Path(min_length=1, max_length=64),
        context: int = Query(default=3, ge=0, le=8),
    ):
        return await asyncio.to_thread(diff.diff_latest, doc_id, context)

    @app.get("/atlas/graph/related/{doc_id}", tags=["Atlas Graph"])
    async def graph_related(
        doc_id: str = Path(min_length=1, max_length=64),
        limit: int = Query(default=30, ge=1, le=60),
    ):
        return await asyncio.to_thread(graph.related, doc_id, limit)

    @app.get("/atlas/graph/path", tags=["Atlas Graph"])
    async def graph_path(
        start_doc_id: str = Query(min_length=1, max_length=64),
        target_doc_id: str = Query(min_length=1, max_length=64),
        max_depth: int = Query(default=4, ge=1, le=6),
    ):
        return await asyncio.to_thread(graph.shortest_path, start_doc_id, target_doc_id, max_depth)

    @app.post("/atlas/discovery/scan", tags=["Atlas Discovery"])
    async def discovery_scan(request: Request, payload: DiscoveryRequest):
        require_protected_action(request, "atlas-discovery", limit=3)
        async with limited_operation("atlas-discovery"):
            try:
                return await discovery.scan(payload.url, payload.max_links)
            except DiscoveryRejected as exc:
                raise HTTPException(400, str(exc)) from exc

    @app.get("/atlas/discovery/candidates", tags=["Atlas Discovery"])
    async def discovery_candidates(status: Optional[Literal["pending", "approved", "rejected"]] = None):
        return {"ok": True, "results": await asyncio.to_thread(store.candidates, status)}

    @app.post("/atlas/discovery/candidates/{candidate_id}/probe", tags=["Atlas Discovery"])
    async def discovery_candidate_probe(
        request: Request,
        candidate_id: str = Path(min_length=1, max_length=64),
    ):
        require_protected_action(request, "atlas-discovery", limit=3)
        candidate = await asyncio.to_thread(store.candidate, candidate_id)
        if not candidate:
            raise HTTPException(404, "Discovery candidate not found.")
        async with limited_operation("atlas-discovery"):
            try:
                return await discovery.probe_candidate(candidate)
            except DiscoveryRejected as exc:
                raise HTTPException(400, str(exc)) from exc

    @app.post("/atlas/discovery/candidates/{candidate_id}/status", tags=["Atlas Discovery"])
    async def discovery_status(
        request: Request,
        payload: CandidateStatusRequest,
        candidate_id: str = Path(min_length=1, max_length=64),
    ):
        require_protected_action(request, "atlas-discovery-status", limit=5)
        candidate = await asyncio.to_thread(store.set_candidate_status, candidate_id, payload.status)
        if not candidate:
            raise HTTPException(404, "Discovery candidate not found.")
        return {"ok": True, "candidate": candidate}

    @app.get("/atlas/media/catalog", tags=["Atlas Media"])
    async def media_catalog():
        return media.catalog()

    @app.get("/atlas/media/commons/search", tags=["Atlas Media"])
    async def media_commons(q: str = Query(min_length=1, max_length=500), limit: int = Query(default=12, ge=1, le=30)):
        result = await media.search_commons(q, limit)
        if result.get("error", {}).get("code") == "source_unavailable":
            raise HTTPException(503, result["error"]["message"])
        return result

    @app.post("/atlas/projects", tags=["Atlas Projects"])
    async def project_create(request: Request, payload: ProjectCreateRequest):
        require_protected_action(request, "atlas-projects", limit=6)
        require_durable_project_storage()
        async with limited_operation("atlas-projects"):
            await asyncio.to_thread(store.purge_expired_projects)
            created = await asyncio.to_thread(
                projects.create,
                payload.name,
                payload.project_type,
                payload.target_platform,
                payload.canon_scope,
                payload.brief,
            )
        return {
            "ok": True,
            "project": created["project"],
            "project_access_token": created["project_access_token"],
            "expires_at": created["expires_at"],
            "access_note": "Keep this capability private. It is required for later project actions and is not a user account credential.",
        }

    @app.get("/atlas/projects/{project_id}", tags=["Atlas Projects"])
    async def project_get(
        request: Request,
        project_token: str = Header(alias="X-Project-Access-Token", min_length=32, max_length=256),
        project_id: str = Path(min_length=1, max_length=64),
    ):
        require_project_access(request, project_id, project_token)
        project = await asyncio.to_thread(projects.get, project_id)
        if not project:
            raise HTTPException(404, "Project not found or access capability is invalid.")
        return {"ok": True, "project": project}

    @app.patch("/atlas/projects/{project_id}", tags=["Atlas Projects"])
    async def project_update(
        request: Request,
        payload: ProjectUpdateRequest,
        project_id: str = Path(min_length=1, max_length=64),
    ):
        require_project_access(request, project_id, payload.project_access_token)
        require_durable_project_storage()
        ensure_patch_size(payload.patch)
        async with limited_operation("atlas-projects"):
            project = await asyncio.to_thread(projects.update, project_id, payload.patch, payload.stage)
        if not project:
            raise HTTPException(404, "Project not found or access capability is invalid.")
        return {"ok": True, "project": project}

    @app.post("/atlas/projects/{project_id}/advance", tags=["Atlas Projects"])
    async def project_advance(
        request: Request,
        payload: ProjectAdvanceRequest,
        project_id: str = Path(min_length=1, max_length=64),
    ):
        require_project_access(request, project_id, payload.project_access_token)
        require_durable_project_storage()
        async with limited_operation("atlas-projects"):
            project = await asyncio.to_thread(projects.advance, project_id)
        if not project:
            raise HTTPException(404, "Project not found or access capability is invalid.")
        return {"ok": True, "project": project}

    @app.post("/atlas/projects/{project_id}/overlap-research", tags=["Atlas Projects"])
    async def project_overlap(
        request: Request,
        payload: ProjectOverlapRequest,
        project_id: str = Path(min_length=1, max_length=64),
    ):
        require_project_access(request, project_id, payload.project_access_token)
        require_durable_project_storage()
        async with limited_operation("atlas-projects"):
            result = await asyncio.to_thread(projects.overlap_research, project_id, payload.query, payload.limit)
        if not result:
            raise HTTPException(404, "Project not found or access capability is invalid.")
        return result

    @app.get("/atlas/projects/{project_id}/checklist", tags=["Atlas Projects"])
    async def project_checklist(
        request: Request,
        project_token: str = Header(alias="X-Project-Access-Token", min_length=32, max_length=256),
        project_id: str = Path(min_length=1, max_length=64),
    ):
        require_project_access(request, project_id, project_token)
        result = await asyncio.to_thread(projects.checklist, project_id)
        if not result:
            raise HTTPException(404, "Project not found or access capability is invalid.")
        return result

    @app.delete("/atlas/projects/{project_id}", tags=["Atlas Projects"])
    async def project_delete(
        request: Request,
        project_token: str = Header(alias="X-Project-Access-Token", min_length=32, max_length=256),
        project_id: str = Path(min_length=1, max_length=64),
    ):
        require_project_access(request, project_id, project_token)
        deleted = await asyncio.to_thread(projects.delete, project_id)
        return {"ok": True, "project_id": project_id, "deleted": deleted}

    @app.post("/atlas/evals/acceptance", tags=["Atlas Evals"])
    async def eval_acceptance(request: Request, payload: EvalRequest):
        require_protected_action(request, "atlas-evals", limit=2)
        async with limited_operation("atlas-evals"):
            return await evals.run_acceptance(payload.live)

    return platform
