import os
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

import app as application


class DailyPredictionRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = application.DB_PATH
        application.DB_PATH = os.path.join(self.tempdir.name, "prediction-sync.db")
        application.init_db()
        self.old_token = os.environ.get("RESULT_SYNC_TOKEN")
        os.environ["RESULT_SYNC_TOKEN"] = "prediction-sync-test-token"
        self.client = application.app.test_client()

    def tearDown(self):
        application.DB_PATH = self.old_db_path
        if self.old_token is None:
            os.environ.pop("RESULT_SYNC_TOKEN", None)
        else:
            os.environ["RESULT_SYNC_TOKEN"] = self.old_token
        self.tempdir.cleanup()

    def _insert_todays_fixtures(self, count):
        kickoff = (datetime.now(ZoneInfo("Europe/Istanbul")) + timedelta(minutes=30)).isoformat()
        with application.db() as c:
            for i in range(count):
                c.execute(
                    """INSERT INTO matches(
                        home,away,league,kickoff,status,created_at,
                        market_probabilities,match_result
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        f"Home {i}", f"Away {i}", "Test League", kickoff,
                        "scheduled", "test", "{}", ""
                    ),
                )

    def test_prediction_batches_are_bounded_and_continue_until_empty(self):
        self._insert_todays_fixtures(10)
        calls = []

        def fake_rebuild(c, target, history):
            calls.append(target["id"])
            c.execute(
                "UPDATE matches SET market_probabilities=?,match_result=? WHERE id=?",
                ('{"match_result":{"1":0.5,"X":0.25,"2":0.25}}', "MS 1", target["id"]),
            )

        with patch.object(application, "prepare_history", side_effect=lambda rows: rows), \
             patch.object(application, "rebuild_one_prediction", side_effect=fake_rebuild):
            first = self.client.post(
                "/sync/predictions/refresh",
                headers={"Authorization": "Bearer prediction-sync-test-token"},
                json={},
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.get_json()["processed"], 8)
            self.assertEqual(first.get_json()["generated"], 8)
            self.assertEqual(first.get_json()["remaining"], 2)

            second = self.client.post(
                "/sync/predictions/refresh",
                headers={"Authorization": "Bearer prediction-sync-test-token"},
                json={},
            )
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.get_json()["processed"], 2)
            self.assertEqual(second.get_json()["remaining"], 0)

        self.assertEqual(len(calls), 10)

    def test_missing_or_wrong_token_is_rejected(self):
        response = self.client.post("/sync/predictions/refresh", json={})
        self.assertEqual(response.status_code, 401)


    def test_prediction_unavailable_rows_do_not_block_later_batches(self):
        self._insert_todays_fixtures(10)
        with application.db() as c:
            c.execute(
                """UPDATE matches SET prediction_message=?
                   WHERE id=(SELECT MIN(id) FROM matches
                    WHERE substr(kickoff,1,10)=?)""",
                ("Yeterli geçmiş veri bulunamadı.", datetime.now(ZoneInfo("Europe/Istanbul")).date().isoformat()),
            )

        calls = []

        def fake_rebuild(c, target, history):
            calls.append(target["id"])
            c.execute(
                "UPDATE matches SET market_probabilities=?,match_result=? WHERE id=?",
                ('{"match_result":{"1":0.5,"X":0.25,"2":0.25}}', "MS 1", target["id"]),
            )

        with patch.object(application, "prepare_history", side_effect=lambda rows: rows), \
             patch.object(application, "rebuild_one_prediction", side_effect=fake_rebuild):
            first = self.client.post(
                "/sync/predictions/refresh",
                headers={"Authorization": "Bearer prediction-sync-test-token"},
                json={},
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["processed"], 8)
        self.assertEqual(first.get_json()["remaining"], 1)
        self.assertEqual(len(calls), 8)



if __name__ == "__main__":
    unittest.main()
