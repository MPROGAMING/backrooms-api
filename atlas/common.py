from __future__ import annotations
import hashlib, math, re
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Sequence

TOKEN_RE = re.compile(r"[a-z0-9\u00C0-\u024F\u0400-\u04FF\u4E00-\u9FFF]{2,}", re.I)
STOP = {"the","and","that","this","with","from","into","level","backrooms","are","was","were","have","has","had","for","not","but","you","your","their","its","can","will","would","could","should","about","there","they","them","then","than","also","only","when","where","which","while","what","who","how","why"}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())

def title_key(text: str) -> str:
    return normalize(re.sub(r"[^\w\- ]+", " ", (text or "").casefold()).replace("_", " "))

def tokenize(text: str) -> List[str]:
    return [t.casefold() for t in TOKEN_RE.findall(text or "") if t.casefold() not in STOP]

def shingles(tokens: Sequence[str], n: int = 2) -> List[str]:
    if len(tokens) < n: return list(tokens)
    return [" ".join(tokens[i:i+n]) for i in range(len(tokens)-n+1)]

def feature_hash_vector(text: str, dims: int = 384) -> Dict[int, float]:
    tokens = tokenize(text)
    feats = tokens + shingles(tokens, 2)
    counts = Counter(feats)
    vec: Dict[int,float] = {}
    for feat, count in counts.items():
        raw = hashlib.blake2b(feat.encode('utf-8'), digest_size=8).digest()
        idx = int.from_bytes(raw, 'big') % dims
        sign = 1.0 if raw[0] % 2 == 0 else -1.0
        vec[idx] = vec.get(idx, 0.0) + sign * (1.0 + math.log1p(count))
    norm = math.sqrt(sum(v*v for v in vec.values())) or 1.0
    return {k: v/norm for k,v in vec.items()}

def cosine_sparse(a: Dict[int,float], b: Dict[int,float]) -> float:
    if len(a) > len(b): a,b = b,a
    return sum(v*b.get(k,0.0) for k,v in a.items())

def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, title_key(a), title_key(b)).ratio()

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
