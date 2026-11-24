import os
import tempfile
import unittest
from datetime import datetime, timedelta
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


    def test_existing_finished_match_is_updated_without_a_duplicate(self):
        yesterday = (
            datetime.now(ZoneInfo("Europe/Istanbul")).date() - timedelta(days=1)
        ).isoformat()
        kickoff = f"{yesterday}T18:00:00+03:00"
        with application.db() as c:
            cur = c.execute(
                """INSERT INTO matches(
                    home,away,league,kickoff,status,created_at,provider,external_id,
                    match_result,over25
                ) VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                (
                    "Home FC", "Away FC", "Test League", kickoff, "finished",
                    "test", "sportscore", "", "MS 1", "Alt 2,5"
                ),
            )
            match_id = cur.fetchone()["id"]

        class FakeProvider:
            def __init__(self, *args, **kwargs):
                pass

            @staticmethod
            def _normalize_search_name(value):
                return " ".join(str(value or "").casefold().replace("fc", "").split())

            def fixtures(self, day):
                if day != yesterday:
                    return []
                return [{
                    "id": "provider-finished-id",
                    "home": "Home FC",
                    "away": "Away FC",
                    "league": "Test League",
                    "kickoff": kickoff,
                    "status": "finished",
                    "status_short": "FT",
                    "home_goals": 2,
                    "away_goals": 0,
                    "ht_home": 1,
                    "ht_away": 0,
                }]

        with patch.object(application, "FootballProvider", FakeProvider):
            response = self.client.post(
                f"/sync/results/refresh?date={yesterday}",
                headers={"Authorization": "Bearer date-sync-test-token"},
                json={},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["finished"], 1)
        self.assertEqual(payload["evaluated_markets"], 2)
        with application.db() as c:
            match = c.execute(
                "SELECT id,home_goals,away_goals,status FROM matches WHERE id=?",
                (match_id,),
            ).fetchone()
            count = c.execute(
                "SELECT COUNT(*) FROM matches WHERE substr(kickoff,1,10)=?",
                (yesterday,),
            ).fetchone()[0]
            outcomes = c.execute(
                "SELECT market,actual,outcome FROM results WHERE match_id=? ORDER BY market",
                (match_id,),
            ).fetchall()
        self.assertEqual(match["status"], "finished")
        self.assertEqual((match["home_goals"], match["away_goals"]), (2, 0))
        self.assertEqual(count, 1)
        self.assertEqual(
            [(row["actual"], row["outcome"]) for row in outcomes],
            [("MS 1", "won"), ("Alt 2,5", "won")],
        )


if __name__ == "__main__":
    unittest.main()
