from __future__ import annotations
import hmac, json, os, sqlite3, threading, uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from .common import now_iso

MAX_SNAPSHOTS_PER_DOCUMENT = int(os.getenv('ATLAS_MAX_SNAPSHOTS_PER_DOCUMENT', '50'))
MAX_PROJECT_EVENTS_PER_PROJECT = int(os.getenv('ATLAS_MAX_PROJECT_EVENTS_PER_PROJECT', '250'))
MAX_TELEMETRY_ROWS = int(os.getenv('ATLAS_MAX_TELEMETRY_ROWS', '10000'))
MAX_EVAL_RUNS = int(os.getenv('ATLAS_MAX_EVAL_RUNS', '200'))
MAX_SEARCH_DOCUMENTS = int(os.getenv('ATLAS_MAX_SEARCH_DOCUMENTS', '500'))
MAX_SEARCH_TEXT_CHARS = int(os.getenv('ATLAS_MAX_SEARCH_TEXT_CHARS', '24000'))

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
  state_json TEXT NOT NULL, access_token_hash TEXT, expires_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
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

    def _new_connection(self):
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        # PRAGMAs are connection-specific. They must be configured here, not
        # only in the schema bootstrap connection.
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        return conn

    def _conn(self):
        conn = getattr(self._local,'conn',None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def _init(self):
        c = self._new_connection()
        try:
            c.executescript(SCHEMA)
            columns = {row['name'] for row in c.execute('PRAGMA table_info(projects)').fetchall()}
            if 'access_token_hash' not in columns:
                c.execute('ALTER TABLE projects ADD COLUMN access_token_hash TEXT')
            if 'expires_at' not in columns:
                c.execute('ALTER TABLE projects ADD COLUMN expires_at TEXT')
            c.commit()
        finally:
            c.close()

    def execute(self, sql:str, params=()):
        c=self._conn(); cur=c.execute(sql,params); c.commit(); return cur
    def query(self, sql:str, params=()):
        return [dict(r) for r in self._conn().execute(sql,params).fetchall()]
    def one(self, sql:str, params=()):
        r=self._conn().execute(sql,params).fetchone(); return dict(r) if r else None
    def upsert_document(self, doc:Dict[str,Any], snapshot:bool=True):
        """Compatibility helper that keeps document/snapshot writes atomic.

        Runtime indexing uses ``upsert_document_with_edges`` directly.  This
        legacy helper remains for tests and callers that only update a document;
        it preserves any existing outgoing edges instead of creating a second,
        non-transactional write path.
        """
        preserved_edges = [
            {
                "source_id": edge["source_id"],
                "to_key": edge["to_key"],
                "to_title": edge.get("to_title"),
                "to_url": edge.get("to_url"),
                "relation": edge.get("relation", "links_to"),
                "confidence": edge.get("confidence", 1.0),
                "metadata": json.loads(edge.get("metadata_json") or "{}"),
            }
            for edge in self.edges_from(doc["doc_id"])
        ]
        self.upsert_document_with_edges(doc, preserved_edges, snapshot=snapshot)

    @staticmethod
    def _prune_snapshots(connection, doc_id: str) -> None:
        connection.execute(
            '''DELETE FROM snapshots WHERE snapshot_id IN (
                SELECT snapshot_id FROM snapshots
                WHERE doc_id=?
                ORDER BY captured_at DESC, snapshot_id DESC
                LIMIT -1 OFFSET ?
            )''',
            (doc_id, MAX_SNAPSHOTS_PER_DOCUMENT),
        )

    def list_documents(self, source_ids:Optional[List[str]]=None, limit:int=1000):
        if source_ids:
            q=','.join('?' for _ in source_ids)
            return self.query(f'SELECT * FROM documents WHERE source_id IN ({q}) ORDER BY updated_at DESC LIMIT ?',(*source_ids,limit))
        return self.query('SELECT * FROM documents ORDER BY updated_at DESC LIMIT ?',(limit,))

    def list_documents_for_search(self, source_ids:Optional[List[str]]=None, limit:int=MAX_SEARCH_DOCUMENTS):
        limit = max(1, min(int(limit), MAX_SEARCH_DOCUMENTS))
        # Only the leading excerpt participates in interactive ranking. Full text
        # remains stored for fetch/diff, but a public search cannot repeatedly
        # tokenize hundreds of megabytes of corpus content.
        select = '''SELECT doc_id,source_id,canon,title,url,summary,vector_json,
                    archived,substr(text,1,?) AS text FROM documents'''
        if source_ids:
            q=','.join('?' for _ in source_ids)
            return self.query(
                f'{select} WHERE source_id IN ({q}) ORDER BY updated_at DESC LIMIT ?',
                (MAX_SEARCH_TEXT_CHARS, *source_ids, limit),
            )
        return self.query(f'{select} ORDER BY updated_at DESC LIMIT ?', (MAX_SEARCH_TEXT_CHARS, limit))

    def document_count(self, source_ids:Optional[List[str]]=None):
        if source_ids:
            q=','.join('?' for _ in source_ids)
            return self.one(f'SELECT COUNT(*) c FROM documents WHERE source_id IN ({q})',tuple(source_ids))['c']
        return self.one('SELECT COUNT(*) c FROM documents')['c']
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
        """Replace all outgoing edges for a document in one transaction."""
        c = self._conn()
        try:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM edges WHERE from_doc_id=?', (doc_id,))
            rows = [
                (
                    uuid.uuid4().hex,
                    e['source_id'],
                    doc_id,
                    e['to_key'],
                    e.get('to_title'),
                    e.get('to_url'),
                    e.get('relation', 'links_to'),
                    float(e.get('confidence', 1.0)),
                    json.dumps(e.get('metadata', {})),
                    now_iso(),
                )
                for e in edges
            ]
            if rows:
                c.executemany(
                    '''INSERT INTO edges(
                        edge_id, source_id, from_doc_id, to_key, to_title,
                        to_url, relation, confidence, metadata_json, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    rows,
                )
            c.commit()
        except Exception:
            c.rollback()
            raise

    def upsert_document_with_edges(self, doc:Dict[str,Any], edges:List[Dict[str,Any]], snapshot:bool=True):
        """Atomically persist a document, optional snapshot, and all outgoing edges.

        This prevents the old failure mode where the document was committed but edge
        insertion failed, causing the API to return 500 while leaving a partially indexed
        document behind.
        """
        c = self._conn()
        try:
            c.execute('BEGIN IMMEDIATE')
            existing = c.execute(
                'SELECT content_hash FROM documents WHERE doc_id=?',
                (doc['doc_id'],),
            ).fetchone()

            if snapshot and existing and existing['content_hash'] != doc['content_hash']:
                old = c.execute(
                    'SELECT * FROM documents WHERE doc_id=?',
                    (doc['doc_id'],),
                ).fetchone()
                # Keep one historical copy per content hash.  The current row
                # is synthesized by ``snapshots()``; writing an initial snapshot
                # plus the same row again on the first change used to duplicate
                # history and made retention less meaningful.
                has_snapshot = c.execute(
                    'SELECT 1 FROM snapshots WHERE doc_id=? AND content_hash=? LIMIT 1',
                    (old['doc_id'], old['content_hash']),
                ).fetchone()
                if not has_snapshot:
                    c.execute(
                        'INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)',
                        (
                            uuid.uuid4().hex,
                            old['doc_id'],
                            old['content_hash'],
                            old['title'],
                            old['text'],
                            old['metadata_json'],
                            now_iso(),
                        ),
                    )

            c.execute(
                '''INSERT INTO documents(
                    doc_id,source_id,canon,title,url,language,text,summary,
                    content_hash,vector_json,metadata_json,archived,fetched_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    source_id=excluded.source_id,
                    canon=excluded.canon,
                    title=excluded.title,
                    url=excluded.url,
                    language=excluded.language,
                    text=excluded.text,
                    summary=excluded.summary,
                    content_hash=excluded.content_hash,
                    vector_json=excluded.vector_json,
                    metadata_json=excluded.metadata_json,
                    archived=excluded.archived,
                    fetched_at=excluded.fetched_at,
                    updated_at=excluded.updated_at''',
                (
                    doc['doc_id'], doc['source_id'], doc.get('canon'), doc['title'],
                    doc['url'], doc.get('language'), doc['text'], doc.get('summary', ''),
                    doc['content_hash'], json.dumps(doc['vector']),
                    json.dumps(doc.get('metadata', {})), 1 if doc.get('archived') else 0,
                    doc.get('fetched_at', now_iso()), now_iso(),
                ),
            )

            c.execute('DELETE FROM edges WHERE from_doc_id=?', (doc['doc_id'],))
            edge_rows = [
                (
                    uuid.uuid4().hex,
                    e['source_id'],
                    doc['doc_id'],
                    e['to_key'],
                    e.get('to_title'),
                    e.get('to_url'),
                    e.get('relation', 'links_to'),
                    float(e.get('confidence', 1.0)),
                    json.dumps(e.get('metadata', {})),
                    now_iso(),
                )
                for e in edges
            ]
            if edge_rows:
                c.executemany(
                    '''INSERT INTO edges(
                        edge_id, source_id, from_doc_id, to_key, to_title,
                        to_url, relation, confidence, metadata_json, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    edge_rows,
                )

            self._prune_snapshots(c, doc['doc_id'])
            c.commit()
        except Exception:
            c.rollback()
            raise
    def edges_from(self,doc_id:str): return self.query('SELECT * FROM edges WHERE from_doc_id=?',(doc_id,))
    def edges_to(self,to_key:str): return self.query('SELECT * FROM edges WHERE to_key=?',(to_key,))
    @staticmethod
    def _public_project(row: Optional[Dict[str, Any]]):
        if not row:
            return None
        project = dict(row)
        project['state'] = json.loads(project.pop('state_json'))
        project.pop('access_token_hash', None)
        return project

    def _prune_project_events(self, connection, project_id: str) -> None:
        connection.execute(
            '''DELETE FROM project_events WHERE event_id IN (
                SELECT event_id FROM project_events
                WHERE project_id=?
                ORDER BY created_at DESC, event_id DESC
                LIMIT -1 OFFSET ?
            )''',
            (project_id, MAX_PROJECT_EVENTS_PER_PROJECT),
        )

    def create_project(
        self,
        name: str,
        project_type: str,
        target_platform: str | None,
        canon_scope: str | None,
        state: Dict[str, Any],
        access_token_hash: str,
        expires_at: str,
    ):
        pid=uuid.uuid4().hex; t=now_iso(); c=self._conn()
        try:
            c.execute('BEGIN IMMEDIATE')
            c.execute(
                '''INSERT INTO projects(
                    project_id,name,project_type,target_platform,canon_scope,stage,
                    state_json,access_token_hash,expires_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (pid,name,project_type,target_platform,canon_scope,'concept',json.dumps(state),access_token_hash,expires_at,t,t),
            )
            c.execute(
                'INSERT INTO project_events VALUES(?,?,?,?,?)',
                (uuid.uuid4().hex,pid,'created',json.dumps(state),t),
            )
            self._prune_project_events(c, pid)
            c.commit()
        except Exception:
            c.rollback()
            raise
        return self.get_project(pid)

    def get_project(self,pid:str):
        return self._public_project(self.one('SELECT * FROM projects WHERE project_id=?',(pid,)))

    def project_token_matches(self, pid: str, token_hash: str) -> bool:
        row = self.one(
            'SELECT access_token_hash,expires_at FROM projects WHERE project_id=?',
            (pid,),
        )
        if not row or not row.get('access_token_hash'):
            return False
        expires_at = row.get('expires_at')
        if expires_at and expires_at <= now_iso():
            return False
        return hmac.compare_digest(str(row['access_token_hash']), str(token_hash))

    def update_project(self,pid:str,stage:Optional[str],patch:Dict[str,Any]):
        c=self._conn()
        try:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM projects WHERE project_id=?',(pid,)).fetchone()
            if not row:
                c.rollback(); return None
            state=json.loads(row['state_json']); state.update(patch); new_stage=stage or row['stage']; updated=now_iso()
            c.execute(
                'UPDATE projects SET stage=?,state_json=?,updated_at=? WHERE project_id=?',
                (new_stage,json.dumps(state),updated,pid),
            )
            c.execute(
                'INSERT INTO project_events VALUES(?,?,?,?,?)',
                (uuid.uuid4().hex,pid,'updated',json.dumps({'stage':new_stage,'patch':patch}),updated),
            )
            self._prune_project_events(c, pid)
            c.commit()
        except Exception:
            c.rollback()
            raise
        return self.get_project(pid)

    def delete_project(self, pid: str) -> bool:
        c=self._conn()
        try:
            c.execute('BEGIN IMMEDIATE')
            result=c.execute('DELETE FROM projects WHERE project_id=?',(pid,))
            c.commit()
            return result.rowcount > 0
        except Exception:
            c.rollback()
            raise

    def purge_expired_projects(self) -> int:
        result=self.execute('DELETE FROM projects WHERE expires_at IS NOT NULL AND expires_at <= ?',(now_iso(),))
        return result.rowcount

    def project_events(self,pid:str):
        rows=self.query(
            'SELECT * FROM project_events WHERE project_id=? ORDER BY created_at LIMIT ?',
            (pid, MAX_PROJECT_EVENTS_PER_PROJECT),
        )
        for r in rows:r['payload']=json.loads(r.pop('payload_json'))
        return rows
    def upsert_candidate(self,host:str,platform:str,discovered_from:str,score:float,evidence:Dict[str,Any]):
        cid=uuid.uuid5(uuid.NAMESPACE_URL,host).hex; t=now_iso()
        self.execute("""INSERT INTO source_candidates VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET platform=excluded.platform,discovered_from=excluded.discovered_from,score=MAX(source_candidates.score,excluded.score),evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",(cid,host,platform,discovered_from,'pending',score,json.dumps(evidence),t,t))
        return self.one('SELECT * FROM source_candidates WHERE host=?',(host,))
    def candidates(self,status:Optional[str]=None):
        if status:return self.query('SELECT * FROM source_candidates WHERE status=? ORDER BY score DESC',(status,))
        return self.query('SELECT * FROM source_candidates ORDER BY score DESC')
    def candidate(self, cid: str):
        return self.one('SELECT * FROM source_candidates WHERE candidate_id=?',(cid,))
    def set_candidate_status(self,cid:str,status:str):
        self.execute('UPDATE source_candidates SET status=?,updated_at=? WHERE candidate_id=?',(status,now_iso(),cid)); return self.one('SELECT * FROM source_candidates WHERE candidate_id=?',(cid,))
    def save_eval(self,suite:str,results:List[Dict[str,Any]]):
        rid=uuid.uuid4().hex; passed=sum(1 for r in results if r.get('passed')); failed=len(results)-passed; c=self._conn()
        try:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT INTO eval_runs VALUES(?,?,?,?,?,?)',(rid,suite,passed,failed,json.dumps(results),now_iso()))
            c.execute('DELETE FROM eval_runs WHERE rowid IN (SELECT rowid FROM eval_runs ORDER BY created_at DESC LIMIT -1 OFFSET ?)',(MAX_EVAL_RUNS,))
            c.commit()
        except Exception:
            c.rollback(); raise
        return {'run_id':rid,'passed':passed,'failed':failed,'results':results}
    def telemetry(self,endpoint:str,status_code:int,elapsed_ms:int):
        c=self._conn()
        try:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT INTO telemetry(endpoint,status_code,elapsed_ms,created_at) VALUES(?,?,?,?)',(endpoint,status_code,elapsed_ms,now_iso()))
            c.execute(
                'DELETE FROM telemetry WHERE metric_id <= COALESCE((SELECT MAX(metric_id)-? FROM telemetry), 0)',
                (MAX_TELEMETRY_ROWS,),
            )
            c.commit()
        except Exception:
            c.rollback()
            raise
    def stats(self):
        docs=self.one('SELECT COUNT(*) c FROM documents')['c']; snaps=self.one('SELECT COUNT(*) c FROM snapshots')['c']; edges=self.one('SELECT COUNT(*) c FROM edges')['c']; projects=self.one('SELECT COUNT(*) c FROM projects')['c']
        sources=self.query('SELECT source_id,COUNT(*) count FROM documents GROUP BY source_id ORDER BY count DESC')
        tele=self.one('SELECT COUNT(*) requests,AVG(elapsed_ms) avg_ms,SUM(CASE WHEN status_code>=400 THEN 1 ELSE 0 END) errors FROM telemetry') or {}
        return {
            'documents':docs,
            'snapshots':snaps,
            'edges':edges,
            'projects':projects,
            'documents_by_source':sources,
            'telemetry':tele,
            'storage_mode':os.getenv('ATLAS_PERSISTENCE_MODE','ephemeral'),
            'telemetry_retention_rows':MAX_TELEMETRY_ROWS,
        }
