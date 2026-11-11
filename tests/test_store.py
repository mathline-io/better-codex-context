"""State store: integrity of archived originals, scope isolation, symlink safety and schema versions."""
from __future__ import annotations
import _isolation  # noqa: F401

import os
from pathlib import Path
import stat
import unittest

from _support import StoreCase
from better_codex_context.requirements import capture
from better_codex_context.store import Store
from better_codex_context.util import ContextError


class IntegrityTests(StoreCase):
    def object_path(self, ref) -> Path:
        return self.store.directory / "objects" / self.store.scope / self.store.artifact_metadata(ref)["sha"]

    def test_tampered_object_fails_its_integrity_check(self):
        ref = self.store.put("good", "tool", "raw")
        self.object_path(ref).write_text("bad")
        with self.assertRaises(ContextError):
            self.store.artifact(ref)


class IsolationTests(StoreCase):
    def test_sessions_and_agents_cannot_read_each_others_archives(self):
        ref = self.store.put("private", "tool", "secret")
        capture(self.store, "Main agent request", "u1")
        for session, agent in (("other", "main"), (self.session, "child")):
            other = Store(self.root, session, agent)
            try:
                with self.assertRaises(ContextError):
                    other.artifact(ref)
                self.assertEqual([], other.requirements())
            finally:
                other.close()

    def test_state_directory_is_private_and_ignored_by_git(self):
        directory = self.root / ".better-codex-context"
        self.assertEqual(directory, self.store.directory)
        self.assertEqual("*\n", (directory / ".gitignore").read_text())
        if os.name != "nt":  # Windows has no POSIX modes
            self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE((directory / "state.sqlite3").stat().st_mode))


class SchemaTests(StoreCase):
    def test_newer_schema_is_rejected_without_downgrade(self):
        self.store.db.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(ContextError, "newer plugin"):
            Store(self.root, "other")
        self.assertEqual(999, self.store.db.execute("PRAGMA user_version").fetchone()[0])

    def test_older_schema_is_migrated_and_keeps_originals_and_legacy_tables(self):
        capture(self.store, "Keep this original across upgrade", "before-upgrade")
        # Recreate the pre-migration layout: columns added since, plus a table only older versions used.
        for table, column in (("shadow_refs", "images"), ("shadow_refs", "line_at"), ("hook_turns", "idle_reset")):
            self.store.db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        self.store.db.execute("CREATE TABLE runners(scope TEXT PRIMARY KEY, state TEXT)")
        self.store.db.execute("INSERT INTO runners VALUES(?, 'uncertain')", (self.store.scope,))
        self.store.db.execute("PRAGMA user_version=0")
        self.store.close()
        self.store = Store(self.root, self.session)
        self.assertEqual("Keep this original across upgrade", self.store.requirements()[0]["text"])
        self.assertEqual(4, self.store.db.execute("PRAGMA user_version").fetchone()[0])
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(shadow_refs)")}
        self.assertLessEqual({"images", "line_at"}, columns)
        self.assertEqual([("uncertain",)], [tuple(r) for r in self.store.db.execute("SELECT state FROM runners")])


if __name__ == "__main__":
    unittest.main()
