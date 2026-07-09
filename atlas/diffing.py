from __future__ import annotations
import difflib
from .common import tokenize

class DiffEngine:
    def __init__(self,store):self.store=store
    def diff_latest(self,doc_id:str,context:int=3):
        snaps=self.store.snapshots(doc_id,limit=3)
        if len(snaps)<2:return {'ok':False,'doc_id':doc_id,'message':'Need at least two snapshots.'}
        new,old=snaps[0],snaps[1]
        a=old['text'].splitlines(); b=new['text'].splitlines()
        diff=list(difflib.unified_diff(a,b,fromfile=old['captured_at'],tofile=new['captured_at'],n=context,lineterm=''))
        old_tokens=set(tokenize(old['text']));new_tokens=set(tokenize(new['text']))
        return {'ok':True,'doc_id':doc_id,'from':old['captured_at'],'to':new['captured_at'],'old_hash':old['content_hash'],'new_hash':new['content_hash'],'added_terms':sorted(new_tokens-old_tokens)[:50],'removed_terms':sorted(old_tokens-new_tokens)[:50],'unified_diff':'\n'.join(diff)[:40000],'truncated':len(diff)>0 and len('\n'.join(diff))>40000}
