from __future__ import annotations
import json, math, time
from collections import Counter, OrderedDict
from threading import RLock
from typing import Any, Dict, List, Optional
from .common import tokenize, feature_hash_vector, cosine_sparse, title_similarity, clamp

class HybridSearchEngine:
    def __init__(self, store):
        self.store=store
        self._cache=OrderedDict()
        self._cache_ttl_seconds=60
        self._max_cache_items=128
        self._cache_lock=RLock()

    def _cached(self, key):
        with self._cache_lock:
            item=self._cache.get(key)
            if not item:return None
            expires,value=item
            if expires <= time.monotonic():
                self._cache.pop(key,None); return None
            self._cache.move_to_end(key); return value

    def _cache_result(self, key, value):
        with self._cache_lock:
            self._cache[key]=(time.monotonic()+self._cache_ttl_seconds,value)
            self._cache.move_to_end(key)
            while len(self._cache)>self._max_cache_items:self._cache.popitem(last=False)
    def _bm25(self, docs, query_tokens):
        if not docs or not query_tokens:return {}
        tokenized=[tokenize(d['title']+' '+d.get('summary','')+' '+d['text']) for d in docs]
        N=len(docs); avg=sum(map(len,tokenized))/max(1,N); df=Counter()
        for toks in tokenized:
            for t in set(toks): df[t]+=1
        out={}; k1=1.5; b=.75
        for d,toks in zip(docs,tokenized):
            tf=Counter(toks); score=0.0; L=max(1,len(toks))
            for q in query_tokens:
                if q not in tf:continue
                idf=math.log(1+(N-df[q]+.5)/(df[q]+.5))
                f=tf[q]; score += idf*(f*(k1+1))/(f+k1*(1-b+b*L/max(1,avg)))
            out[d['doc_id']]=score
        mx=max(out.values(),default=1.0) or 1.0
        return {k:v/mx for k,v in out.items()}
    def search(self, query:str, source_ids:Optional[List[str]]=None, limit:int=20, weights:Optional[Dict[str,float]]=None):
        query=(query or '').strip()
        if not query:
            return {'ok':False,'query':query,'results':[],'error':{'code':'validation_failure','message':'query cannot be empty'}}
        if len(query)>500:
            return {'ok':False,'query':query[:500],'results':[],'error':{'code':'validation_failure','message':'query exceeds 500 characters'}}
        normalized_sources=tuple(sorted(set(source_ids or [])))
        bounded_limit=max(1,min(int(limit),50))
        cache_key=(query.casefold(),normalized_sources,bounded_limit)
        cached=self._cached(cache_key)
        if cached is not None:return cached
        docs=self.store.list_documents_for_search(source_ids=list(normalized_sources) or None)
        if not docs:
            result={'ok':True,'query':query,'results':[],'meta':{'indexed_documents':0,'documents_scanned':0,'coverage_limited':False}}
            self._cache_result(cache_key,result); return result
        qtok=tokenize(query); qvec=feature_hash_vector(query); lex=self._bm25(docs,qtok)
        w={'lexical':.45,'semantic':.35,'title':.20}; w.update(weights or {})
        rows=[]
        for d in docs:
            try: dvec={int(k):float(v) for k,v in json.loads(d['vector_json']).items()}
            except Exception:dvec={}
            sem=max(0.0,cosine_sparse(qvec,dvec)); title=title_similarity(query,d['title']); lexical=lex.get(d['doc_id'],0.0)
            score=w['lexical']*lexical+w['semantic']*sem+w['title']*title
            rows.append({'doc_id':d['doc_id'],'source_id':d['source_id'],'canon':d.get('canon'),'title':d['title'],'url':d['url'],'summary':d.get('summary','')[:1000],'archived':bool(d.get('archived')),'score':round(score,5),'score_breakdown':{'lexical':round(lexical,5),'semantic':round(sem,5),'title':round(title,5)}})
        rows.sort(key=lambda r:r['score'],reverse=True)
        result={
            'ok':True,
            'query':query,
            'results':rows[:bounded_limit],
            'meta':{
                'indexed_documents':self.store.document_count(source_ids=list(normalized_sources) or None),
                'documents_scanned':len(docs),
                'coverage_limited':len(docs)>=500,
                'weights':w,
                'method':'BM25 + hashed semantic cosine + title similarity',
                'note':'Scores rank research candidates; they are not proof of duplication.',
            },
        }
        self._cache_result(cache_key,result)
        return result
