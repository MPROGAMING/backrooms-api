import asyncio
import re
import time
import logging
import math
import concurrent.futures
import platform
import random
from collections import defaultdict
from typing import List, Optional, Dict, Any, Set
from urllib.parse import unquote

from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import httpx

# ==============================================================================
# 1. ADVANCED TELEMETRY & HARDWARE PROFILING
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | [%(processName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("OMNI-NODE-V12")

def detect_optimal_compute_backend() -> dict:
    """Probes host OS for hardware acceleration and multithreading capabilities."""
    sys_platform = platform.system()
    machine = platform.machine()
    is_apple_silicon = sys_platform == "Darwin" and machine == "arm64"
    
    config = {
        "architecture": "Apple Silicon (M-Series / MPS Optimized)" if is_apple_silicon else f"Cloud CPU ({sys_platform} {machine})",
        "max_workers": 8 if is_apple_silicon else 4,
        "mode": "Ultra-Performance" if is_apple_silicon else "Standard Cloud"
    }
    return config

HARDWARE_PROFILE = detect_optimal_compute_backend()
logger.info(f"BOOT SEQUENCE INITIATED. Architecture: {HARDWARE_PROFILE['architecture']}")
logger.info(f"Allocating NLP Executor Pool with {HARDWARE_PROFILE['max_workers']} workers...")

NLP_EXECUTOR = concurrent.futures.ProcessPoolExecutor(max_workers=HARDWARE_PROFILE["max_workers"])

# ==============================================================================
# 2. NEURAL TTL CACHE (LRU MEMORY MANAGEMENT)
# ==============================================================================
class NeuralTTLCache:
    """Thread-Safe Cache with Least-Recently-Used (LRU) eviction."""
    def __init__(self, ttl_seconds: int = 3600, max_size: int = 2000):
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.ttl = ttl_seconds
        self.max_size = max_size
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            if key in self.cache:
                entry = self.cache[key]
                if time.time() - entry['timestamp'] < self.ttl:
                    entry['hits'] += 1
                    logger.debug(f"[CACHE HIT] Neural pathway restored: {key}")
                    return entry['data']
                else:
                    del self.cache[key]
            return None

    async def set(self, key: str, data: Any):
        async with self._lock:
            if len(self.cache) >= self.max_size:
                lru_key = min(self.cache.keys(), key=lambda k: (self.cache[k]['hits'], self.cache[k]['timestamp']))
                del self.cache[lru_key]
                logger.warning(f"[CACHE EVICT] Purged stale node to free memory: {lru_key}")
                
            self.cache[key] = {
                'timestamp': time.time(),
                'hits': 0,
                'data': data
            }
            logger.info(f"[CACHE SET] Engram encrypted and stored: {key}")

memory_bank = NeuralTTLCache(ttl_seconds=3600, max_size=1500)

# ==============================================================================
# 3. HYPER-RESILIENT NETWORK LAYER & CIRCUIT BREAKER
# ==============================================================================
class CircuitBreaker:
    """Software-based circuit breaker to prevent cascade failures on dead nodes."""
    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 30):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.last_failure_time = 0.0
        self.state = "CLOSED"

    def record_failure(self):
        self.failures += 1
        self.last_failure_time = time.time()
        if self.failures >= self.failure_threshold:
            self.state = "OPEN"
            logger.error("[CIRCUIT BREAKER] Circuit OPENED. Node isolated.")

    def record_success(self):
        self.failures = 0
        self.state = "CLOSED"

    def can_request(self) -> bool:
        if self.state == "CLOSED": return True
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "HALF-OPEN"
                logger.warning("[CIRCUIT BREAKER] Circuit HALF-OPEN. Testing node...")
                return True
            return False
        return True

class ResilientHTTPClient:
    """Manages HTTPX sessions, User-Agent spoofing, and Domain Circuit Breakers."""
    def __init__(self):
        self.timeout = httpx.Timeout(15.0, connect=5.0)
        self.limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
        self.client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, limits=self.limits)
        self.circuit_breakers: Dict[str, CircuitBreaker] = defaultdict(CircuitBreaker)
        self.user_agents = [
            "MEG-Archivist-AI/12.0 (Macintosh; Intel Mac OS X 10_15_7) Neural-Engine",
            "Omni-Node-Crawler/12.0 (Windows NT 10.0; Win64; x64) AI-Sync",
            "Backrooms-Logistics-Bot/12.0 (X11; Linux x86_64) Semantic-Analyzer"
        ]

    def _get_headers(self) -> dict:
        return {
            "User-Agent": random.choice(self.user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }

    def _extract_domain(self, url: str) -> str:
        return url.split("/")[2] if "//" in url else url

    async def get(self, url: str, params: dict = None) -> httpx.Response:
        domain = self._extract_domain(url)
        breaker = self.circuit_breakers[domain]
        
        if not breaker.can_request():
            raise HTTPException(status_code=503, detail=f"Circuit Breaker OPEN for domain: {domain}")

        try:
            response = await self.client.get(url, params=params, headers=self._get_headers())
            response.raise_for_status()
            breaker.record_success()
            return response
        except Exception as e:
            breaker.record_failure()
            raise e

network_core = ResilientHTTPClient()

# ==============================================================================
# 4. PURE PYTHON NATIVE A.I. NLP ENGINE
# ==============================================================================
class BackroomsIntelligenceCore:
    """Mathematical NLP Engine. Zero external ML dependencies."""
    
    STOP_WORDS = {
        "the", "and", "is", "in", "it", "to", "of", "a", "this", "level", 
        "are", "that", "you", "for", "on", "as", "with", "be", "or", "by", 
        "an", "can", "not", "has", "have", "from", "at", "but", "there", "they"
    }

    HAZARD_KEYWORDS = {
        "safe": -3, "secure": -2, "empty": -2, "habitable": -2, "peaceful": -2,
        "danger": 2, "unsafe": 2, "entity": 2, "entities": 2, "anomalous": 1,
        "hostile": 3, "deadly": 4, "death": 5, "run": 4, "trap": 3,
        "unstable": 2, "hazard": 2, "toxic": 3, "lethal": 5, "insanity": 3
    }
    
    REGEX_ENTRANCES = re.compile(r'(?i)(?:enter|entering|entrance|how to enter)(?:[^.!?]*)(?:by|through|when|if)([^.!?]+)')
    REGEX_EXITS = re.compile(r'(?i)(?:exit|exiting|how to leave|leave)(?:[^.!?]*)(?:by|through|when|if)([^.!?]+)')
    REGEX_MEG = re.compile(r'(?i)(?:M\.E\.G\.|MEG)(?:[^.!?]*)(?:Base|Outpost|Camp|Team)([^.!?]+)')

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        words = re.findall(r'\b[a-z]{3,}\b', text.lower())
        return [w for w in words if w not in BackroomsIntelligenceCore.STOP_WORDS]

    @staticmethod
    def classify_danger_index(text: str) -> str:
        tokens = BackroomsIntelligenceCore._tokenize(text)
        danger_score = sum(BackroomsIntelligenceCore.HAZARD_KEYWORDS.get(word, 0) for word in tokens)
        
        if danger_score <= -5: return "Class 0 (Absolute Safety / Secure Zone)"
        if -4 <= danger_score <= 1: return "Class 1 (Habitable / Low Anomaly Count)"
        if 2 <= danger_score <= 8: return "Class 2 (Unsafe / Minor Hostility)"
        if 9 <= danger_score <= 18: return "Class 3 (Dangerous / Moderate Entity Clusters)"
        if 19 <= danger_score <= 35: return "Class 4 (Highly Dangerous / Severe Environmental Hazards)"
        return "Class 5 (Lethal / Immediate Deathzone / Unpredictable)"

    @staticmethod
    def extract_entities(text: str) -> List[str]:
        found = set()
        text_lower = text.lower()
        
        known_beasts = [
            "faceling", "hound", "smiler", "skin-stealer", "skin stealer", 
            "partygoer", "deathmoth", "wretch", "camo crawler", "duller", 
            "clump", "burrower", "bacteria"
        ]
        for beast in known_beasts:
            if beast in text_lower:
                found.add(beast.title() + "s" if not beast.endswith('s') else beast.title())
                
        dynamic = re.findall(r'(?i)\bentity[- ]\d{1,3}\b', text)
        found.update([e.title() for e in dynamic])
        
        return list(found) if found else ["No documented biological anomalies detected."]

    @staticmethod
    def extract_logistics(text: str) -> dict:
        entrances = [m.strip().capitalize() for m in BackroomsIntelligenceCore.REGEX_ENTRANCES.findall(text)][:2]
        exits = [m.strip().capitalize() for m in BackroomsIntelligenceCore.REGEX_EXITS.findall(text)][:2]
        meg_bases = [m.strip().capitalize() for m in BackroomsIntelligenceCore.REGEX_MEG.findall(text)][:2]
        
        return {
            "entrances": entrances if entrances else ["Data Expunged / Unknown Method."],
            "exits": exits if exits else ["Data Expunged / No Known Exit."],
            "outposts": meg_bases if meg_bases else ["No M.E.G. presence confirmed."]
        }

    @staticmethod
    def generate_extractive_summary(text: str, sentence_count: int = 3) -> str:
        sentences = re.split(r'(?<=[.!?]) +', text.replace('\n', ' '))
        sentences = [s.strip() for s in sentences if len(s.split()) > 5]
        
        if len(sentences) <= sentence_count:
            return text

        def sentence_similarity(s1: str, s2: str) -> float:
            w1 = set(BackroomsIntelligenceCore._tokenize(s1))
            w2 = set(BackroomsIntelligenceCore._tokenize(s2))
            if not w1 or not w2: return 0.0
            return len(w1.intersection(w2)) / (math.log10(len(w1)) + math.log10(len(w2)) + 1e-5)

        scores = [0.0] * len(sentences)
        for i in range(len(sentences)):
            for j in range(len(sentences)):
                if i != j:
                    scores[i] += sentence_similarity(sentences[i], sentences[j])

        ranked = sorted(((scores[i], s, i) for i, s in enumerate(sentences)), reverse=True)
        top_sentences = sorted([item for item in ranked[:sentence_count]], key=lambda x: x[2])
        
        return " ".join([item[1] for item in top_sentences])

# ==============================================================================
# 5. ALGORITHMIC SLUG PERMUTATION & ERROR CORRECTION
# ==============================================================================
def levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2): return levenshtein_distance(s2, s1)
    if len(s2) == 0: return len(s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]

