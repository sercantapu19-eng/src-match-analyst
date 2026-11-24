import os
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import app as application


class BulletinFixtureFilteringTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = application.DB_PATH
        self.old_database_path_env = os.environ.get("DATABASE_PATH")
        application.DB_PATH = os.path.join(self.tempdir.name, "bulletin.db")
        os.environ["DATABASE_PATH"] = application.DB_PATH
        application.init_db()
        application.init_auth_db()
        self.client = application.app.test_client()
        self.day = datetime.now(ZoneInfo("Europe/Istanbul")).date().isoformat()
        self.now = datetime.now(ZoneInfo("Europe/Istanbul"))

    def tearDown(self):
        application.DB_PATH = self.old_db_path
        if self.old_database_path_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self.old_database_path_env
        self.tempdir.cleanup()

    def add_match(self, home, away, kickoff, status="scheduled", league="Test League"):
        with application.db() as conn:
            conn.execute(
                """INSERT INTO matches(home,away,league,kickoff,status,created_at,provider,external_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (home, away, league, kickoff.isoformat(), status, "test", "sportscore", "")
            )

    def test_past_kickoffs_are_separated_from_upcoming_without_marking_finished(self):
        past = self.now - timedelta(hours=2)
        future = self.now + timedelta(hours=2)
        self.add_match("Past FC", "Old United", past)
        self.add_match("Future FC", "New United", future)

        upcoming = self.client.get("/?date=" + self.day + "&view=upcoming")
        self.assertEqual(upcoming.status_code, 200)
        upcoming_html = upcoming.get_data(as_text=True)
        self.assertIn("Future FC", upcoming_html)
        self.assertNotIn("Past FC", upcoming_html)
        self.assertIn("Oynanmamış maçlar <span>1</span>", upcoming_html)

        unverified = self.client.get("/?date=" + self.day + "&view=unverified")
        self.assertEqual(unverified.status_code, 200)
        unverified_html = unverified.get_data(as_text=True)
        self.assertIn("Past FC", unverified_html)
        self.assertNotIn("Saati geçti", unverified_html)
        self.assertNotIn("Saati geçti · doğrulanacak", unverified_html)
        self.assertNotIn("Future FC", unverified_html)
        with application.db() as conn:
            row = conn.execute(
                "SELECT status FROM matches WHERE home='Past FC'"
            ).fetchone()
        self.assertEqual(row["status"], "scheduled")

    def test_missing_provider_ids_do_not_create_duplicate_history_rows(self):
        fixture = {
            "id": "", "home": "History FC", "away": "Archive United",
            "league": "Test League", "kickoff": "2026-11-20T18:00:00+03:00",
            "status": "finished", "home_goals": 2, "away_goals": 1,
            "ht_home": 1, "ht_away": 0, "status_short": "FT",
            "elapsed_minute": None
        }
        with application.db() as conn:
            application.store_fixtures(conn, [fixture, dict(fixture)])
            count = conn.execute(
                """SELECT COUNT(*) FROM matches
                   WHERE home='History FC' AND away='Archive United'
                    AND provider='sportscore' AND COALESCE(external_id,'')=''"""
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_identical_provider_rows_are_shown_once_but_not_deleted(self):
        future = self.now + timedelta(hours=1)
        self.add_match("Macau U15", "Guam U15", future)
        self.add_match("Macau U15", "Guam U15", future)

        response = self.client.get("/?date=" + self.day + "&view=upcoming")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(html.count("Macau U15"), 1)
        with application.db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM matches WHERE home='Macau U15' AND away='Guam U15'"
            ).fetchone()[0]
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
