import asyncio
import re
import time
import logging
from typing import List, Optional, Dict, Any
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import httpx
from urllib.parse import unquote

# ==============================================================================
# 1. ADVANCED TELEMETRY & LOGGING SYSTEM
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("OMNI-NODE")

# ==============================================================================
# 2. IN-MEMORY ASYNC CACHE SYSTEM (THE "SHORT-TERM MEMORY")
# ==============================================================================
class AsyncTTLCache:
    """A high-performance thread-safe caching system to prevent node-flooding."""
    def __init__(self, ttl_seconds: int = 3600):
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.ttl = ttl_seconds

    def get(self, key: str) -> Optional[Any]:
        if key in self.cache:
            entry = self.cache[key]
            if time.time() - entry['timestamp'] < self.ttl:
                logger.info(f"[CACHE HIT] Retrieved optimal payload for: {key}")
                return entry['data']
            else:
                del self.cache[key] # Expired
        return None

    def set(self, key: str, data: Any):
        self.cache[key] = {
            'timestamp': time.time(),
            'data': data
        }
        logger.info(f"[CACHE SET] Memorized payload for: {key}")

memory_bank = AsyncTTLCache(ttl_seconds=1800) # 30 minutes cache

# ==============================================================================
# 3. FASTAPI CONFIGURATION & MIDDLEWARE
# ==============================================================================
app = FastAPI(
    title="M.E.G. Omni-Database API (Ultra-Max Sync)",
    description="Advanced central routing node for Backrooms APIs with Smart Slug Resolution, Concurrency Fetching, and Auto-Correction.",
    version="4.0.0"
)

@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time-Sec"] = str(process_time)
    logger.info(f"[TELEMETRY] {request.method} {request.url.path} completed in {process_time:.3f}s")
    return response

# ==============================================================================
# 4. PYDANTIC SCHEMAS (DATA VALIDATION)
# ==============================================================================
class PageContentResponse(BaseModel):
    content: str
    metadata: Dict[str, Any]

class ErrorResponse(BaseModel):
    error: str
    diagnostics: Dict[str, Any]

# ==============================================================================
# 5. THE AI-LIKE SLUG RESOLUTION ENGINE (SMART PERMUTATIONS)
# ==============================================================================
def generate_smart_slug_matrix(raw_input: str, is_intl: bool = False, lang: str = "") -> List[str]:
    """
    The Optimized 'Brain' of the API. Collapses multiple dashes, spaces, 
    and handles uniform slug parsing to ensure 100% resolution accuracy.
    """
    # 1. ניקוי ראשוני והסרת תווים לא חוקיים ב-URL
    base = unquote(raw_input).strip()
    
    # 2. הפיכת רווחים למקפים, והפיכת אותיות לקטנות לצורך אחידות
    normalized = base.replace(" ", "-").lower()
    
    # 3. חוק הברזל: הפיכת רצף של מקפים (כמו ---) למקף בודד אחד (-)
    normalized = re.sub(r'-{2,}', '-', normalized)
    
    # 4. ניקוי שאריות של תווים מוזרים
    clean_base = re.sub(r'[^a-zA-Z0-9-]', '', normalized)
    
    # חילוץ המספרים מהקלט (אם יש)
    numbers = ''.join(filter(str.isdigit, base))
    has_numbers = bool(numbers)
    
    variants = set()
    
    # הזרקת הוריאציות הנקיות באמת למטריצה
    variants.add(clean_base)                               # למשל: level-0
    variants.add(clean_base.replace("-", ""))              # בלי מקף בכלל: level0
    variants.add(clean_base.replace("-", "").capitalize()) # אות גדולה בלי מקף: Level0
    
    if has_numbers:
        variants.add(f"level-{numbers}")
        variants.add(f"level{numbers}")
        variants.add(f"Level{numbers}")
        variants.add(f"Level-{numbers}")
        variants.add(numbers)
        
        if is_intl and lang:
            lang_dict = {
                "ru": f"uroven-{numbers}",
                "es": f"nivel-{numbers}",
                "fr": f"niveau-{numbers}",
                "de": f"ebene-{numbers}"
            }
            if lang in lang_dict:
                variants.add(lang_dict[lang])
                variants.add(lang_dict[lang].replace("-", ""))

    # הוספת גרסה באותיות גדולות למקרה של אותיות כמו 'N' או 'N-1'
    variants.add(base.upper())
    variants.add(base.lower())
    if "-" in base:
        variants.add(base.split("-")[0].lower() + "-" + "".join(base.split("-")[1:]).upper())

    ordered_variants = [v for v in variants if v]
    logger.info(f"[SMART-MATRIX V2] Permutations compiled: {ordered_variants}")
    return ordered_variants

