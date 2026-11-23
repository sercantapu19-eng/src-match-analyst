import os
import tempfile
import unittest
import app as application
from unittest.mock import patch

class HistorySyncRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = application.DB_PATH
        application.DB_PATH = os.path.join(self.tempdir.name, "test.db")
        application.init_db()
        self.client = application.app.test_client()
        self.old_admin_emails = os.environ.get("ADMIN_EMAILS")
        os.environ["ADMIN_EMAILS"] = "admin@example.com"
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["user_email"] = "admin@example.com"
            session["_csrf_token"] = "test-csrf-token"

    def tearDown(self):
        application.DB_PATH = self.old_db_path
        if self.old_admin_emails is None:
            os.environ.pop("ADMIN_EMAILS", None)
        else:
            os.environ["ADMIN_EMAILS"] = self.old_admin_emails
        self.tempdir.cleanup()

    def test_start_creates_resumable_history_job(self):
        response = self.client.post("/sync/history/start", headers={"X-CSRF-Token": "test-csrf-token"})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["phase"], "history")
        self.assertEqual(payload["teams_total"], 0)
        self.assertEqual(payload["total_days"], 180)
        self.assertIsNotNone(payload["job_id"])

    def test_team_history_step_imports_team_fixtures_and_moves_to_predictions(self):
        with application.db() as c:
            c.execute(
                "INSERT INTO matches(home,away,kickoff,status,created_at) VALUES(?,?,?,?,?)",
                ("Home FC", "Away FC", "2099-01-01T18:00:00+03:00", "scheduled", "test")
            )
            cur = c.execute("""INSERT INTO sync_jobs(
                phase,status,start_date,end_date,cursor_date,total_days,team_names,team_index,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                "team_history", "running", "2026-01-01", "2026-06-30", "2026-06-30",
                180, '["Home FC"]', 0, "test", "test"
            ))
            job_id = cur.lastrowid

        class FakeProvider:
            def __init__(self, *args, **kwargs):
                pass
            def team_fixtures_for_team(self, team_name, limit=30):
                return [{
                    "id": "past-1", "home": "Home FC", "away": "Opponent FC",
                    "league": "Test League", "kickoff": "2026-11-01T15:00:00+03:00",
                    "status": "finished", "home_goals": 2, "away_goals": 1,
                    "ht_home": 1, "ht_away": 0, "status_short": "FT"
                }]

        with patch.object(application, "FootballProvider", FakeProvider):
            response = self.client.post("/sync/history/step", json={"job_id": job_id}, headers={"X-CSRF-Token": "test-csrf-token"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["phase"], "predictions")
        self.assertEqual(payload["team_index"], 1)
        self.assertEqual(payload["teams_total"], 1)
        self.assertEqual(payload["fixtures_count"], 1)
        self.assertEqual(payload["predictions_total"], 1)
        with application.db() as c:
            imported = c.execute("SELECT home_goals,away_goals FROM matches WHERE external_id='past-1'").fetchone()
        self.assertEqual(tuple(imported), (2, 1))

    def test_status_endpoint_restores_last_job(self):
        started = self.client.post("/sync/history/start", headers={"X-CSRF-Token": "test-csrf-token"}).get_json()
        restored = self.client.get("/sync/history/status").get_json()
        self.assertEqual(restored["job_id"], started["job_id"])
        self.assertEqual(restored["phase"], "history")

    def test_second_start_reuses_active_job(self):
        first = self.client.post("/sync/history/start", headers={"X-CSRF-Token": "test-csrf-token"}).get_json()
        second = self.client.post("/sync/history/start", headers={"X-CSRF-Token": "test-csrf-token"}).get_json()
        self.assertEqual(first["job_id"], second["job_id"])

    def test_live_refresh_updates_minute_and_score(self):
        with application.db() as c:
            c.execute(
                """INSERT INTO matches(home,away,league,kickoff,status,created_at,provider,external_id,
                   home_goals,away_goals,status_short,elapsed_minute)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("Home FC", "Away FC", "Test League", "2026-11-23T12:00:00+03:00",
                 "live", "test", "sportscore", "live-1", 1, 0, "1H", 12)
            )

        class FakeProvider:
            def __init__(self, *args, **kwargs):
                pass
            @staticmethod
            def _normalize_search_name(value):
                return " ".join(str(value or "").casefold().replace("fc", "").split())
            def live_matches(self, limit=50):
                return [{
                    "id": "live-1", "home": "Home FC", "away": "Away FC",
                    "league": "Test League", "kickoff": "2026-11-23T12:00:00+03:00",
                    "status": "live", "status_short": "1H", "elapsed_minute": 37,
                    "home_goals": 2, "away_goals": 1
                }]

        with patch.object(application, "FootballProvider", FakeProvider):
            response = self.client.get("/sync/live/refresh?date=2026-11-23")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["matches"][0]["minute_label"], "Canlı 37′")
        self.assertEqual(payload["matches"][0]["score"], "2 – 1")
        with application.db() as c:
            saved = c.execute(
                "SELECT elapsed_minute,home_goals,away_goals FROM matches WHERE external_id='live-1'"
            ).fetchone()
        self.assertEqual(tuple(saved), (37, 2, 1))

if __name__ == "__main__":
    unittest.main()
