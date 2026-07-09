from __future__ import annotations
import asyncio, hashlib, json
from urllib.parse import urlparse
from .common import feature_hash_vector, now_iso, title_key

class AtlasIndexer:
    def __init__(self,store,omni,registry,adapter_for):
        self.store=store;self.omni=omni;self.registry=registry;self.adapter_for=adapter_for
    def _doc_from_page(self,page:dict):
        source_id=page.get('source_id') or page.get('provenance',{}).get('source_id') or 'unknown'
        text=page.get('text',''); title=page.get('title','Untitled'); url=page.get('url','')
        content_hash=hashlib.sha256(text.encode('utf-8',errors='ignore')).hexdigest()
        doc_id=hashlib.sha256(f'{source_id}|{url or title}'.encode()).hexdigest()[:32]
        analysis=page.get('analysis',{})
        return {'doc_id':doc_id,'source_id':source_id,'canon':page.get('canon') or page.get('provenance',{}).get('canon'),'title':title,'url':url,'language':page.get('language','en'),'text':text,'summary':analysis.get('summary',''),'content_hash':content_hash,'vector':feature_hash_vector(title+'\n'+analysis.get('summary','')+'\n'+text[:60000]),'metadata':{'analysis':analysis,'links':page.get('links',[]),'image_urls':page.get('image_urls',[]),'provenance':page.get('provenance',{}),'resolved_via':page.get('resolved_via')},'archived':page.get('archived',False),'fetched_at':page.get('retrieved_at',now_iso())}
    def _edges(self,doc,page):
        out=[]
        for link in page.get('links',[])[:300]:
            url=link.get('url',''); title=link.get('title') or url.rsplit('/',1)[-1]
            key=hashlib.sha256(f"{doc['source_id']}|{url or title}".encode()).hexdigest()[:32]
            out.append({'source_id':doc['source_id'],'to_key':key,'to_title':title,'to_url':url,'relation':'links_to','confidence':1.0})
        signals=page.get('analysis',{}).get('named_signals',{})
        for rel, vals in [('mentions_level',signals.get('level_designations',[])),('mentions_entity',signals.get('entity_designations',[])),('mentions_group',signals.get('groups',[]))]:
            for val in vals[:80]:
                key=f"signal:{rel}:{title_key(val)}"
                out.append({'source_id':doc['source_id'],'to_key':key,'to_title':val,'relation':rel,'confidence':.8})
        return out
    def index_page_payload(self,page:dict):
        doc = self._doc_from_page(page)
        edges = self._edges(doc, page)
        self.store.upsert_document_with_edges(doc, edges, snapshot=True)
        return doc
    async def ingest(self,query:str,scope:str='core',source_ids=None):
        result=await self.omni.resolve_and_fetch(query,scope=scope,source_ids=source_ids,allow_archive_fallback=True)
        doc=self.index_page_payload(result['page'])
        return {'ok':True,'query':query,'indexed':{k:doc[k] for k in ['doc_id','source_id','canon','title','url','content_hash']},'alternatives':result.get('alternatives',[])[:5]}
    async def ingest_source_page(self,source_id:str,page:str):
        source=self.registry.get(source_id); payload=(await self.adapter_for(source).fetch_page(page,max_chars=160000,allow_archive_fallback=True)).to_dict(); doc=self.index_page_payload(payload)
        return {'ok':True,'indexed':{k:doc[k] for k in ['doc_id','source_id','canon','title','url','content_hash']}}
    async def sync_recent(self,scope:str='core',per_source_limit:int=5,total_limit:int=25):
        recent=await self.omni.recent_across_sources(scope=scope,per_source_limit=per_source_limit,total_limit=total_limit)
        hits=recent.get('results',[]); sem=asyncio.Semaphore(6); results=[]
        async def one(hit):
            async with sem:
                try:
                    return await self.ingest_source_page(hit['source_id'],hit['title'])
                except Exception as e:return {'ok':False,'source_id':hit.get('source_id'),'title':hit.get('title'),'error':f'{type(e).__name__}: {e}'}
        results=await asyncio.gather(*(one(h) for h in hits))
        return {'ok':True,'attempted':len(hits),'indexed':sum(1 for r in results if r.get('ok')),'failed':sum(1 for r in results if not r.get('ok')),'results':results}
