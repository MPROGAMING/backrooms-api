from __future__ import annotations
import re
from urllib.parse import urljoin,urlparse
from bs4 import BeautifulSoup

class SourceDiscovery:
    def __init__(self,store,network):self.store=store;self.network=network
    @staticmethod
    def classify(host:str):
        h=host.casefold()
        if h.endswith('.fandom.com'):return 'mediawiki-fandom'
        if h.endswith('.wikidot.com'):return 'wikidot'
        return None
    async def scan(self,url:str,max_links:int=250):
        host=urlparse(url).hostname or ''; platform=self.classify(host)
        response,diag=await self.network.get(url,retries=1,allow_404=False)
        if not response:return {'ok':False,'url':url,'candidates':[]}
        soup=BeautifulSoup(response.text,'html.parser'); found={}
        for a in soup.find_all('a',href=True)[:max_links*4]:
            full=urljoin(str(response.url),a['href']); h=urlparse(full).hostname or ''; p=self.classify(h)
            if not p:continue
            score=.5
            txt=(a.get_text(' ',strip=True) or '').casefold()
            if 'backroom' in h or 'backroom' in txt:score+=.35
            if 'wiki' in txt or 'archive' in txt:score+=.1
            found[h]=max(found.get(h,0),score)
        candidates=[]
        for h,score in sorted(found.items(),key=lambda x:x[1],reverse=True):
            row=self.store.upsert_candidate(h,self.classify(h),url,score,{'anchor_discovery':True})
            candidates.append(row)
        return {'ok':True,'seed_url':url,'seed_platform':platform,'candidate_count':len(candidates),'candidates':candidates[:max_links],'diagnostics':[d.to_dict() if hasattr(d,'to_dict') else str(d) for d in diag]}