def generate_smart_slug_matrix(raw_input: str, is_intl: bool = False, lang: str = "") -> List[str]:
    base = unquote(raw_input).strip()
    
    preserved = base.replace(" ", "-")
    preserved = re.sub(r'-{2,}', '-', preserved)
    preserved = re.sub(r'[^a-zA-Z0-9:-]', '', preserved)
    
    lowered = preserved.lower()
    numbers = ''.join(filter(str.isdigit, base))
    has_numbers = bool(numbers)
    
    variants = set()
    variants.update([
        preserved, lowered, lowered.replace("-", ""), preserved.replace("-", ""),
        lowered.replace("system-", "system:"), preserved.replace("system-", "system:"), base
    ])
    
    if has_numbers:
        variants.update([
            f"level-{numbers}", f"level{numbers}", f"Level{numbers}", 
            f"Level-{numbers}", f"level_{numbers}", numbers
        ])
        if is_intl and lang:
            lang_dict = {
                "ru": f"uroven-{numbers}", "es": f"nivel-{numbers}",
                "fr": f"niveau-{numbers}", "de": f"ebene-{numbers}",
                "it": f"livello-{numbers}", "cn": f"level-{numbers}", "pl": f"poziom-{numbers}"
            }
            if lang in lang_dict:
                variants.update([lang_dict[lang], lang_dict[lang].replace("-", "")])

    variants.update([base.upper(), base.lower()])
    
    ordered_variants = list(dict.fromkeys([v for v in variants if v]))
    logger.info(f"[SLUG-MATRIX] Generated {len(ordered_variants)} quantum vectors for '{raw_input}'")
    return ordered_variants

