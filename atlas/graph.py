from __future__ import annotations
from collections import deque
from typing import Dict, List

class KnowledgeGraph:
    def __init__(self,store):self.store=store
    def related(self,doc_id:str,limit:int=30):
        outgoing=self.store.edges_from(doc_id)[:limit]
        incoming=self.store.query('SELECT * FROM edges WHERE to_key=? LIMIT ?',(doc_id,limit))
        return {'ok':True,'doc_id':doc_id,'outgoing':outgoing,'incoming':incoming,'meta':{'outgoing_count':len(outgoing),'incoming_count':len(incoming)}}
    def shortest_path(self,start_doc_id:str,target_doc_id:str,max_depth:int=4):
        q=deque([(start_doc_id,[start_doc_id])]); seen={start_doc_id}
        while q:
            node,path=q.popleft()
            if node==target_doc_id:return {'ok':True,'path':path,'depth':len(path)-1}
            if len(path)-1>=max_depth:continue
            for e in self.store.edges_from(node)[:100]:
                nxt=e['to_key']
                if nxt not in seen:
                    seen.add(nxt);q.append((nxt,path+[nxt]))
        return {'ok':False,'path':[],'depth':None,'message':'No path found within max_depth'}
