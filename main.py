from fastapi import FastAPI, Query
import httpx
from bs4 import BeautifulSoup

app = FastAPI(title="Backrooms Live API")

@app.get("/fandom/search")
async def search_fandom(q: str):
    # פנייה ל-API החי של Fandom Backrooms
    url = "https://backrooms.fandom.com/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": q,
        "format": "json",
        "utf8": 1
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params)
        return response.json()

@app.get("/fandom/page")
async def get_fandom_page(title: str):
    # שליפת התוכן המלא והמעודכן של ערך בפאנדום
    url = "https://backrooms.fandom.com/api.php"
    params = {
        "action": "parse",
        "page": title,
        "format": "json",
        "prop": "text",
        "disabletoc": 1
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params)
        data = response.json()
        if "parse" in data:
            raw_html = data["parse"]["text"]["*"]
            soup = BeautifulSoup(raw_html, "html.parser")
            return {"content": soup.get_text(separator="\n", strip=True)}
        return {"error": "Page not found"}

@app.get("/wikidot/page")
async def get_wikidot_page(url: str):
    # שליפת עמוד או Sandbox בלייב מ-Wikidot וניקוי שלו
    if not url.startswith("http"):
        url = f"https://backrooms-wiki.wikidot.com/{url}"
    
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            # מוצא את תיבת התוכן המרכזית של וויקידוט ומנקה תפריטים
            content_div = soup.find(id="page-content")
            if content_div:
                return {"content": content_div.get_text(separator="\n", strip=True)}
            return {"content": soup.get_text(separator="\n", strip=True)}
        return {"error": f"Failed to fetch Wikidot page: {response.status_code}"}