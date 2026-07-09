from __future__ import annotations
import json, os, sqlite3, threading, uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from .common import now_iso

SCHEMA = r"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS documents(
  doc_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, canon TEXT, title TEXT NOT NULL,
  url TEXT NOT NULL, language TEXT, text TEXT NOT NULL, summary TEXT,
  content_hash TEXT NOT NULL, vector_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0, fetched_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_documents_title ON documents(title);
CREATE TABLE IF NOT EXISTS snapshots(
  snapshot_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, content_hash TEXT NOT NULL,
  title TEXT NOT NULL, text TEXT NOT NULL, metadata_json TEXT NOT NULL,
  captured_at TEXT NOT NULL, FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_snapshots_doc_time ON snapshots(doc_id, captured_at DESC);
CREATE TABLE IF NOT EXISTS edges(
  edge_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, from_doc_id TEXT NOT NULL,
  to_key TEXT NOT NULL, to_title TEXT, to_url TEXT, relation TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_doc_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_key);
CREATE TABLE IF NOT EXISTS projects(
  project_id TEXT PRIMARY KEY, name TEXT NOT NULL, project_type TEXT NOT NULL,
  target_platform TEXT, canon_scope TEXT, stage TEXT NOT NULL,
  state_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_events(
  event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS source_candidates(
  candidate_id TEXT PRIMARY KEY, host TEXT NOT NULL UNIQUE, platform TEXT NOT NULL,
  discovered_from TEXT, status TEXT NOT NULL, score REAL NOT NULL,
  evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_runs(
  run_id TEXT PRIMARY KEY, suite TEXT NOT NULL, passed INTEGER NOT NULL,
  failed INTEGER NOT NULL, results_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry(
  metric_id INTEGER PRIMARY KEY AUTOINCREMENT, endpoint TEXT NOT NULL,
  status_code INTEGER NOT NULL, elapsed_ms INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
"""

class AtlasStore:
    def __init__(self, path: Optional[str]=None):
        root = Path(os.getenv('ATLAS_DATA_DIR', '/tmp/backroomsgpt-atlas'))
        root.mkdir(parents=True, exist_ok=True)
        self.path = str(Path(path) if path else root/'atlas.sqlite3')
        self._local = threading.local()
        self._init()
    def _conn(self):
        conn = getattr(self._local,'conn',None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn
    def _init(self):
        c = sqlite3.connect(self.path); c.executescript(SCHEMA); c.close()
    def execute(self, sql:str, params=()):
        c=self._conn(); cur=c.execute(sql,params); c.commit(); return cur
    def query(self, sql:str, params=()):
        return [dict(r) for r in self._conn().execute(sql,params).fetchall()]
    def one(self, sql:str, params=()):
        r=self._conn().execute(sql,params).fetchone(); return dict(r) if r else None
    def upsert_document(self, doc:Dict[str,Any], snapshot:bool=True):
        existing=self.one('SELECT content_hash FROM documents WHERE doc_id=?',(doc['doc_id'],))
        if snapshot and existing and existing['content_hash'] != doc['content_hash']:
            old=self.one('SELECT * FROM documents WHERE doc_id=?',(doc['doc_id'],))
            self.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)',(
                uuid.uuid4().hex, old['doc_id'], old['content_hash'], old['title'], old['text'], old['metadata_json'], now_iso()))
        self.execute("""INSERT INTO documents(doc_id,source_id,canon,title,url,language,text,summary,content_hash,vector_json,metadata_json,archived,fetched_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(doc_id) DO UPDATE SET
        source_id=excluded.source_id,canon=excluded.canon,title=excluded.title,url=excluded.url,language=excluded.language,text=excluded.text,summary=excluded.summary,content_hash=excluded.content_hash,vector_json=excluded.vector_json,metadata_json=excluded.metadata_json,archived=excluded.archived,fetched_at=excluded.fetched_at,updated_at=excluded.updated_at""",(
            doc['doc_id'],doc['source_id'],doc.get('canon'),doc['title'],doc['url'],doc.get('language'),doc['text'],doc.get('summary',''),doc['content_hash'],json.dumps(doc['vector']),json.dumps(doc.get('metadata',{})),1 if doc.get('archived') else 0,doc.get('fetched_at',now_iso()),now_iso()))
        if snapshot and not existing:
            self.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)',(
                uuid.uuid4().hex,doc['doc_id'],doc['content_hash'],doc['title'],doc['text'],json.dumps(doc.get('metadata',{})),now_iso()))
    def list_documents(self, source_ids:Optional[List[str]]=None, limit:int=1000):
        if source_ids:
            q=','.join('?' for _ in source_ids)
            return self.query(f'SELECT * FROM documents WHERE source_id IN ({q}) ORDER BY updated_at DESC LIMIT ?',(*source_ids,limit))
        return self.query('SELECT * FROM documents ORDER BY updated_at DESC LIMIT ?',(limit,))
    def get_document(self, doc_id:str): return self.one('SELECT * FROM documents WHERE doc_id=?',(doc_id,))
    def get_by_source_title(self, source_id:str,title:str):
        return self.one('SELECT * FROM documents WHERE source_id=? AND lower(title)=lower(?) LIMIT 1',(source_id,title))
    def snapshots(self, doc_id:str,limit:int=10):
        rows=self.query('SELECT * FROM snapshots WHERE doc_id=? ORDER BY captured_at DESC LIMIT ?',(doc_id,limit))
        current=self.get_document(doc_id)
        if current:
            rows.insert(0,{'snapshot_id':'current','doc_id':doc_id,'content_hash':current['content_hash'],'title':current['title'],'text':current['text'],'metadata_json':current['metadata_json'],'captured_at':current['updated_at']})
        return rows
    def replace_edges(self, doc_id:str, edges:List[Dict[str,Any]]):
        self.execute('DELETE FROM edges WHERE from_doc_id=?',(doc_id,))
        for e in edges:
            self.execute('INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?)',(
                uuid.uuid4().hex,e['source_id'],doc_id,e['to_key'],e.get('to_title'),e.get('to_url'),e.get('relation','links_to'),float(e.get('confidence',1.0)),json.dumps(e.get('metadata',{})),now_iso()))
    def edges_from(self,doc_id:str): return self.query('SELECT * FROM edges WHERE from_doc_id=?',(doc_id,))
    def edges_to(self,to_key:str): return self.query('SELECT * FROM edges WHERE to_key=?',(to_key,))
    def create_project(self,name:str,project_type:str,target_platform:str|None,canon_scope:str|None,state:Dict[str,Any]):
        pid=uuid.uuid4().hex; t=now_iso()
        self.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?)',(pid,name,project_type,target_platform,canon_scope,'concept',json.dumps(state),t,t))
        self.add_project_event(pid,'created',state); return self.get_project(pid)
    def get_project(self,pid:str):
        p=self.one('SELECT * FROM projects WHERE project_id=?',(pid,))
        if p: p['state']=json.loads(p.pop('state_json'))
        return p
    def update_project(self,pid:str,stage:Optional[str],patch:Dict[str,Any]):
        p=self.get_project(pid)
        if not p: return None
        state=p['state']; state.update(patch); stage=stage or p['stage']
        self.execute('UPDATE projects SET stage=?,state_json=?,updated_at=? WHERE project_id=?',(stage,json.dumps(state),now_iso(),pid))
        self.add_project_event(pid,'updated',{'stage':stage,'patch':patch}); return self.get_project(pid)
    def add_project_event(self,pid:str,event_type:str,payload:Dict[str,Any]):
        self.execute('INSERT INTO project_events VALUES(?,?,?,?,?)',(uuid.uuid4().hex,pid,event_type,json.dumps(payload),now_iso()))
    def project_events(self,pid:str):
        rows=self.query('SELECT * FROM project_events WHERE project_id=? ORDER BY created_at',(pid,))
        for r in rows:r['payload']=json.loads(r.pop('payload_json'))
        return rows
    def upsert_candidate(self,host:str,platform:str,discovered_from:str,score:float,evidence:Dict[str,Any]):
        cid=uuid.uuid5(uuid.NAMESPACE_URL,host).hex; t=now_iso()
        self.execute("""INSERT INTO source_candidates VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET platform=excluded.platform,discovered_from=excluded.discovered_from,score=MAX(source_candidates.score,excluded.score),evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",(cid,host,platform,discovered_from,'pending',score,json.dumps(evidence),t,t))
        return self.one('SELECT * FROM source_candidates WHERE host=?',(host,))
    def candidates(self,status:Optional[str]=None):
        if status:return self.query('SELECT * FROM source_candidates WHERE status=? ORDER BY score DESC',(status,))
        return self.query('SELECT * FROM source_candidates ORDER BY score DESC')
    def set_candidate_status(self,cid:str,status:str):
        self.execute('UPDATE source_candidates SET status=?,updated_at=? WHERE candidate_id=?',(status,now_iso(),cid)); return self.one('SELECT * FROM source_candidates WHERE candidate_id=?',(cid,))
    def save_eval(self,suite:str,results:List[Dict[str,Any]]):
        rid=uuid.uuid4().hex; passed=sum(1 for r in results if r.get('passed')); failed=len(results)-passed
        self.execute('INSERT INTO eval_runs VALUES(?,?,?,?,?,?)',(rid,suite,passed,failed,json.dumps(results),now_iso())); return {'run_id':rid,'passed':passed,'failed':failed,'results':results}
    def telemetry(self,endpoint:str,status_code:int,elapsed_ms:int):
        self.execute('INSERT INTO telemetry(endpoint,status_code,elapsed_ms,created_at) VALUES(?,?,?,?)',(endpoint,status_code,elapsed_ms,now_iso()))
    def stats(self):
        docs=self.one('SELECT COUNT(*) c FROM documents')['c']; snaps=self.one('SELECT COUNT(*) c FROM snapshots')['c']; edges=self.one('SELECT COUNT(*) c FROM edges')['c']; projects=self.one('SELECT COUNT(*) c FROM projects')['c']
        sources=self.query('SELECT source_id,COUNT(*) count FROM documents GROUP BY source_id ORDER BY count DESC')
        tele=self.one('SELECT COUNT(*) requests,AVG(elapsed_ms) avg_ms,SUM(CASE WHEN status_code>=400 THEN 1 ELSE 0 END) errors FROM telemetry') or {}
        return {'documents':docs,'snapshots':snaps,'edges':edges,'projects':projects,'documents_by_source':sources,'telemetry':tele,'database_path':self.path}
