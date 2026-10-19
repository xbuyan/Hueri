"""Offline tests for the pure parts of scripts/mcp_demo.py (no Docker, no Postgres)."""
import datetime as dt
import importlib.util
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from app.mcp_server import serialize as ser

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mcp_demo.py"
spec = importlib.util.spec_from_file_location("mcp_demo", SCRIPT)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class EnvFileTests(unittest.TestCase):
    def test_round_trip_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "demo.env"
            values = {"A": "1", "MCP_ALLOWED_ORIGINS": "", "URL": "postgresql+asyncpg://u:p@h:1/d"}
            demo.write_env(path, values)
            self.assertEqual(demo.read_env(path), values)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(demo.read_env(Path("/nonexistent/none.env")), {})


class GrantTests(unittest.TestCase):
    def test_role_gets_select_on_two_tables_only(self):
        grants = demo.grant_statements()
        selects = [g for g in grants if g.startswith("GRANT SELECT")]
        self.assertEqual(selects, [f"GRANT SELECT ON tenders, tender_analyses TO {demo.RO_ROLE}"])
        self.assertFalse(any(w in g for g in grants for w in ("INSERT", "UPDATE", "DELETE", "ALL PRIVILEGES")
                             if g.startswith("GRANT")))
        self.assertTrue(any("default_transaction_read_only = on" in g for g in grants))
        self.assertTrue(any(g.startswith("REVOKE CREATE ON SCHEMA") for g in grants))


class SampleDataTests(unittest.TestCase):
    def setUp(self):
        self.tenders, self.analyses = demo.sample_rows()

    def test_everything_is_labelled_as_sample(self):
        for t in self.tenders:
            self.assertTrue(t["is_mock"])
            self.assertTrue(t["title"].startswith("[SAMPLE]"))
            self.assertTrue(t["external_id"].startswith("SAMPLE-"))

    def test_ids_and_references_are_consistent(self):
        ids = [t["id"] for t in self.tenders]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len({a["tender_id"] for a in self.analyses}), len(self.analyses))
        for a in self.analyses:
            self.assertIn(a["tender_id"], ids)

    def test_covers_every_state_the_demo_needs(self):
        analysed = {a["tender_id"] for a in self.analyses}
        unreviewed = [t for t in self.tenders if t["is_unreviewed"]]
        pending = [t for t in self.tenders if not t["is_unreviewed"] and t["id"] not in analysed]
        self.assertTrue(unreviewed and pending)
        self.assertTrue(any(a["is_fit"] for a in self.analyses) and any(not a["is_fit"] for a in self.analyses))
        self.assertTrue(any(t["buyer"] is None for t in self.tenders))
        parsed = [ser.parse_deadline(t["deadline_str"])["parsed"] for t in self.tenders]
        self.assertTrue(any(parsed) and not all(parsed))

    def test_analysis_arrays_are_lists_of_strings(self):
        for a in self.analyses:
            for key in ("matched_services", "eligibility_gaps", "suggested_next_steps"):
                self.assertIsInstance(a[key], list)
                self.assertTrue(all(isinstance(x, str) for x in a[key]))
            self.assertTrue(0 <= a["relevance_score"] <= 10)
            self.assertIn(a["status"], {"NEW", "UNDER_REVIEW", "BIDDING", "DISCARDED"})

    def test_objects_build_from_the_real_models(self):
        tender_objs, analysis_objs = demo.build_objects(self.tenders, self.analyses)
        self.assertEqual(len(tender_objs), len(self.tenders))
        self.assertEqual(len(analysis_objs), len(self.analyses))
        self.assertEqual(analysis_objs[0].status.value, "NEW")


class SqliteCopyTests(unittest.TestCase):
    def test_copies_tenders_and_analyses_only(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from app.models import Base, User

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.db"
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)
            tenders, analyses = demo.sample_rows()
            t_objs, a_objs = demo.build_objects(tenders[:3], [a for a in analyses if a["tender_id"] <= 3])
            with Session(engine) as session:
                session.add_all(t_objs)
                session.flush()
                session.add_all(a_objs)
                session.commit()
            engine.dispose()

            got_tenders, got_analyses = demo.read_sqlite_rows(str(path))
            self.assertEqual([t["id"] for t in got_tenders], [1, 2, 3])
            self.assertEqual(len(got_analyses), 3)
            self.assertIsInstance(got_analyses[0]["matched_services"], list)
            self.assertIsInstance(got_tenders[0]["created_at"], dt.datetime)
            self.assertIs(got_tenders[0]["is_mock"], True)
            self.assertEqual(got_analyses[0]["status"], "NEW")
            self.assertNotIn("password", " ".join(got_tenders[0]).lower())
            # the copy must be readable by the loader that writes to Postgres
            demo.build_objects(got_tenders, got_analyses)
            # source is opened read-only
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DELETE FROM tenders")
            con.close()

    def test_unreadable_source_is_reported(self):
        with self.assertRaises(SystemExit):
            demo.read_sqlite_rows("/nonexistent/none.db")


class CliTests(unittest.TestCase):
    def test_help_lists_every_command(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), self.assertRaises(SystemExit):
            demo.main(["--help"])
        text = buffer.getvalue()
        for command in ("up", "seed", "verify", "run", "check", "down"):
            self.assertIn(command, text)

    def test_server_start_command_disables_access_logs(self):
        self.assertIn('"--no-access-log"', SCRIPT.read_text())


if __name__ == "__main__":
    unittest.main()
