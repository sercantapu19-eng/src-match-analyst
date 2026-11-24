import os
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

import app as application


class DailyFixtureSyncTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = application.DB_PATH
        application.DB_PATH = os.path.join(self.tempdir.name, "daily-sync.db")
        application.init_db()
        with application.db() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS user_saved_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                match_id INTEGER NOT NULL,
                saved_at TEXT NOT NULL
            )""")
        self.old_token = os.environ.get("RESULT_SYNC_TOKEN")
        os.environ["RESULT_SYNC_TOKEN"] = "test-result-token"
        self.client = application.app.test_client()

    def tearDown(self):
        application.DB_PATH = self.old_db_path
        if self.old_token is None:
            os.environ.pop("RESULT_SYNC_TOKEN", None)
        else:
            os.environ["RESULT_SYNC_TOKEN"] = self.old_token
        self.tempdir.cleanup()

    def test_scheduled_fixtures_are_imported_for_today(self):
        today = datetime.now(ZoneInfo("Europe/Istanbul")).date().isoformat()
        kickoff = f"{today}T20:00:00+03:00"

        class FakeProvider:
            def __init__(self, *args, **kwargs):
                pass

            @staticmethod
            def _normalize_search_name(value):
                return " ".join(str(value or "").casefold().replace("fc", "").split())

            def fixtures(self, day):
                if day != today:
                    return []
                return [{
                    "id": "today-fixture-1",
                    "home": "Home FC",
                    "away": "Away FC",
                    "league": "Test League",
                    "kickoff": kickoff,
                    "status": "scheduled",
                    "status_short": "NS",
                    "home_goals": None,
                    "away_goals": None,
                }]

        with patch.object(application, "FootballProvider", FakeProvider):
            response = self.client.post(
                "/sync/results/refresh",
                headers={"Authorization": "Bearer test-result-token"},
                json={},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["upcoming_added"], 1)
        self.assertEqual(payload["upcoming_ready"], 1)
        with application.db() as c:
            row = c.execute(
                "SELECT home,away,status,provider,external_id FROM matches WHERE external_id=?",
                ("today-fixture-1",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(row["provider"], "sportscore")


if __name__ == "__main__":
    unittest.main()
