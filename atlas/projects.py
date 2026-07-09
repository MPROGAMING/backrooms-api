from __future__ import annotations
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

STAGES=['concept','overlap_research','rules','environment','narrative','outline','draft','critique','revision','visuals','technical','publication_check']
PROJECT_RETENTION_DAYS=max(1, min(int(os.getenv('ATLAS_PROJECT_RETENTION_DAYS','30')), 365))

class WriterProjects:
    def __init__(self,store,search_engine):self.store=store;self.search_engine=search_engine
    def create(self,name,project_type='level',target_platform=None,canon_scope=None,brief=''):
        state={'brief':brief,'core_concept':'','mechanism':'','setting':'','emotional_target':'','rules':[],'research':{},'outline':[],'critique_notes':[],'visual_plan':{},'publication_checklist':{}}
        access_token=secrets.token_urlsafe(32)
        expires_at=(datetime.now(timezone.utc)+timedelta(days=PROJECT_RETENTION_DAYS)).isoformat()
        project=self.store.create_project(
            name,
            project_type,
            target_platform,
            canon_scope,
            state,
            hashlib.sha256(access_token.encode()).hexdigest(),
            expires_at,
        )
        return {'project':project,'project_access_token':access_token,'expires_at':expires_at}
    @staticmethod
    def token_hash(access_token):
        return hashlib.sha256((access_token or '').encode()).hexdigest()
    def authorized(self,pid,access_token):
        return self.store.project_token_matches(pid,self.token_hash(access_token))
    def get(self,pid):
        p=self.store.get_project(pid)
        if p:p['events']=self.store.project_events(pid)
        return p
    def update(self,pid,patch,stage=None):return self.store.update_project(pid,stage,patch)
    def advance(self,pid):
        p=self.store.get_project(pid)
        if not p:return None
        i=STAGES.index(p['stage']) if p['stage'] in STAGES else 0
        nxt=STAGES[min(i+1,len(STAGES)-1)]
        return self.store.update_project(pid,nxt,{})
    def overlap_research(self,pid,query,limit=12):
        p=self.store.get_project(pid)
        if not p:return None
        result=self.search_engine.search(query,limit=limit)
        research={'query':query,'indexed_matches':result['results'],'method':result['meta'].get('method')}
        state=p['state']; state['research']=research
        self.store.update_project(pid,'overlap_research',state)
        return {'ok':True,'project_id':pid,'research':research,'note':'Similarity scores rank candidates; they are not proof of duplication.'}
    def checklist(self,pid):
        p=self.store.get_project(pid)
        if not p:return None
        s=p['state']; checks=[
            ('core_concept_defined',bool(s.get('core_concept') or s.get('brief'))),
            ('overlap_research_recorded',bool(s.get('research'))),
            ('rules_recorded',bool(s.get('rules'))),
            ('outline_recorded',bool(s.get('outline'))),
            ('visual_plan_recorded',bool(s.get('visual_plan'))),
        ]
        return {'ok':True,'project_id':pid,'stage':p['stage'],'checks':[{'name':n,'passed':v} for n,v in checks],'passed':sum(v for _,v in checks),'total':len(checks)}
    def delete(self,pid):
        return self.store.delete_project(pid)
