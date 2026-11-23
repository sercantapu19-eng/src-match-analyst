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

    def tearDown(self):
        application.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def test_start_creates_resumable_history_job(self):
        response = self.client.post("/sync/history/start")
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
            response = self.client.post("/sync/history/step", json={"job_id": job_id})

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
        started = self.client.post("/sync/history/start").get_json()
        restored = self.client.get("/sync/history/status").get_json()
        self.assertEqual(restored["job_id"], started["job_id"])
        self.assertEqual(restored["phase"], "history")

    def test_second_start_reuses_active_job(self):
        first = self.client.post("/sync/history/start").get_json()
        second = self.client.post("/sync/history/start").get_json()
        self.assertEqual(first["job_id"], second["job_id"])

if __name__ == "__main__":
    unittest.main()
