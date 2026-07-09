import asyncio
import re
import time
import logging
import math
import concurrent.futures
from collections import defaultdict, Counter
from typing import List, Optional, Dict, Any, Tuple, Set
from urllib.parse import unquote
import platform

from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import httpx

# ==============================================================================
# 1. ADVANCED TELEMETRY & HARDWARE DETECTION
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | [%(processName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("OMNI-AI-CORE")

def detect_optimal_compute_backend() -> str:
    """Detects available hardware for local execution optimizations."""
    sys_platform = platform.system()
    machine = platform.machine()
    if sys_platform == "Darwin" and machine == "arm64":
        return "Apple Silicon (MPS Optimized)"
    return "Standard Cloud CPU (Render Mode)"

COMPUTE_BACKEND = detect_optimal_compute_backend()
logger.info(f"Initialized AI Omni-Node targeting architecture: {COMPUTE_BACKEND}")

# ==============================================================================
# 2. IN-MEMORY VECTOR-LIKE ASYNC CACHE
# ==============================================================================
class NeuralTTLCache:
    """
    Advanced Thread-Safe Caching System.
    Uses LRU (Least Recently Used) eviction heuristics when memory gets bloated.
    """
    def __init__(self, ttl_seconds: int = 3600, max_size: int = 1000):
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
                    logger.info(f"[CACHE HIT] Restored neural mapping for: {key}")
                    return entry['data']
                else:
                    del self.cache[key]
            return None

    async def set(self, key: str, data: Any):
        async with self._lock:
            if len(self.cache) >= self.max_size:
                # LRU Eviction: Remove item with fewest hits and oldest timestamp
                lru_key = min(self.cache.keys(), key=lambda k: (self.cache[k]['hits'], self.cache[k]['timestamp']))
                del self.cache[lru_key]
                
            self.cache[key] = {
                'timestamp': time.time(),
                'hits': 0,
                'data': data
            }
            logger.info(f"[CACHE SET] Engram memorized for: {key}")

memory_bank = NeuralTTLCache(ttl_seconds=1800, max_size=500)

# ==============================================================================
# 3. PURE PYTHON NATIVE A.I. NLP ENGINE (NO HEAVY ML DEPENDENCIES)
# ==============================================================================
class BackroomsIntelligenceCore:
    """
    A lightweight, mathematics-based NLP engine written from scratch.
    Calculates Page Summaries using TextRank (Graph Theory), 
    and extracts Survival Hazards using semantic TF-IDF concepts.
    """
    
    STOP_WORDS = {
        "the", "and", "is", "in", "it", "to", "of", "a", "this", "level", 
        "are", "that", "you", "for", "on", "as", "with", "be", "or", "by", 
        "an", "can", "not", "has", "have", "from", "at", "but", "there"
    }

    HAZARD_KEYWORDS = {
        "safe": -2, "secure": -2, "empty": -1, "habitable": -1,
        "danger": 2, "unsafe": 2, "entity": 2, "entities": 2, 
        "hostile": 3, "deadly": 4, "death": 4, "run": 3,
        "unstable": 2, "hazard": 2, "toxic": 3, "lethal": 4
    }

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        words = re.findall(r'\b[a-z]{3,}\b', text.lower())
        return [w for w in words if w not in BackroomsIntelligenceCore.STOP_WORDS]

    @staticmethod
    def extract_survival_difficulty(text: str) -> str:
        """Heuristic sentiment analysis to classify Backrooms Danger Levels."""
        tokens = BackroomsIntelligenceCore._tokenize(text)
        danger_score = sum(BackroomsIntelligenceCore.HAZARD_KEYWORDS.get(word, 0) for word in tokens)
        
        if danger_score <= -3: return "Class 0 (Safe / Secure)"
        if -2 <= danger_score <= 1: return "Class 1 (Habitable / Low Danger)"
        if 2 <= danger_score <= 6: return "Class 2 (Unsafe / Minor Entity Count)"
        if 7 <= danger_score <= 12: return "Class 3 (Dangerous / Moderate Entities)"
        if 13 <= danger_score <= 20: return "Class 4 (Highly Dangerous / Severe Hazards)"
        if danger_score > 20: return "Class 5 (Lethal / Environmental Deathzone)"
        return "Class Undetermined (Variable Properties)"

    @staticmethod
    def extract_entities(text: str) -> List[str]:
        """Pattern recognition for known Entity naming conventions."""
        found = []
        if "faceling" in text.lower(): found.append("Facelings")
        if "hound" in text.lower(): found.append("Hounds")
        if "smiler" in text.lower(): found.append("Smilers")
        if "skin-stealer" in text.lower() or "skin stealer" in text.lower(): found.append("Skin-Stealers")
        if "partygoer" in text.lower(): found.append("Partygoers")
        if "deathmoth" in text.lower(): found.append("Deathmoths")
        if "wretch" in text.lower(): found.append("Wretches")
        
        # Regex for standard Entity designations (e.g., Entity 14, Entity-14)
        dynamic_entities = re.findall(r'(?i)\bentity[- ]\d{1,3}\b', text)
        found.extend([e.title() for e in set(dynamic_entities)])
        
        return list(set(found)) if found else ["No documented entity clusters detected."]

    @staticmethod
    def generate_extractive_summary(text: str, sentence_count: int = 3) -> str:
        """
        Pure-Python TextRank Algorithm.
        Builds a graph of sentences, scores them based on word intersection, 
        and extracts the most mathematically significant sentences.
        """
        sentences = re.split(r'(?<=[.!?]) +', text.replace('\n', ' '))
        sentences = [s.strip() for s in sentences if len(s.split()) > 4]
        
        if len(sentences) <= sentence_count:
            return text

        def sentence_similarity(s1: str, s2: str) -> float:
            w1 = set(BackroomsIntelligenceCore._tokenize(s1))
            w2 = set(BackroomsIntelligenceCore._tokenize(s2))
            if not w1 or not w2: return 0.0
            return len(w1.intersection(w2)) / (math.log10(len(w1)) + math.log10(len(w2)) + 1e-5)

        # Build similarity matrix (Graph)
        scores = [0.0] * len(sentences)
        for i in range(len(sentences)):
            for j in range(len(sentences)):
                if i != j:
                    scores[i] += sentence_similarity(sentences[i], sentences[j])

        # Rank and extract top sentences
        ranked = sorted(((scores[i], s, i) for i, s in enumerate(sentences)), reverse=True)
        top_sentences = sorted([item for item in ranked[:sentence_count]], key=lambda x: x[2])
        
        return " ".join([item[1] for item in top_sentences])

# ==============================================================================
# 4. ALGORITHMIC SLUG PERMUTATION & ERROR CORRECTION
# ==============================================================================
def levenshtein_distance(s1: str, s2: str) -> int:
    """Calculates minimal edit distance between two strings (Fuzzy Logic)."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    
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
    """
    Advanced routing matrix. Preserves colons, generates mathematically probable 
    slugs, and uses structural normalizations.
    """
    base = unquote(raw_input).strip()
    
    preserved = base.replace(" ", "-")
    preserved = re.sub(r'-{2,}', '-', preserved)
    preserved = re.sub(r'[^a-zA-Z0-9:-]', '', preserved)
    
    lowered = preserved.lower()
    numbers = ''.join(filter(str.isdigit, base))
    has_numbers = bool(numbers)
    
    variants = set()
    
    variants.add(preserved)
    variants.add(lowered)
    variants.add(lowered.replace("-", ""))
    variants.add(preserved.replace("-", ""))
    variants.add(lowered.replace("system-", "system:"))
    variants.add(preserved.replace("system-", "system:"))
    variants.add(base)
    
    if has_numbers:
        variants.add(f"level-{numbers}")
        variants.add(f"level{numbers}")
        variants.add(f"Level{numbers}")
        variants.add(f"Level-{numbers}")
        variants.add(numbers)
        
        if is_intl and lang:
            lang_dict = {
                "ru": f"uroven-{numbers}", "es": f"nivel-{numbers}",
                "fr": f"niveau-{numbers}", "de": f"ebene-{numbers}",
                "it": f"livello-{numbers}", "cn": f"level-{numbers}"
            }
            if lang in lang_dict:
                variants.add(lang_dict[lang])
                variants.add(lang_dict[lang].replace("-", ""))

    variants.add(base.upper())
    variants.add(base.lower())

    ordered_variants = list(dict.fromkeys([v for v in variants if v]))
    logger.info(f"[SMART-MATRIX V5.0] Synthesized {len(ordered_variants)} dimensional vectors for '{raw_input}'")
    return ordered_variants

# ==============================================================================
# 5. FASTAPI CONFIGURATION & PYDANTIC MODELS
# ==============================================================================
app = FastAPI(
    title="M.E.G. Omni-Database API (AI Core v5.0)",
    description="Maximum-complexity routing node with NLP-based summarization, entity extraction, and fuzzy slug resolution.",
    version="5.0.0"
)

class AIAnalysis(BaseModel):
    survival_difficulty: str = Field(..., description="NLP generated danger classification")
    entities_detected: List[str] = Field(..., description="Entities extracted from raw text")
    ai_summary: str = Field(..., description="Machine-generated TextRank TL;DR")

class ExtractedPayload(BaseModel):
    source_node: str
    resolved_url: Optional[str]
    ai_analysis: AIAnalysis
    raw_content: str

# Middleware for request timing and metrics
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-AI-Compute-Time-Sec"] = str(process_time)
    logger.info(f"[TELEMETRY] {request.method} {request.url.path} computed in {process_time:.3f}s")
    return response

# Global HTTPX Client (follow_redirects=True is CRITICAL for Wikidot)
TIMEOUT_SETTINGS = httpx.Timeout(15.0, connect=5.0)
DEFAULT_HEADERS = {
    "User-Agent": "MEG-AI-Bot/5.0 (Macintosh; Intel Mac OS X 10_15_7) Neural-Engine",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}
http_client = httpx.AsyncClient(timeout=TIMEOUT_SETTINGS, follow_redirects=True)

# ==============================================================================
# 6. ASYNC CONCURRENCY & DOM PARSING
# ==============================================================================
async def fetch_single_url(url: str) -> Optional[httpx.Response]:
    try:
        response = await http_client.get(url, headers=DEFAULT_HEADERS)
        if response.status_code == 200:
            return response
    except Exception:
        pass
    return None

async def concurrent_smart_fetch(base_url_template: str, variants: List[str]) -> tuple[Optional[str], Optional[httpx.Response]]:
    """Spawns parallel asynchronous HTTP requests to test all slug permutations simultaneously."""
    tasks = []
    for variant in variants:
        url = base_url_template.format(variant=variant)
        tasks.append((url, asyncio.create_task(fetch_single_url(url))))
    
    for url, task in tasks:
        response = await task
        if response and response.status_code == 200:
            logger.info(f"[CONCURRENCY HIT] Slug collapsed and resolved at: {url}")
            for _, t in tasks:
                if not t.done(): t.cancel() # Conserve system memory
            return url, response
            
    return None, None

def advanced_html_sanitizer(html_text: str, container_id: str = "page-content") -> str:
    """Uses Abstract Syntax Tree logic via BeautifulSoup to strip Wiki bloat."""
    soup = BeautifulSoup(html_text, "html.parser")
    content_div = soup.find(id=container_id) or soup.find(id="mw-content-text") or soup
        
    # Decompose script execution blocks and layout garbage
    for tag in content_div.find_all(['script', 'style', 'nav', 'footer', 'iframe']):
        tag.decompose()
        
    for class_name in ['page-rate-widget-box', 'page-tags', 'footer-wikiwiki', 'wd-adunit']:
        for div in content_div.find_all('div', class_=class_name):
            div.decompose()

    clean_text = content_div.get_text(separator="\n", strip=True)
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)
    return clean_text

def build_ai_response(node_name: str, clean_text: str, url: str = None) -> dict:
    """Orchestrates the NLP pipeline over the scraped text."""
    logger.info("Initializing NLP Pipeline on extracted text...")
    
    # Run NLP heuristics
    difficulty = BackroomsIntelligenceCore.extract_survival_difficulty(clean_text)
    entities = BackroomsIntelligenceCore.extract_entities(clean_text)
    summary = BackroomsIntelligenceCore.generate_extractive_summary(clean_text)
    
    url_str = f"Resolved URL: {url}\n" if url else ""
    
    # Format the payload perfectly for the GPT Archivist to read
    compiled_data = (
        f"[{node_name.upper()}]\n"
        f"{url_str}"
        f"--- AI METADATA ---\n"
        f"Classification: {difficulty}\n"
        f"Entities Detected: {', '.join(entities)}\n"
        f"AI Summary: {summary}\n"
        f"-------------------\n\n"
        f"[RAW DATABANK TEXT]\n"
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
        response = await http_client.get(url, params=params)
        data = response.json()
        await memory_bank.set(cache_key, data)
        return data
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": "Fandom database node unreachable.", "diagnostics": str(e)})

@app.get("/fandom/page")
async def get_fandom_page(title: str):
    cache_key = f"fandom_page_{title}"
    if cached := await memory_bank.get(cache_key): return cached
    
    url = "https://backrooms.fandom.com/api.php"
    params = {"action": "parse", "page": title, "format": "json", "prop": "text", "disabletoc": 1}
    try:
        response = await http_client.get(url, params=params)
        data = response.json()
        if "parse" in data:
            clean_text = advanced_html_sanitizer(data["parse"]["text"]["*"], container_id="mw-content-text")
            
            # Offload heavy NLP to ThreadPool (prevents blocking the Async Event Loop)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, build_ai_response, "FANDOM CORE NODE", clean_text, None
            )
            
            await memory_bank.set(cache_key, result)
            return result
        return JSONResponse(status_code=404, content={"error": f"Fandom page '{title}' not found."})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": "Error parsing Fandom data stream."})

@app.get("/wikidot/page")
async def get_wikidot_page(url: str):
    cache_key = f"wikidot_page_{url}"
    if cached := await memory_bank.get(cache_key): return cached
    
    variants = generate_smart_slug_matrix(url)
    base_template = "https://backrooms-wiki.wikidot.com/{variant}"
    
    successful_url, response = await concurrent_smart_fetch(base_template, variants)
    
    if successful_url and response:
        clean_text = advanced_html_sanitizer(response.text)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, build_ai_response, "WIKIDOT CORE NODE", clean_text, successful_url)
        await memory_bank.set(cache_key, result)
        return result
    return JSONResponse(status_code=404, content={"error": f"Wikidot page '{url}' unavailable."})

@app.get("/wikidot/international")
async def get_international_wikidot(lang: str, page: str):
    cache_key = f"intl_{lang}_{page}"
    if cached := await memory_bank.get(cache_key): return cached
    
    variants = generate_smart_slug_matrix(page, is_intl=True, lang=lang)
    base_template = f"https://backrooms-{lang}.wikidot.com/{{variant}}"
    
    successful_url, response = await concurrent_smart_fetch(base_template, variants)
    
    if successful_url and response:
        clean_text = advanced_html_sanitizer(response.text)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, build_ai_response, f"INTERNATIONAL NODE: {lang.upper()}", clean_text, successful_url)
        await memory_bank.set(cache_key, result)
        return result
    return JSONResponse(status_code=404, content={"error": f"International Wikidot branch '{lang}' page '{page}' not found."})

@app.get("/wikidot/freewriting")
async def get_free_writing_wiki(page: str):
    cache_key = f"freewriting_{page}"
    if cached := await memory_bank.get(cache_key): return cached
    
    variants = generate_smart_slug_matrix(page)
    base_template = "https://backrooms-freewriting.wikidot.com/{variant}"
    
    successful_url, response = await concurrent_smart_fetch(base_template, variants)
    
    if successful_url and response:
        clean_text = advanced_html_sanitizer(response.text)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, build_ai_response, "FREE WRITING WIKI NODE", clean_text, successful_url)
        await memory_bank.set(cache_key, result)
        return result
    return JSONResponse(status_code=404, content={"error": f"Free Writing Wiki page '{page}' not found."})

@app.get("/archives/liminal")
async def get_liminal_archives(page: str):
    cache_key = f"liminal_{page}"
    if cached := await memory_bank.get(cache_key): return cached
    
    variants = generate_smart_slug_matrix(page)
    base_template = "https://liminalarchives.xyz/wiki/{variant}"
    
    successful_url, response = await concurrent_smart_fetch(base_template, variants)
    if successful_url and response:
        clean_text = advanced_html_sanitizer(response.text, container_id="mw-content-text")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, build_ai_response, "LIMINAL ARCHIVES NODE", clean_text, successful_url)
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
        response = await http_client.get(url, params=search_params)
        search_data = response.json()
        if not search_data.get("query", {}).get("search"):
            return JSONResponse(status_code=404, content={"error": f"No cinematic records found for topic: {topic}"})
        
        exact_title = search_data["query"]["search"][0]["title"]
        page_params = {"action": "parse", "page": exact_title, "format": "json", "prop": "text", "disabletoc": 1}
        page_res = await http_client.get(url, params=page_params)
        page_data = page_res.json()
        
        if "parse" in page_data:
            clean_text = advanced_html_sanitizer(page_data["parse"]["text"]["*"], container_id="mw-content-text")
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, build_ai_response, f"KANE PIXELS CANON FILE: {exact_title}", clean_text, None)
            await memory_bank.set(cache_key, result)
            return result
        return JSONResponse(status_code=404, content={"error": "Failed to parse cinematic lore file."})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": "Cinematic tracking node unreachable."})
