from __future__ import annotations
from typing import Any,Dict,List

class MediaResearch:
    COMMONS_API='https://commons.wikimedia.org/w/api.php'
    def __init__(self,network):self.network=network
    async def search_commons(self,query:str,limit:int=12):
        params={'action':'query','generator':'search','gsrsearch':query,'gsrnamespace':6,'gsrlimit':max(1,min(limit,30)),'prop':'imageinfo','iiprop':'url|extmetadata|mime|size','format':'json','origin':'*'}
        r,diag=await self.network.get(self.COMMONS_API,params=params,json_preferred=True)
        if not r or r.status_code!=200:return {'ok':False,'query':query,'results':[],'diagnostics':[str(d) for d in diag]}
        data=r.json(); pages=data.get('query',{}).get('pages',{}); results=[]
        for p in pages.values():
            ii=(p.get('imageinfo') or [{}])[0]; meta=ii.get('extmetadata',{})
            def val(k):return (meta.get(k) or {}).get('value')
            results.append({'title':p.get('title'),'pageid':p.get('pageid'),'image_url':ii.get('url'),'description_url':ii.get('descriptionurl'),'mime':ii.get('mime'),'width':ii.get('width'),'height':ii.get('height'),'author':val('Artist'),'license_short_name':val('LicenseShortName'),'license_url':val('LicenseUrl'),'usage_terms':val('UsageTerms'),'credit':val('Credit'),'attribution_required':val('AttributionRequired'),'restrictions':val('Restrictions')})
        return {'ok':True,'query':query,'source':'Wikimedia Commons','result_count':len(results),'results':results,'note':'Verify the file description page and license terms before publication.'}
    def catalog(self):
        return {'ok':True,'sources':[{'id':'wikimedia-commons','name':'Wikimedia Commons','live_search':True,'license_metadata':True},{'id':'manual-render','name':'Manual render workflow','live_search':False,'notes':'Use Blender, Roblox Studio, photography, or other non-AI manual creation.'},{'id':'original-photo','name':'Original photography','live_search':False,'notes':'Check people, trademarks, private property, and local photography restrictions.'}]}
