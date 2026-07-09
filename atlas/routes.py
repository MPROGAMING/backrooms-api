from __future__ import annotations
import time
from typing import Any,Dict,List,Optional
from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from .storage import AtlasStore
from .search import HybridSearchEngine
from .indexer import AtlasIndexer
from .graph import KnowledgeGraph
from .diffing import DiffEngine
from .discovery import SourceDiscovery
from .media import MediaResearch
from .projects import WriterProjects
from .evals import EvalSuite
from .dashboard import render_dashboard

class AtlasPlatform:
    def __init__(self,store,search,indexer,graph,diff,discovery,media,projects,evals):
        self.store=store;self.search=search;self.indexer=indexer;self.graph=graph;self.diff=diff;self.discovery=discovery;self.media=media;self.projects=projects;self.evals=evals

def install_atlas_platform(*,app,registry,omni,adapter_for,network,utc_now_iso):
    store=AtlasStore(); search=HybridSearchEngine(store); indexer=AtlasIndexer(store,omni,registry,adapter_for); graph=KnowledgeGraph(store); diff=DiffEngine(store); discovery=SourceDiscovery(store,network); media=MediaResearch(network); projects=WriterProjects(store,search); evals=EvalSuite(store,omni,registry,adapter_for)
    platform=AtlasPlatform(store,search,indexer,graph,diff,discovery,media,projects,evals)

    @app.middleware('http')
    async def atlas_telemetry(request:Request,call_next):
        started=time.monotonic()
        try:
            response=await call_next(request); return response
        finally:
            elapsed=int((time.monotonic()-started)*1000)
            try: store.telemetry(request.url.path, getattr(locals().get('response',None),'status_code',500), elapsed)
            except Exception: pass

    @app.get('/privacy',response_class=HTMLResponse,tags=['Atlas System'])
    async def privacy():
        return '''<html><body><h1>BackroomsGPT API Privacy Policy</h1><p>The API processes query parameters and public wiki URLs to retrieve public Backrooms lore. It does not require user accounts and does not intentionally collect personal data. Operational telemetry stores endpoint path, status code, latency, and timestamp; it does not store ChatGPT conversation text. Indexed public wiki content and writer projects explicitly submitted to project endpoints may be stored in the configured Atlas database. Do not submit private or confidential drafts to public project endpoints. Data on ephemeral hosting may be reset. Contact the service operator through the Builder Profile for deletion or privacy requests.</p><p>Last updated: 2026-07-09.</p></body></html>'''

    @app.get('/atlas/stats',tags=['Atlas Index'])
    async def atlas_stats(): return {'ok':True,'version':'20.0.0','stats':store.stats()}

    @app.get('/atlas/dashboard',response_class=HTMLResponse,tags=['Atlas System'])
    async def dashboard(): return render_dashboard(store.stats())

    @app.get('/atlas/index/search',tags=['Atlas Index'])
    async def index_search(q:str,source_ids:Optional[List[str]]=Query(None),limit:int=20): return search.search(q,source_ids=source_ids,limit=limit)

    @app.post('/atlas/index/ingest',tags=['Atlas Index'])
    async def index_ingest(payload:Dict[str,Any]=Body(...)):
        return await indexer.ingest(payload.get('query',''),scope=payload.get('scope','core'),source_ids=payload.get('source_ids'))

    @app.post('/atlas/index/ingest-page',tags=['Atlas Index'])
    async def index_ingest_page(payload:Dict[str,Any]=Body(...)):
        return await indexer.ingest_source_page(payload['source_id'],payload['page'])

    @app.post('/atlas/index/sync-recent',tags=['Atlas Index'])
    async def sync_recent(payload:Dict[str,Any]=Body(default={})):
        return await indexer.sync_recent(scope=payload.get('scope','core'),per_source_limit=int(payload.get('per_source_limit',5)),total_limit=int(payload.get('total_limit',25)))

    @app.get('/atlas/diff/{doc_id}',tags=['Atlas Diff'])
    async def diff_latest(doc_id:str): return diff.diff_latest(doc_id)

    @app.get('/atlas/graph/related/{doc_id}',tags=['Atlas Graph'])
    async def graph_related(doc_id:str,limit:int=30): return graph.related(doc_id,limit)

    @app.get('/atlas/graph/path',tags=['Atlas Graph'])
    async def graph_path(start_doc_id:str,target_doc_id:str,max_depth:int=4): return graph.shortest_path(start_doc_id,target_doc_id,max_depth)

    @app.post('/atlas/discovery/scan',tags=['Atlas Discovery'])
    async def discovery_scan(payload:Dict[str,Any]=Body(...)): return await discovery.scan(payload['url'],int(payload.get('max_links',100)))

    @app.get('/atlas/discovery/candidates',tags=['Atlas Discovery'])
    async def discovery_candidates(status:Optional[str]=None): return {'ok':True,'results':store.candidates(status)}

    @app.post('/atlas/discovery/candidates/{candidate_id}/status',tags=['Atlas Discovery'])
    async def discovery_status(candidate_id:str,payload:Dict[str,Any]=Body(...)):
        status=payload.get('status');
        if status not in {'pending','approved','rejected'}: raise HTTPException(400,'status must be pending, approved, or rejected')
        return {'ok':True,'candidate':store.set_candidate_status(candidate_id,status)}

    @app.get('/atlas/media/catalog',tags=['Atlas Media'])
    async def media_catalog(): return media.catalog()

    @app.get('/atlas/media/commons/search',tags=['Atlas Media'])
    async def media_commons(q:str,limit:int=12): return await media.search_commons(q,limit)

    @app.post('/atlas/projects',tags=['Atlas Projects'])
    async def project_create(payload:Dict[str,Any]=Body(...)):
        return {'ok':True,'project':projects.create(payload['name'],payload.get('project_type','level'),payload.get('target_platform'),payload.get('canon_scope'),payload.get('brief',''))}

    @app.get('/atlas/projects/{project_id}',tags=['Atlas Projects'])
    async def project_get(project_id:str):
        p=projects.get(project_id)
        if not p: raise HTTPException(404,'project not found')
        return {'ok':True,'project':p}

    @app.patch('/atlas/projects/{project_id}',tags=['Atlas Projects'])
    async def project_update(project_id:str,payload:Dict[str,Any]=Body(...)):
        p=projects.update(project_id,payload.get('patch',{}),payload.get('stage'))
        if not p: raise HTTPException(404,'project not found')
        return {'ok':True,'project':p}

    @app.post('/atlas/projects/{project_id}/advance',tags=['Atlas Projects'])
    async def project_advance(project_id:str):
        p=projects.advance(project_id)
        if not p: raise HTTPException(404,'project not found')
        return {'ok':True,'project':p}

    @app.post('/atlas/projects/{project_id}/overlap-research',tags=['Atlas Projects'])
    async def project_overlap(project_id:str,payload:Dict[str,Any]=Body(...)):
        r=projects.overlap_research(project_id,payload['query'],int(payload.get('limit',12)))
        if not r: raise HTTPException(404,'project not found')
        return r

    @app.get('/atlas/projects/{project_id}/checklist',tags=['Atlas Projects'])
    async def project_checklist(project_id:str):
        r=projects.checklist(project_id)
        if not r: raise HTTPException(404,'project not found')
        return r

    @app.post('/atlas/evals/acceptance',tags=['Atlas Evals'])
    async def eval_acceptance(payload:Dict[str,Any]=Body(default={})):
        return await evals.run_acceptance(bool(payload.get('live',True)))

    return platform
