import os
import tempfile
import unittest
import app as application

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

    def test_second_start_reuses_active_job(self):
        first = self.client.post("/sync/history/start").get_json()
        second = self.client.post("/sync/history/start").get_json()
        self.assertEqual(first["job_id"], second["job_id"])

if __name__ == "__main__":
    unittest.main()
