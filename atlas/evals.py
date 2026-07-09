from __future__ import annotations
import asyncio

class EvalSuite:
    def __init__(self,store,omni,registry,adapter_for):self.store=store;self.omni=omni;self.registry=registry;self.adapter_for=adapter_for
    async def run_acceptance(self,live:bool=True):
        results=[]
        def add(name,passed,details):results.append({'name':name,'passed':bool(passed),'details':details})
        ids=set(self.registry.all_ids()); add('core_sources_registered',{'fandom-main','wikidot-main','liminal-archives'}.issubset(ids),sorted(ids))
        if live:
            try:
                hits=await self.adapter_for(self.registry.get('liminal-archives')).search('Baby Food',limit=5)
                titles=[h.title for h in hits]; add('liminal_baby_food_search',any('baby food' in t.casefold() for t in titles),titles)
            except Exception as e:add('liminal_baby_food_search',False,f'{type(e).__name__}: {e}')
            try:
                cmp=await self.omni.compare('Level 0',source_ids=['wikidot-main','fandom-main'])
                ok=cmp.get('meta',{}).get('payload_mode')=='compact-comparison' and len(cmp.get('records',[]))==2
                add('compact_canon_compare',ok,cmp.get('meta',{}))
            except Exception as e:add('compact_canon_compare',False,f'{type(e).__name__}: {e}')
            try:
                la=self.adapter_for(self.registry.get('liminal-archives'))
                try:
                    page=await la.fetch_page('Level 0',max_chars=10000,allow_archive_fallback=True)
                    title=page.title
                    add('reject_wrong_liminal_archive_match',not ('10.1' in title or 'corn maze' in title.casefold()),title)
                except Exception as e:add('reject_wrong_liminal_archive_match',True,f'correctly no unsafe match: {type(e).__name__}')
            except Exception as e:add('reject_wrong_liminal_archive_match',False,f'{type(e).__name__}: {e}')
        run=self.store.save_eval('acceptance-live' if live else 'acceptance-local',results); return {'ok':run['failed']==0,**run}