# ==============================================================================
# 6. FASTAPI CORE & DOM PARSING ENGINE
# ==============================================================================
app = FastAPI(
    title="M.E.G. Omni-Database API (God-Tier AI Sync)",
    description="Maximum-complexity routing node with Multi-threaded NLP, Logistics Extraction, and Smart Redirection.",
    version="12.0.0"
)

@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-AI-Compute-Time-Sec"] = f"{process_time:.4f}"
    response.headers["X-Hardware-Backend"] = HARDWARE_PROFILE["architecture"]
    logger.info(f"[ROUTER] {request.method} {request.url.path} computed in {process_time:.4f}s")
    return response

async def concurrent_smart_fetch(base_url_template: str, variants: List[str]) -> tuple[Optional[str], Optional[httpx.Response]]:
    tasks = []
    for variant in variants:
        url = base_url_template.format(variant=variant)
        tasks.append((url, asyncio.create_task(network_core.get(url))))
    
    for url, task in tasks:
        try:
            response = await task
            if response and response.status_code == 200:
                logger.info(f"[CONCURRENCY HIT] Slug successfully resolved at: {url}")
                for _, t in tasks:
                    if not t.done(): t.cancel()
                return url, response
        except Exception:
            continue
    return None, None

def advanced_html_sanitizer(html_text: str, container_id: str = "page-content") -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    content_div = soup.find(id=container_id) or soup.find(id="mw-content-text") or soup
        
    for tag in content_div.find_all(['script', 'style', 'nav', 'footer', 'iframe', 'table']):
        tag.decompose()
        
    for class_name in ['page-rate-widget-box', 'page-tags', 'footer-wikiwiki', 'wd-adunit', 'toc']:
        for div in content_div.find_all('div', class_=class_name):
            div.decompose()

    clean_text = content_div.get_text(separator="\n", strip=True)
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)
    return clean_text

