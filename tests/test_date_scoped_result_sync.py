import os
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

import app as application


class DateScopedResultSyncTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = application.DB_PATH
        application.DB_PATH = os.path.join(self.tempdir.name, "date-sync.db")
        application.init_db()
        self.old_token = os.environ.get("RESULT_SYNC_TOKEN")
        os.environ["RESULT_SYNC_TOKEN"] = "date-sync-test-token"
        self.client = application.app.test_client()

    def tearDown(self):
        application.DB_PATH = self.old_db_path
        if self.old_token is None:
            os.environ.pop("RESULT_SYNC_TOKEN", None)
        else:
            os.environ["RESULT_SYNC_TOKEN"] = self.old_token
        self.tempdir.cleanup()

    def test_date_query_fetches_only_the_requested_day(self):
        today = datetime.now(ZoneInfo("Europe/Istanbul")).date().isoformat()
        kickoff = f"{today}T20:00:00+03:00"
        calls = []

        class FakeProvider:
            def __init__(self, *args, **kwargs):
                pass

            @staticmethod
            def _normalize_search_name(value):
                return " ".join(str(value or "").casefold().replace("fc", "").split())

            def fixtures(self, day):
                calls.append(day)
                if day != today:
                    return []
                return [{
                    "id": "date-scoped-fixture",
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
                f"/sync/results/refresh?date={today}",
                headers={"Authorization": "Bearer date-sync-test-token"},
                json={},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["days"], [today])
        self.assertEqual(payload["fetched"], 1)
        self.assertEqual(payload["upcoming_added"], 1)
        self.assertEqual(calls, [today])
        with application.db() as c:
            row = c.execute(
                "SELECT home,away,status FROM matches WHERE external_id=?",
                ("date-scoped-fixture",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "scheduled")

    def test_invalid_or_out_of_range_date_is_rejected(self):
        response = self.client.post(
            "/sync/results/refresh?date=not-a-date",
            headers={"Authorization": "Bearer date-sync-test-token"},
            json={},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
