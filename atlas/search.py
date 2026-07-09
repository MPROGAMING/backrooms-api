from __future__ import annotations
import json, math
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional
from .common import tokenize, feature_hash_vector, cosine_sparse, title_similarity, clamp

class HybridSearchEngine:
    def __init__(self, store): self.store=store
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
        docs=self.store.list_documents(source_ids=source_ids,limit=5000)
        if not docs:return {'ok':True,'query':query,'results':[],'meta':{'indexed_documents':0}}
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
        return {'ok':True,'query':query,'results':rows[:max(1,min(limit,100))],'meta':{'indexed_documents':len(docs),'weights':w,'method':'BM25 + hashed semantic cosine + title similarity'}}
