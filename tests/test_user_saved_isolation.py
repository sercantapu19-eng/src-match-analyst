import os
import tempfile
import unittest
from datetime import date

import app as application
import auth


class SavedMatchIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = application.DB_PATH
        self.old_env_db_path = os.environ.get("DATABASE_PATH")
        database_path = os.path.join(self.tempdir.name, "multi-user-test.db")
        os.environ["DATABASE_PATH"] = database_path
        application.DB_PATH = database_path
        application.init_db()
        auth.init_auth_db()

        with auth._db() as conn:
            self.user_one = auth._execute(
                conn,
                "INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?) RETURNING id",
                ("one@example.com", "test-hash", "test"),
            ).fetchone()[0]
            self.user_two = auth._execute(
                conn,
                "INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?) RETURNING id",
                ("two@example.com", "test-hash", "test"),
            ).fetchone()[0]

        with application.db() as conn:
            self.match_one = conn.execute(
                "INSERT INTO matches(home,away,kickoff,status,created_at) VALUES(?,?,?,?,?) RETURNING id",
                ("Alpha FC", "Beta FC", date.today().isoformat() + "T18:00:00+03:00", "scheduled", "test"),
            ).fetchone()["id"]
            self.match_two = conn.execute(
                "INSERT INTO matches(home,away,kickoff,status,created_at) VALUES(?,?,?,?,?) RETURNING id",
                ("Gamma FC", "Delta FC", date.today().isoformat() + "T20:00:00+03:00", "scheduled", "test"),
            ).fetchone()["id"]

        self.client = application.app.test_client()

    def tearDown(self):
        application.DB_PATH = self.old_db_path
        if self.old_env_db_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self.old_env_db_path
        self.tempdir.cleanup()

    def _login_as(self, user_id, email):
        with self.client.session_transaction() as session:
            session.clear()
            session["user_id"] = user_id
            session["user_email"] = email
            session["_csrf_token"] = "isolation-test-token"

    def _save(self, match_id):
        return self.client.post(
            f"/match/{match_id}/save",
            headers={"X-CSRF-Token": "isolation-test-token"},
        )

    def test_saved_matches_are_isolated_between_accounts(self):
        self._login_as(self.user_one, "one@example.com")
        self.assertEqual(self._save(self.match_one).status_code, 302)
        first_account_page = self.client.get("/saved").get_data(as_text=True)
        self.assertIn("Alpha FC", first_account_page)
        self.assertNotIn("Gamma FC", first_account_page)

        self._login_as(self.user_two, "two@example.com")
        self.assertEqual(self._save(self.match_two).status_code, 302)
        second_account_page = self.client.get("/saved").get_data(as_text=True)
        self.assertIn("Gamma FC", second_account_page)
        self.assertNotIn("Alpha FC", second_account_page)

    def test_save_returns_to_match_and_detail_has_navigation_links(self):
        self._login_as(self.user_one, "one@example.com")
        response = self._save(self.match_one)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/match/{self.match_one}")

        detail = self.client.get(f"/match/{self.match_one}").get_data(as_text=True)
        self.assertIn("Bültene dön", detail)
        self.assertIn("Kaydedilen maçlar", detail)
        self.assertIn("★ Kaydedildi", detail)

    def test_saved_page_has_mobile_friendly_return_and_remove_actions(self):
        self._login_as(self.user_one, "one@example.com")
        self._save(self.match_one)
        response = self.client.get("/saved")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Bültene dön", page)
        self.assertIn("Alpha FC", page)
        self.assertIn("Listemden çıkar", page)

    def test_individual_coupon_prediction_saves_without_redirect_and_is_account_scoped(self):
        self._login_as(self.user_one, "one@example.com")
        with application.db() as conn:
            conn.execute(
                "UPDATE matches SET market_probabilities=? WHERE id=?",
                ('{"goals":{"2.5":{"over":0.82,"under":0.18}}}', self.match_one),
            )
        response = self.client.post(
            f"/match/{self.match_one}/save-prediction",
            data={"csrf_token":"isolation-test-token","market_key":"goals","selection_key":"over"},
            headers={"X-CSRF-Token":"isolation-test-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["saved"])
        with application.db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM user_saved_predictions WHERE user_id=? AND match_id=?",
                (self.user_one, self.match_one),
            ).fetchone()[0]
        self.assertEqual(count, 1)
        page = self.client.get("/saved").get_data(as_text=True)
        self.assertIn("Kaydettiğim tahminler", page)
        self.assertIn("over", page)

        self._login_as(self.user_two, "two@example.com")
        other_page = self.client.get("/saved").get_data(as_text=True)
        self.assertNotIn("Alpha FC", other_page)

    def test_individual_prediction_save_is_idempotent(self):
        self._login_as(self.user_one, "one@example.com")
        with application.db() as conn:
            conn.execute(
                "UPDATE matches SET market_probabilities=? WHERE id=?",
                ('{"btts":{"Var":0.75,"Yok":0.25}}', self.match_one),
            )
        for _ in range(2):
            response = self.client.post(
                f"/match/{self.match_one}/save-prediction",
                data={"csrf_token":"isolation-test-token","market_key":"btts","selection_key":"Var"},
                headers={"X-CSRF-Token":"isolation-test-token"},
            )
            self.assertEqual(response.status_code, 200)
        with application.db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM user_saved_predictions WHERE user_id=? AND match_id=? AND market_key='btts' AND selection_key='Var'",
                (self.user_one, self.match_one),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_regular_user_does_not_see_admin_match_controls(self):
        self._login_as(self.user_one, "one@example.com")
        response = self.client.get(f"/match/{self.match_one}")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('action="/match/%s/status"' % self.match_one, page)
        self.assertNotIn('action="/match/%s/result"' % self.match_one, page)


if __name__ == "__main__":
    unittest.main()