def build_ai_response(node_name: str, clean_text: str, url: str = None) -> dict:
    logger.info(f"[{node_name}] Executing mathematical NLP heuristics...")
    
    difficulty = BackroomsIntelligenceCore.classify_danger_index(clean_text)
    entities = BackroomsIntelligenceCore.extract_entities(clean_text)
    logistics = BackroomsIntelligenceCore.extract_logistics(clean_text)
    summary = BackroomsIntelligenceCore.generate_extractive_summary(clean_text)
    
    url_str = f"Resolved Node URL: {url}\n" if url else ""
    
    compiled_data = (
        f"[{node_name.upper()}]\n"
        f"{url_str}"
        f"=========================================\n"
        f"| M.E.G. AI TELEMETRY & NLP METADATA    |\n"
        f"=========================================\n"
        f"Threat Level: {difficulty}\n"
        f"Biological Signatures: {', '.join(entities)}\n"
        f"Known Entrances: {', '.join(logistics['entrances'])}\n"
        f"Known Exits: {', '.join(logistics['exits'])}\n"
        f"M.E.G. Presence: {', '.join(logistics['outposts'])}\n"
        f"-----------------------------------------\n"
        f"AI Graph-Summary: {summary}\n"
        f"=========================================\n\n"
        f"[RAW ARCHIVAL TEXT STREAM]\n"
        f"{clean_text}"
    )
    return {"content": compiled_data}

# ==============================================================================
# 7. ROUTING ENDPOINTS (THE DATA INJECTORS)
# ==============================================================================

@app.get("/fandom/search")
async def search_fandom(q: str):
    cache_key = f"fandom_search_{q}"
    if cached := await memory_bank.get(cache_key): return cached
    
    url = "https://backrooms.fandom.com/api.php"
    params = {"action": "query", "list": "search", "srsearch": q, "format": "json", "utf8": 1}
    try:
        response = await network_core.get(url, params=params)
        data = response.json()
        await memory_bank.set(cache_key, data)
        return data
    except Exception as e:
        logger.error(f"Fandom Search Critical Failure: {e}")
        return JSONResponse(status_code=503, content={"error": "Fandom node unresponsive or Circuit OPEN.", "diagnostics": str(e)})

@app.get("/fandom/page")
async def get_fandom_page(title: str):
    cache_key = f"fandom_page_{title}"
    if cached := await memory_bank.get(cache_key): return cached
    
    url = "https://backrooms.fandom.com/api.php"
    params = {"action": "parse", "page": title, "format": "json", "prop": "text", "disabletoc": 1}
    try:
        response = await network_core.get(url, params=params)
        data = response.json()
        if "parse" in data:
            clean_text = advanced_html_sanitizer(data["parse"]["text"]["*"], container_id="mw-content-text")
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(NLP_EXECUTOR, build_ai_response, "FANDOM CORE NODE", clean_text, None)
            await memory_bank.set(cache_key, result)
            return result
        return JSONResponse(status_code=404, content={"error": f"Fandom page '{title}' voided."})
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": "Data stream corrupted.", "diagnostics": str(e)})

@app.get("/wikidot/page")
async def get_wikidot_page(url: str):
    cache_key = f"wikidot_page_{url}"
    if cached := await memory_bank.get(cache_key): return cached
    
    variants = generate_smart_slug_matrix(url)
    base_template = "https://backrooms-wiki.wikidot.com/{variant}"
    
    successful_url, response = await concurrent_smart_fetch(base_template, variants)
    if successful_url and response:
        clean_text = advanced_html_sanitizer(response.text)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(NLP_EXECUTOR, build_ai_response, "WIKIDOT CORE NODE", clean_text, successful_url)
        await memory_bank.set(cache_key, result)
        return result
    return JSONResponse(status_code=404, content={"error": f"Wikidot page '{url}' collapsed into the void."})

