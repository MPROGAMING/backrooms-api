from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
import httpx
from bs4 import BeautifulSoup
import logging

# הגדרת מערכת לוגים ברמת שרת
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="M.E.G. Omni-Database API (Ultra-Max Sync)",
    description="Central routing node for Backrooms APIs including Fandom, Wikidot, Liminal Archives, and Kane Pixels.",
    version="4.0.0"
)

# הגדרת Timeout קשיח כדי למנוע קריסה של Render
TIMEOUT_SETTINGS = httpx.Timeout(15.0)
DEFAULT_HEADERS = {"User-Agent": "MEG-Archival-Bot/4.0 (Macintosh; Intel Mac OS X 10_15_7)"}

@app.get("/fandom/search")
async def search_fandom(q: str):
    url = "https://backrooms.fandom.com/api.php"
    params = {"action": "query", "list": "search", "srsearch": q, "format": "json", "utf8": 1}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SETTINGS) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error(f"Fandom Search Error: {e}")
        return JSONResponse(status_code=500, content={"error": "Fandom database node unreachable."})

@app.get("/fandom/page")
async def get_fandom_page(title: str):
    url = "https://backrooms.fandom.com/api.php"
    params = {"action": "parse", "page": title, "format": "json", "prop": "text", "disabletoc": 1}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SETTINGS) as client:
            response = await client.get(url, params=params)
            data = response.json()
            if "parse" in data:
                soup = BeautifulSoup(data["parse"]["text"]["*"], "html.parser")
                clean_text = soup.get_text(separator="\n", strip=True)
                return {"content": clean_text}
            return JSONResponse(status_code=404, content={"error": f"Fandom page '{title}' not found."})
    except Exception as e:
        logger.error(f"Fandom Page Error: {e}")
        return JSONResponse(status_code=500, content={"error": "Error parsing Fandom data stream."})

@app.get("/wikidot/page")
async def get_wikidot_page(url: str):
    if not url.startswith("http"):
        url = f"https://backrooms-wiki.wikidot.com/{url}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SETTINGS) as client:
            response = await client.get(url, headers=DEFAULT_HEADERS)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                content_div = soup.find(id="page-content")
                text = content_div.get_text(separator="\n", strip=True) if content_div else soup.get_text(separator="\n", strip=True)
                return {"content": text}
            return JSONResponse(status_code=404, content={"error": f"Wikidot page unavailable. Status: {response.status_code}"})
    except Exception as e:
        logger.error(f"Wikidot Page Error: {e}")
        return JSONResponse(status_code=500, content={"error": "Wikidot node connection timed out."})

@app.get("/wikidot/international")
async def get_international_wikidot(lang: str, page: str):
    url = f"https://backrooms-{lang}.wikidot.com/{page}"
    logger.info(f"Targeting International URL: {url}")
    
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SETTINGS) as client:
            response = await client.get(url, headers=DEFAULT_HEADERS)
            
            if response.status_code == 404 and "-" in page:
                alt_page = page.replace("-", "")
                url = f"https://backrooms-{lang}.wikidot.com/{alt_page}"
                logger.info(f"Retrying with Alternative URL: {url}")
                response = await client.get(url, headers=DEFAULT_HEADERS)

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                content_div = soup.find(id="page-content")
                text = content_div.get_text(separator="\n", strip=True) if content_div else soup.get_text(separator="\n", strip=True)
                return {"content": f"[INTERNATIONAL NODE: {lang.upper()}]\n{text}"}
                
            return JSONResponse(status_code=404, content={"error": f"International Wikidot branch '{lang}' page '{page}' not found. Tried URL: {url}"})
    except Exception as e:
        logger.error(f"International Wikidot Error: {e}")
        return JSONResponse(status_code=500, content={"error": f"International node '{lang}' connection failed: {str(e)}"})

@app.get("/wikidot/freewriting")
async def get_free_writing_wiki(page: str):
    url = f"https://backrooms-freewriting.wikidot.com/{page}"
    logger.info(f"Targeting Free Writing Wiki URL: {url}")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SETTINGS) as client:
            response = await client.get(url, headers=DEFAULT_HEADERS)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                content_div = soup.find(id="page-content")
                text = content_div.get_text(separator="\n", strip=True) if content_div else soup.get_text(separator="\n", strip=True)
                return {"content": f"[FREE WRITING WIKI NODE]\n{text}"}
            return JSONResponse(status_code=404, content={"error": f"Free Writing Wiki page '{page}' not found."})
    except Exception as e:
        logger.error(f"Free Writing Wiki Error: {e}")
        return JSONResponse(status_code=500, content={"error": f"Free Writing Wiki connection failed: {str(e)}"})

@app.get("/archives/liminal")
async def get_liminal_archives(page: str):
    url = f"https://liminalarchives.xyz/wiki/{page}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SETTINGS) as client:
            response = await client.get(url, headers=DEFAULT_HEADERS)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                content_div = soup.find(id="mw-content-text")
                text = content_div.get_text(separator="\n", strip=True) if content_div else soup.get_text(separator="\n", strip=True)
                return {"content": f"[LIMINAL ARCHIVES NODE]\n{text}"}
            return JSONResponse(status_code=404, content={"error": f"Liminal Archives page '{page}' not found."})
    except Exception as e:
        logger.error(f"Liminal Archives Error: {e}")
        return JSONResponse(status_code=500, content={"error": "Liminal Archives database unreachable."})

@app.get("/cinematic/kanepixels")
async def get_kane_pixels_lore(topic: str):
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
                soup = BeautifulSoup(page_data["parse"]["text"]["*"], "html.parser")
                clean_text = soup.get_text(separator="\n", strip=True)
                return {"content": f"[KANE PIXELS CANON FILE: {exact_title}]\n{clean_text}"}
                
            return JSONResponse(status_code=404, content={"error": "Failed to parse cinematic lore file."})
    except Exception as e:
        logger.error(f"Kane Pixels Lore Error: {e}")
        return JSONResponse(status_code=500, content={"error": "Cinematic tracking node unreachable."})
