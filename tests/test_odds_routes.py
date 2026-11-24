import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import app as application


class OddsRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = application.DB_PATH
        self.old_database_url = os.environ.pop("DATABASE_URL", None)
        self.old_env_db_path = os.environ.get("DATABASE_PATH")
        self.old_api_key = os.environ.pop("API_FOOTBALL_KEY", None)
        self.db_path = os.path.join(self.tempdir.name, "odds-test.db")
        os.environ["DATABASE_PATH"] = self.db_path
        application.DB_PATH = self.db_path
        application.init_db()
        with application.db() as conn:
            self.match_id = conn.execute(
                """INSERT INTO matches(home,away,league,kickoff,status,created_at)
                   VALUES(?,?,?,?,?,?) RETURNING id""",
                ("Alpha FC", "Beta FC", "Test League",
                 date.today().isoformat() + "T18:00:00+03:00", "live", "test"),
            ).fetchone()["id"]
        self.client = application.app.test_client()

    def tearDown(self):
        application.DB_PATH = self.old_db_path
        if self.old_database_url is not None:
            os.environ["DATABASE_URL"] = self.old_database_url
        if self.old_env_db_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self.old_env_db_path
        if self.old_api_key is not None:
            os.environ["API_FOOTBALL_KEY"] = self.old_api_key
        self.tempdir.cleanup()

    def test_missing_api_key_returns_clear_unconfigured_state(self):
        response = self.client.get(f"/match/{self.match_id}/odds-data")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["configured"])
        self.assertIn("API_FOOTBALL_KEY", payload["message"])

    def test_odds_are_saved_as_changed_snapshots_and_shared_between_requests(self):
        class FakeOddsProvider:
            configured = True
            pre_calls = 0
            live_calls = 0

            def __init__(self):
                pass

            def resolve_fixture(self, match):
                return {"fixture_id": 98765, "home": "Alpha FC", "away": "Beta FC"}

            def pre_match_odds(self, fixture_id):
                type(self).pre_calls += 1
                return [{
                    "source": "prematch", "bookmaker_id": "8", "bookmaker": "Example Book",
                    "market_id": "1", "market": "Match Winner", "selection": "Home",
                    "handicap": "", "odd": "2.1", "suspended": False,
                    "captured_at": "2026-11-24T18:00:00+00:00", "raw": {},
                }]

            def live_odds(self, fixture_id):
                type(self).live_calls += 1
                return [{
                    "source": "live", "bookmaker_id": "8", "bookmaker": "Example Book",
                    "market_id": "1", "market": "Match Winner", "selection": "Home",
                    "handicap": "", "odd": "2.25", "suspended": False,
                    "captured_at": "2026-11-24T18:00:15+00:00", "raw": {},
                }]

        with patch.object(application, "ApiFootballOddsProvider", FakeOddsProvider):
            first = self.client.get(f"/match/{self.match_id}/odds-data")
            second = self.client.get(f"/match/{self.match_id}/odds-data")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        payload = first.get_json()
        self.assertEqual(payload["fixture_id"], 98765)
        self.assertEqual(len(payload["first_seen"]), 1)
        self.assertEqual(payload["first_seen"][0]["odd"], "2.1")
        self.assertEqual(payload["live"][0]["odd"], "2.25")
        self.assertEqual(FakeOddsProvider.pre_calls, 1)
        self.assertEqual(FakeOddsProvider.live_calls, 1)
        with application.db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM match_odds_snapshots WHERE match_id=?",
                (self.match_id,),
            ).fetchone()["count"]
            mapping = conn.execute(
                "SELECT provider_fixture_id FROM match_odds_fixture_map WHERE match_id=?",
                (self.match_id,),
            ).fetchone()
        self.assertEqual(count, 2)
        self.assertEqual(mapping["provider_fixture_id"], 98765)


if __name__ == "__main__":
    unittest.main()