@app.get("/wikidot/international")
async def get_international_wikidot(lang: str, page: str):
    cache_key = f"intl_{lang}_{page}"
    if cached := await memory_bank.get(cache_key): return cached
    
    variants = generate_smart_slug_matrix(page, is_intl=True, lang=lang)
    base_template = f"https://backrooms-{lang}.wikidot.com/{{variant}}"
    
    successful_url, response = await concurrent_smart_fetch(base_template, variants)
    if successful_url and response:
        clean_text = advanced_html_sanitizer(response.text)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(NLP_EXECUTOR, build_ai_response, f"INTERNATIONAL NODE: {lang.upper()}", clean_text, successful_url)
        await memory_bank.set(cache_key, result)
        return result
    return JSONResponse(status_code=404, content={"error": f"International Wikidot branch '{lang}' page '{page}' not found."})

@app.get("/wikidot/freewriting")
async def get_free_writing_wiki(page: str):
    """Targeted Fandom integration for the Free Writing Wiki."""
    cache_key = f"freewriting_fandom_{page}"
    if cached := await memory_bank.get(cache_key): return cached
    
    url = "https://backrooms-freewriting.fandom.com/api.php"
    search_term = page.replace("-", " ")
    search_params = {"action": "query", "list": "search", "srsearch": search_term, "format": "json", "utf8": 1}
    
    try:
        search_res = await network_core.get(url, params=search_params)
        search_data = search_res.json()
        
        if not search_data.get("query", {}).get("search"):
            return JSONResponse(status_code=404, content={"error": f"No records found in Free Writing Fandom for: {page}"})
        
        exact_title = search_data["query"]["search"][0]["title"]
        page_params = {"action": "parse", "page": exact_title, "format": "json", "prop": "text", "disabletoc": 1}
        page_res = await network_core.get(url, params=page_params)
        page_data = page_res.json()
        
        if "parse" in page_data:
            clean_text = advanced_html_sanitizer(page_data["parse"]["text"]["*"], container_id="mw-content-text")
            page_url = f"https://backrooms-freewriting.fandom.com/wiki/{exact_title.replace(' ', '_')}"
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(NLP_EXECUTOR, build_ai_response, f"FREE WRITING WIKI (FANDOM): {exact_title}", clean_text, page_url)
            await memory_bank.set(cache_key, result)
            return result
        return JSONResponse(status_code=404, content={"error": "Failed to parse Free Writing Fandom file."})
    except Exception as e:
        logger.error(f"Free Writing Fandom Error: {e}")
        return JSONResponse(status_code=503, content={"error": "Free Writing Fandom tracking node unreachable.", "diagnostics": str(e)})

@app.get("/archives/liminal")
async def get_liminal_archives(page: str):
    cache_key = f"liminal_{page}"
    if cached := await memory_bank.get(cache_key): return cached
    
    variants = generate_smart_slug_matrix(page)
    base_template = "https://liminalarchives.xyz/wiki/{variant}"
    
    successful_url, response = await concurrent_smart_fetch(base_template, variants)
    if successful_url and response:
        clean_text = advanced_html_sanitizer(response.text, container_id="mw-content-text")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(NLP_EXECUTOR, build_ai_response, "LIMINAL ARCHIVES NODE", clean_text, successful_url)
        await memory_bank.set(cache_key, result)
        return result
    return JSONResponse(status_code=404, content={"error": f"Liminal Archives page '{page}' not found."})

@app.get("/cinematic/kanepixels")
async def get_kane_pixels_lore(topic: str):
    cache_key = f"kane_{topic}"
    if cached := await memory_bank.get(cache_key): return cached
    
    url = "https://kane-pixels-backrooms.fandom.com/api.php"
    search_params = {"action": "query", "list": "search", "srsearch": topic, "format": "json", "utf8": 1}
    try:
        response = await network_core.get(url, params=search_params)
        search_data = response.json()
        if not search_data.get("query", {}).get("search"):
            return JSONResponse(status_code=404, content={"error": f"No cinematic records found for topic: {topic}"})
        
        exact_title = search_data["query"]["search"][0]["title"]
        page_params = {"action": "parse", "page": exact_title, "format": "json", "prop": "text", "disabletoc": 1}
        page_res = await network_core.get(url, params=page_params)
        page_data = page_res.json()
        
        if "parse" in page_data:
            clean_text = advanced_html_sanitizer(page_data["parse"]["text"]["*"], container_id="mw-content-text")
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(NLP_EXECUTOR, build_ai_response, f"KANE PIXELS CANON FILE: {exact_title}", clean_text, None)
            await memory_bank.set(cache_key, result)
            return result
        return JSONResponse(status_code=404, content={"error": "Failed to parse cinematic lore file."})
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": "Cinematic tracking node unreachable.", "diagnostics": str(e)})
