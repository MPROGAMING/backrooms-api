from __future__ import annotations
from html import escape

def render_dashboard(stats:dict) -> str:
    src=''.join(f"<tr><td>{escape(str(r['source_id']))}</td><td>{r['count']}</td></tr>" for r in stats.get('documents_by_source',[])) or '<tr><td colspan=2>No indexed documents yet</td></tr>'
    tele=stats.get('telemetry') or {}
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>BackroomsGPT Atlas</title><style>
body{{font-family:system-ui;background:#0d0d0d;color:#eee;margin:0;padding:32px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px}}.card{{background:#171717;border:1px solid #333;border-radius:12px;padding:18px}}h1{{color:#e2c44e}}table{{width:100%;border-collapse:collapse}}td,th{{padding:8px;border-bottom:1px solid #333;text-align:left}}code{{color:#e2c44e}}</style></head><body>
<h1>BackroomsGPT Atlas v21</h1><div class=grid><div class=card><h2>{stats.get('documents',0)}</h2>Indexed documents</div><div class=card><h2>{stats.get('snapshots',0)}</h2>Snapshots</div><div class=card><h2>{stats.get('edges',0)}</h2>Graph edges</div><div class=card><h2>{stats.get('projects',0)}</h2>Writer projects</div></div>
<h2>Documents by source</h2><table><tr><th>Source</th><th>Count</th></tr>{src}</table><h2>Telemetry</h2><pre>{escape(str(tele))}</pre><p>Storage mode: <code>{escape(str(stats.get('storage_mode','unknown')))}</code></p><p>Telemetry retention: <code>{escape(str(stats.get('telemetry_retention_rows','bounded')))}</code></p></body></html>'''
