"""SQLite-backed state store with content-addressed, integrity-checked originals."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat

from .util import ContextError, atomic_write, canonical, digest, read_scoped, user_directory, utc_now


def user_state_home() -> Path:
    """Per-user plugin state outside any workspace (hook error log, session roots, fallback stores)."""
    return user_directory("XDG_STATE_HOME", ".local", "state") / "better-codex-context"


def state_directory(root: Path) -> Path:
    """`<root>/.better-codex-context`, or a per-user fallback when the workspace cannot be written.

    Codex may run in `/`, a root-owned or read-only directory; hooks and the MCP server must still
    agree on one location, so the choice depends only on the root and is the same for both.
    """
    local = root / ".better-codex-context"
    if local.is_symlink():
        raise ContextError("state directory cannot be a symlink")
    if os.access(local if local.is_dir() else root, os.W_OK | os.X_OK):
        return local
    return user_state_home() / "workspaces" / digest(str(root))[:24]


class Store:
    """Per-workspace state: content-addressed originals, events, the request ledger and the task card.

    Each (session, agent) pair is an isolated scope. Tables written by plugin versions before 1.0
    (bridges, runners) are left untouched in existing databases and simply no longer read.
    """

    def __init__(self, root: str | Path, session: str = "default", agent: str = "main", timeout: float = 10):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir() or not session or not agent:
            raise ContextError("workspace, session and agent are required")
        self.scope = digest(canonical([session, agent]))[:24]
        self.session, self.agent = session, agent
        self.directory = state_directory(self.root)
        if self.directory.parent != self.root:
            self.directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        ignore = self.directory / ".gitignore"
        if not ignore.exists() and not ignore.is_symlink():
            # Archived command output may contain secrets; never let it reach Git.
            atomic_write(ignore, b"*\n")
        database = self.directory / "state.sqlite3"
        if database.is_symlink():
            raise ContextError("database cannot be a symlink")
        self.db = sqlite3.connect(database, timeout=timeout, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 4:
            self.db.close()
            raise ContextError("state was created by a newer plugin; upgrade instead of opening it")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS scopes(scope TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          generation INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, scope TEXT NOT NULL, sha TEXT NOT NULL,
          kind TEXT NOT NULL, title TEXT NOT NULL, metadata TEXT NOT NULL, UNIQUE(scope,sha,kind,title));
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
          source_key TEXT NOT NULL, kind TEXT NOT NULL, artifact TEXT NOT NULL, metadata TEXT NOT NULL,
          at TEXT NOT NULL DEFAULT '', UNIQUE(scope,source_key));
        CREATE TABLE IF NOT EXISTS requirements(id TEXT NOT NULL, scope TEXT NOT NULL, source TEXT NOT NULL,
          text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', evidence TEXT NOT NULL DEFAULT '[]',
          note TEXT NOT NULL DEFAULT '', superseded_by TEXT, PRIMARY KEY(scope,id));
        CREATE TABLE IF NOT EXISTS requirement_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
          requirement TEXT NOT NULL, old_status TEXT NOT NULL, new_status TEXT NOT NULL, evidence TEXT NOT NULL, note TEXT NOT NULL,
          at TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS card_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, item TEXT NOT NULL,
          old_status TEXT NOT NULL, new_status TEXT NOT NULL, note TEXT NOT NULL, at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS card_changes_item ON card_changes(scope,item,seq);
        CREATE TABLE IF NOT EXISTS supervisor_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, trigger TEXT NOT NULL,
          mode TEXT NOT NULL, status TEXT NOT NULL, pid INTEGER, upto INTEGER, started REAL NOT NULL, finished REAL,
          screen TEXT, verdict TEXT, usage TEXT, error TEXT, delivered INTEGER NOT NULL DEFAULT 0, woke INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS supervisor_marks(scope TEXT NOT NULL, segment TEXT NOT NULL, trigger TEXT NOT NULL,
          PRIMARY KEY(scope, segment, trigger));
        CREATE TABLE IF NOT EXISTS evidence_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, status TEXT NOT NULL,
          pid INTEGER, refs TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0, started REAL NOT NULL, finished REAL,
          error TEXT, usage TEXT, summaries INTEGER NOT NULL DEFAULT 0, reported INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS evidence_summaries(scope TEXT NOT NULL, ref TEXT NOT NULL, sha TEXT NOT NULL,
          summary TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}', tokens INTEGER NOT NULL, run INTEGER, at TEXT NOT NULL,
          members TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(scope,ref));
        CREATE TABLE IF NOT EXISTS evidence_members(scope TEXT NOT NULL, ref TEXT NOT NULL, owner TEXT NOT NULL,
          PRIMARY KEY(scope,ref));
        CREATE INDEX IF NOT EXISTS evidence_summaries_sha ON evidence_summaries(scope,sha);
        CREATE TABLE IF NOT EXISTS evidence_restores(scope TEXT PRIMARY KEY, at TEXT NOT NULL, upto INTEGER NOT NULL,
          seen INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS evidence_delivered(scope TEXT NOT NULL, ref TEXT NOT NULL, at TEXT NOT NULL,
          PRIMARY KEY(scope,ref));
        CREATE TABLE IF NOT EXISTS user_messages(scope TEXT NOT NULL,source TEXT NOT NULL,sha TEXT NOT NULL,
          class TEXT NOT NULL,item TEXT,PRIMARY KEY(scope,source));
        CREATE INDEX IF NOT EXISTS user_messages_sha ON user_messages(scope,sha);
        CREATE TABLE IF NOT EXISTS card_items(scope TEXT NOT NULL,id TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,
          text TEXT NOT NULL,sources TEXT NOT NULL DEFAULT '[]',note TEXT NOT NULL DEFAULT '',tags TEXT NOT NULL DEFAULT '[]',
          repeats INTEGER NOT NULL DEFAULT 0,merged_into TEXT,origin TEXT NOT NULL DEFAULT 'user',ordinal INTEGER NOT NULL,
          touched INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL DEFAULT '',status_at TEXT NOT NULL DEFAULT '',
          seen_status TEXT NOT NULL DEFAULT '',PRIMARY KEY(scope,id));
        CREATE TABLE IF NOT EXISTS hook_turns(scope TEXT NOT NULL,turn_id TEXT NOT NULL,source_key TEXT NOT NULL,
          opened REAL NOT NULL,closed REAL,worked INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(scope,turn_id));
        CREATE TABLE IF NOT EXISTS work_files(scope TEXT NOT NULL,path TEXT NOT NULL,reads INTEGER NOT NULL DEFAULT 0,
          edits INTEGER NOT NULL DEFAULT 0,last_seq INTEGER NOT NULL DEFAULT 0,last_action TEXT NOT NULL,sha TEXT,
          inside INTEGER NOT NULL DEFAULT 1,detail TEXT NOT NULL DEFAULT '',PRIMARY KEY(scope,path));
        CREATE TABLE IF NOT EXISTS transcript_sync(scope TEXT PRIMARY KEY,path TEXT NOT NULL,inode INTEGER NOT NULL,
          offset INTEGER NOT NULL,resets INTEGER NOT NULL DEFAULT 0,next_ref INTEGER NOT NULL DEFAULT 1,
          post_reset INTEGER NOT NULL DEFAULT 0,awaiting INTEGER NOT NULL DEFAULT 0,injected INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS idle_resets(scope TEXT PRIMARY KEY,blocked_at REAL NOT NULL DEFAULT 0,
          blocked_tokens INTEGER NOT NULL DEFAULT 0,ignored INTEGER NOT NULL DEFAULT 0,last_reset REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS shadow_entries(id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,kind TEXT NOT NULL,
          text TEXT NOT NULL,tokens INTEGER NOT NULL,ref TEXT);
        CREATE INDEX IF NOT EXISTS shadow_entries_scope ON shadow_entries(scope,id);
        CREATE TABLE IF NOT EXISTS shadow_refs(scope TEXT NOT NULL,ref TEXT NOT NULL,call_id TEXT,kind TEXT NOT NULL,
          tool TEXT NOT NULL DEFAULT '',command TEXT NOT NULL DEFAULT '',artifact TEXT,tokens INTEGER NOT NULL DEFAULT 0,
          prompt TEXT,entry INTEGER,images INTEGER NOT NULL DEFAULT 0,line_at INTEGER,PRIMARY KEY(scope,ref));
        CREATE INDEX IF NOT EXISTS shadow_refs_call ON shadow_refs(scope,call_id);
        CREATE TABLE IF NOT EXISTS shadow_digests(id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,upto INTEGER NOT NULL,
          text TEXT NOT NULL,open_items TEXT NOT NULL DEFAULT '[]',tokens INTEGER NOT NULL,input_tokens INTEGER NOT NULL,
          next_trigger INTEGER NOT NULL,usage TEXT NOT NULL DEFAULT '{}',created REAL NOT NULL,reported INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sidecar_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,status TEXT NOT NULL,
          pid INTEGER,upto INTEGER NOT NULL,body_tokens INTEGER NOT NULL,started REAL NOT NULL,finished REAL,error TEXT,
          usage TEXT NOT NULL DEFAULT '{}',reported INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS sidecar_runs_scope ON sidecar_runs(scope,id);
        CREATE TABLE IF NOT EXISTS constraint_checks(scope TEXT NOT NULL,turn_id TEXT NOT NULL,item TEXT NOT NULL,
          path TEXT NOT NULL,PRIMARY KEY(scope,turn_id,item,path));
        CREATE TABLE IF NOT EXISTS file_versions(scope TEXT NOT NULL,path TEXT NOT NULL,seq INTEGER NOT NULL,sha TEXT NOT NULL,
          PRIMARY KEY(scope,path,seq));
        CREATE INDEX IF NOT EXISTS file_versions_seq ON file_versions(scope,seq);
        CREATE TABLE IF NOT EXISTS change_notes(scope TEXT NOT NULL,id TEXT NOT NULL,sha TEXT NOT NULL,files TEXT NOT NULL,
          task TEXT NOT NULL DEFAULT '',motive TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',risk TEXT NOT NULL DEFAULT '',
          tests TEXT NOT NULL DEFAULT '',seq INTEGER NOT NULL DEFAULT 0,time TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(scope,id),UNIQUE(scope,sha));
        CREATE TABLE IF NOT EXISTS plans(scope TEXT NOT NULL,id TEXT NOT NULL,sha TEXT NOT NULL,text TEXT NOT NULL,
          seq INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,PRIMARY KEY(scope,id),UNIQUE(scope,sha));
        CREATE TABLE IF NOT EXISTS protected_docs(scope TEXT NOT NULL,path TEXT NOT NULL,origin TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '',noticed TEXT,PRIMARY KEY(scope,path));
        CREATE INDEX IF NOT EXISTS requirements_source ON requirements(scope,source);
        CREATE INDEX IF NOT EXISTS events_scope_seq ON events(scope,seq);
        CREATE INDEX IF NOT EXISTS artifacts_scope_sha ON artifacts(scope,sha);
        CREATE INDEX IF NOT EXISTS events_scope_artifact ON events(scope,artifact);
        CREATE INDEX IF NOT EXISTS events_reviews ON events(scope,seq)
          WHERE json_array_length(json_extract(metadata,'$.needs_review'))>0;
        """)
        # Additive migrations preserve every original, requirement and uncertain operation.
        for table, column, declaration in (
            # A mirrored tool output's images, and where its line starts in the session log (evidence.py reads them there).
            ("shadow_refs", "images", "INTEGER NOT NULL DEFAULT 0"),
            ("shadow_refs", "line_at", "INTEGER"),
            # The outputs one evidence summary covers (a step of several); '[]' for one output (1.2.0).
            ("evidence_summaries", "members", "TEXT NOT NULL DEFAULT '[]'"),
            # Older plugin versions keep inserting rows without these; card sync classifies them later.
            ("requirements", "class", "TEXT"),
            ("requirements", "item", "TEXT"),
            ("hook_turns", "idle_reset", "INTEGER NOT NULL DEFAULT 0"),
            # The model updated the task card in this turn (context_state action=card); see card_request.
            ("hook_turns", "carded", "INTEGER NOT NULL DEFAULT 0"),
            # Reminders given while the turn worked (1 change note, 2 code comments), and whether the next message
            # already asked for the statuses it left unset (card.followups); never a hold of the finished turn.
            ("hook_turns", "reminded", "INTEGER NOT NULL DEFAULT 0"),
            ("hook_turns", "followed_up", "INTEGER NOT NULL DEFAULT 0"),
            ("supervisor_runs", "woke", "INTEGER NOT NULL DEFAULT 0"),
            # Fingerprint of the session log bytes already read (shadow.py); '' for state written before it existed.
            ("transcript_sync", "anchor", "TEXT NOT NULL DEFAULT ''"),
            # "<turn id> <trigger>" of the latest turn the log started (task_started): a codex exec prompt has no
            # client's message id and is still the user's (shadow._ingest).
            ("transcript_sync", "turn_trigger", "TEXT NOT NULL DEFAULT ''"),
            # UTC time of a change note ("2026-11-09 15:20:33"); '' for notes stored before it existed.
            ("change_notes", "time", "TEXT NOT NULL DEFAULT ''"),
            # The classes, methods, functions and variables a note's change touched ('' before 1.3.0).
            ("change_notes", "symbols", "TEXT NOT NULL DEFAULT ''"),
            # UTC times ("2026-11-09 15:20:33", see util.utc_now); '' for rows written before they existed.
            ("events", "at", "TEXT NOT NULL DEFAULT ''"),
            ("requirement_changes", "at", "TEXT NOT NULL DEFAULT ''"),
            ("card_items", "created", "TEXT NOT NULL DEFAULT ''"),
            # When the item's status last changed, and the status card_changes last recorded for it.
            ("card_items", "status_at", "TEXT NOT NULL DEFAULT ''"),
            ("card_items", "seen_status", "TEXT NOT NULL DEFAULT ''"),
            # The order plans were last proposed in (a plan proposed again moves up), and the card items of the
            # requests that asked for it (plans.requests); 0 and '[]' for plans stored before (1.2.7).
            ("plans", "proposed", "INTEGER NOT NULL DEFAULT 0"),
            ("plans", "requests", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(" + table + ")")}
            if column not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        self.db.execute("PRAGMA user_version=4")
        self.db.execute("INSERT OR IGNORE INTO scopes(scope) VALUES(?)", (self.scope,))
        self.db.execute("COMMIT")
        database.chmod(0o600)
        self._verified_objects = {}

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def row(self):
        return self.db.execute("SELECT * FROM scopes WHERE scope=?", (self.scope,)).fetchone()

    def bump(self):
        self.db.execute("UPDATE scopes SET revision=revision+1 WHERE scope=?", (self.scope,))

    def put(self, raw: bytes | str, kind: str, title: str, metadata: dict | None = None) -> str:
        raw = raw.encode() if isinstance(raw, str) else raw
        if len(raw) > 16 * 1024 * 1024:
            raise ContextError("artifact exceeds 16 MiB; split the source into explicit versioned sections")
        sha = digest(raw)
        identifier = "a" + digest(canonical([self.scope, sha, kind, title]))[:20]
        path = self.directory / "objects" / self.scope / sha
        try:
            info = path.lstat()
        except FileNotFoundError:
            atomic_write(path, raw)
            info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ContextError("object must be a regular file, not a symlink")
        signature = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if self._verified_objects.get(sha) != signature:
            existing = read_scoped(self.directory, f"objects/{self.scope}/{sha}")
            if digest(existing) != sha:
                raise ContextError("existing immutable object is corrupt; it was not overwritten")
            self._verified_objects[sha] = signature
        self.db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?)",
                        (identifier, self.scope, sha, kind, title, canonical({**(metadata or {}), "_bytes": len(raw)})))
        return identifier

    def artifact(self, identifier: str) -> tuple[dict, bytes]:
        meta = self.artifact_metadata(identifier)
        raw = read_scoped(self.directory, f"objects/{self.scope}/{meta['sha']}")
        if digest(raw) != meta["sha"]:
            raise ContextError("artifact integrity check failed")
        return meta, raw

    def artifact_metadata(self, identifier: str) -> dict:
        row = self.db.execute("SELECT * FROM artifacts WHERE id=? AND scope=?", (identifier, self.scope)).fetchone()
        if row is None:
            raise ContextError("unknown artifact in this session/agent")
        return {**dict(row), "metadata": json.loads(row["metadata"])}

    def archive_stream(self, stream, title: str, chunk_bytes: int = 4 * 1024 * 1024):
        """Exact byte chunks; avoid cutting a valid UTF-8 character across references."""
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        refs, pending = [], b""
        while chunk := stream.read(chunk_bytes):
            chunk = pending + chunk
            pending = b""
            try:
                chunk.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.end == len(chunk) and exc.reason == "unexpected end of data":
                    chunk, pending = chunk[:exc.start], chunk[exc.start:]
            if chunk:
                refs.append(self.put(chunk, "round-stream", title + " part " + str(len(refs))))
        if pending:
            refs.append(self.put(pending, "round-stream", title + " part " + str(len(refs))))
        return refs

    def append(self, source_key: str, kind: str, text: str, metadata: dict | None = None):
        if not source_key or not isinstance(text, str):
            raise ContextError("source_key and text are required")
        metadata = metadata or {}
        with self.transaction():
            old = self.db.execute("SELECT * FROM events WHERE scope=? AND source_key=?",
                                  (self.scope, source_key)).fetchone()
            if old:
                _, raw = self.artifact(old["artifact"])
                if old["kind"] != kind or raw.decode() != text or json.loads(old["metadata"]) != metadata:
                    raise ContextError("source key reused with different content")
                return dict(old)
            artifact = self.put(text, kind, kind + " " + source_key, metadata)
            cursor = self.db.execute("INSERT INTO events(scope,source_key,kind,artifact,metadata,at) VALUES(?,?,?,?,?,?)",
                                    (self.scope, source_key, kind, artifact, canonical(metadata), utc_now()))
            self.bump()
            return dict(self.db.execute("SELECT * FROM events WHERE seq=?", (cursor.lastrowid,)).fetchone())

    def events(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE scope=? ORDER BY seq", (self.scope,))]

    def recent_evidence(self, limit: int = 8):
        rows = self.db.execute("SELECT artifact,kind,metadata FROM events WHERE scope=? AND kind IN ('tool','native_change','native_mcp') "
                               "ORDER BY seq DESC LIMIT ?", (self.scope, limit)).fetchall()
        result = []
        for row in rows:
            metadata = json.loads(row["metadata"])
            result.append({"ref": row["artifact"], "kind": row["kind"],
                           "command_or_tool": str(metadata.get("command", metadata.get("tool_name", "")))[:240],
                           "exit_code": metadata.get("exit_code"), "status": metadata.get("status")})
        return result

    def requirements(self):
        return [{**dict(r), "evidence": json.loads(r["evidence"])} for r in self.db.execute(
            "SELECT * FROM requirements WHERE scope=? ORDER BY CAST(substr(id,2) AS INTEGER)", (self.scope,))]

    def state(self):
        return json.loads(self.row()["state"])

    def checkpoint(self, summary: str, next_steps: list[str], focus: str, important_refs: list[str] | None = None):
        if not isinstance(summary, str) or not isinstance(focus, str) or not isinstance(next_steps, list) or not all(isinstance(x, str) for x in next_steps):
            raise ContextError("checkpoint requires summary, focus and string next_steps")
        value = {"summary": summary, "next_steps": next_steps, "focus": focus}
        if important_refs is not None:
            if not isinstance(important_refs, list) or len(important_refs) > 32 or not all(isinstance(ref, str) for ref in important_refs):
                raise ContextError("important_refs must contain at most 32 in-scope artifact references")
            for ref in important_refs:
                if self.artifact_metadata(ref)["kind"] == "user":
                    raise ContextError("original requests are already mandatory; pin material references instead")
            value["important_refs"] = list(dict.fromkeys(important_refs))
        with self.transaction():
            state = self.state()
            if important_refs is None and "important_refs" in state:
                value["important_refs"] = state["important_refs"]
            if {key: state.get(key) for key in value} != value:
                # Other state (the latest change of direction) stays; the revision dates the checkpoint.
                kept = {k: v for k, v in state.items() if k not in ("summary", "next_steps", "focus", "important_refs")}
                stored = {**kept, **value, "checkpoint_revision": self.row()["revision"]}
                self.db.execute("UPDATE scopes SET state=? WHERE scope=?", (canonical(stored), self.scope))
                self.bump()
        return value

    def capture_file(self, path: str, source_key: str):
        raw = read_scoped(self.root, path)
        relative = str(Path(path).relative_to(self.root) if Path(path).is_absolute() else Path(path))
        identifier = self.put(raw, "file", relative, {"path": relative, "version": digest(raw)})
        self.append(source_key, "file", identifier, {"path": relative, "artifact": identifier})
        return identifier