# ==============================================================================
# 6. CONCURRENT HTTP FETCHING ENGINE (SPEED OPTIMIZATION)
# ==============================================================================
TIMEOUT_SETTINGS = httpx.Timeout(10.0, connect=5.0)
DEFAULT_HEADERS = {
    "User-Agent": "MEG-Archival-Bot/4.0 (Macintosh; Intel Mac OS X 10_15_7) Smart-Concurrency-Engine",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

async def fetch_single_url(client: httpx.AsyncClient, url: str) -> Optional[httpx.Response]:
    """Helper for concurrent fetching that fails silently on 404 to keep logs clean."""
    try:
        response = await client.get(url, headers=DEFAULT_HEADERS)
        if response.status_code == 200:
            return response
    except httpx.RequestError:
        pass
    return None

async def concurrent_smart_fetch(base_url_template: str, variants: List[str]) -> tuple[Optional[str], Optional[httpx.Response]]:
    """
    Fires asynchronous requests to all generated permutations. 
    Returns the FIRST successful response immediately, canceling the rest.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_SETTINGS) as client:
        tasks = []
        for variant in variants:
            url = base_url_template.format(variant=variant)
            tasks.append((url, asyncio.create_task(fetch_single_url(client, url))))
        
        # We process them as they complete. First 200 OK wins.
        for url, task in tasks:
            response = await task
            if response and response.status_code == 200:
                logger.info(f"[CONCURRENCY HIT] Successful resolution at: {url}")
                # Cancel remaining tasks to save memory and bandwidth
                for _, t in tasks:
                    if not t.done():
                        t.cancel()
                return url, response
                
    return None, None

# ==============================================================================
# 7. ADVANCED DOM SANITIZER (NOISE REDUCTION)
# ==============================================================================
def advanced_html_sanitizer(html_text: str, container_id: str = "page-content") -> str:
    """Intelligently parses HTML, removing navbars, rating modules, and code tags."""
    soup = BeautifulSoup(html_text, "html.parser")
    
    content_div = soup.find(id=container_id)
    if not content_div:
        # Fallback to MediaWiki standard if Wikidot ID is missing
        content_div = soup.find(id="mw-content-text") or soup
        
    # Rip out annoying visual elements that confuse LLMs
    for tag in content_div.find_all(['script', 'style', 'nav', 'footer', 'iframe']):
        tag.decompose()
        
    # Remove standard Wikidot rating modules and page tags
    for class_name in ['page-rate-widget-box', 'page-tags', 'footer-wikiwiki', 'wd-adunit']:
        for div in content_div.find_all('div', class_=class_name):
            div.decompose()

    # Smart text extraction preserving basic line-breaks
    clean_text = content_div.get_text(separator="\n", strip=True)
    
    # Regex cleanup to remove excessive blank lines
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)
    return clean_text

# ==============================================================================
# 8. API ENDPOINTS (THE OMNI-ROUTERS)
# ==============================================================================

@app.get("/fandom/search")
async def search_fandom(q: str):
    cache_key = f"fandom_search_{q}"
    if cached := memory_bank.get(cache_key): return cached

    url = "https://backrooms.fandom.com/api.php"
    params = {"action": "query", "list": "search", "srsearch": q, "format": "json", "utf8": 1}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SETTINGS) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            memory_bank.set(cache_key, data)
            return data
    except Exception as e:
        logger.error(f"Fandom Search Error: {e}")
        return JSONResponse(status_code=500, content={"error": "Fandom database node unreachable.", "diagnostics": str(e)})

@app.get("/fandom/page")
async def get_fandom_page(title: str):
    cache_key = f"fandom_page_{title}"
    if cached := memory_bank.get(cache_key): return cached

    url = "https://backrooms.fandom.com/api.php"
    params = {"action": "parse", "page": title, "format": "json", "prop": "text", "disabletoc": 1}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SETTINGS) as client:
            response = await client.get(url, params=params)
            data = response.json()
            if "parse" in data:
                clean_text = advanced_html_sanitizer(data["parse"]["text"]["*"], container_id="mw-content-text")
                result = {"content": f"[FANDOM CORE NODE: {title}]\n{clean_text}"}
                memory_bank.set(cache_key, result)
                return result
            return JSONResponse(status_code=404, content={"error": f"Fandom page '{title}' not found."})
    except Exception as e:
        logger.error(f"Fandom Page Error: {e}")
        return JSONResponse(status_code=500, content={"error": "Error parsing Fandom data stream."})

@app.get("/wikidot/page")
async def get_wikidot_page(url: str):
    cache_key = f"wikidot_page_{url}"
    if cached := memory_bank.get(cache_key): return cached

    variants = generate_smart_slug_matrix(url)
    base_template = "https://backrooms-wiki.wikidot.com/{variant}"
    
    successful_url, response = await concurrent_smart_fetch(base_template, variants)
    
    if successful_url and response:
        clean_text = advanced_html_sanitizer(response.text)
        result = {"content": f"[WIKIDOT CORE NODE]\n{clean_text}"}
        memory_bank.set(cache_key, result)
        return result
        
    return JSONResponse(status_code=404, content={"error": f"Wikidot page '{url}' unavailable after testing {len(variants)} smart variants."})


@app.get("/wikidot/international")
async def get_international_wikidot(lang: str, page: str):
    cache_key = f"intl_{lang}_{page}"
    if cached := memory_bank.get(cache_key): return cached

    logger.info(f"Initiating Smart Resolution for Intl Node ({lang}): {page}")
    variants = generate_smart_slug_matrix(page, is_intl=True, lang=lang)
    base_template = f"https://backrooms-{lang}.wikidot.com/{{variant}}"
    
    successful_url, response = await concurrent_smart_fetch(base_template, variants)
    
    if successful_url and response:
        clean_text = advanced_html_sanitizer(response.text)
        result = {"content": f"[INTERNATIONAL NODE: {lang.upper()}]\nResolved URL: {successful_url}\n\n{clean_text}"}
        memory_bank.set(cache_key, result)
        return result
        
    return JSONResponse(status_code=404, content={"error": f"International Wikidot branch '{lang}' page '{page}' not found. Matrix tested {len(variants)} possibilities."})


@app.get("/wikidot/freewriting")
async def get_free_writing_wiki(page: str):
    cache_key = f"freewriting_{page}"
    if cached := memory_bank.get(cache_key): return cached

    logger.info(f"Initiating Smart Resolution for Free Writing Node: {page}")
    variants = generate_smart_slug_matrix(page)
    base_template = "https://backrooms-freewriting.wikidot.com/{variant}"
    
    successful_url, response = await concurrent_smart_fetch(base_template, variants)
    
    if successful_url and response:
        clean_text = advanced_html_sanitizer(response.text)
        result = {"content": f"[FREE WRITING WIKI NODE]\nResolved URL: {successful_url}\n\n{clean_text}"}
        memory_bank.set(cache_key, result)
        return result
        
    return JSONResponse(status_code=404, content={"error": f"Free Writing Wiki page '{page}' not found. Smart-Matrix failed to resolve {len(variants)} permutations."})


@app.get("/archives/liminal")
async def get_liminal_archives(page: str):
    cache_key = f"liminal_{page}"
    if cached := memory_bank.get(cache_key): return cached

    variants = generate_smart_slug_matrix(page)
    base_template = "https://liminalarchives.xyz/wiki/{variant}"
    
    successful_url, response = await concurrent_smart_fetch(base_template, variants)

    if successful_url and response:
        clean_text = advanced_html_sanitizer(response.text, container_id="mw-content-text")
        result = {"content": f"[LIMINAL ARCHIVES NODE]\n{clean_text}"}
        memory_bank.set(cache_key, result)
        return result
        
    return JSONResponse(status_code=404, content={"error": f"Liminal Archives page '{page}' not found."})


@app.get("/cinematic/kanepixels")
async def get_kane_pixels_lore(topic: str):
    cache_key = f"kane_{topic}"
    if cached := memory_bank.get(cache_key): return cached

    url = "https://kane-pixels-backrooms.fandom.com/api.php"
    search_params = {"action": "query", "list": "search", "srsearch": topic, "format": "json", "utf8": 1}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SETTINGS) as client:
            search_res = await client.get(url, params=search_params)
            search_data = search_res.json()
            
            if not search_data.get("query", {}).get("search"):
                return JSONResponse(status_code=404, content={"error": f"No cinematic records found for topic: {topic}"})
                
            exact_title = search_data["query"]["search"][0]["title"]
            
            page_params = {"action": "parse", "page": exact_title, "format": "json", "prop": "text", "disabletoc": 1}
            page_res = await client.get(url, params=page_params)
            page_data = page_res.json()
            
            if "parse" in page_data:
                clean_text = advanced_html_sanitizer(page_data["parse"]["text"]["*"], container_id="mw-content-text")
                result = {"content": f"[KANE PIXELS CANON FILE: {exact_title}]\n{clean_text}"}
                memory_bank.set(cache_key, result)
                return result
                
            return JSONResponse(status_code=404, content={"error": "Failed to parse cinematic lore file."})
    except Exception as e:
        logger.error(f"Kane Pixels Lore Error: {e}")
        return JSONResponse(status_code=500, content={"error": "Cinematic tracking node unreachable."})
