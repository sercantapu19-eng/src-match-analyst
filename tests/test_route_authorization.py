import os
import unittest

import app as app_module


class RouteAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.previous_admin_ids = os.environ.get("ADMIN_USER_IDS")
        os.environ.pop("ADMIN_USER_IDS", None)
        os.environ.pop("ADMIN_EMAILS", None)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as session:
            session["_csrf_token"] = "test-csrf-token"

    def tearDown(self):
        if self.previous_admin_ids is None:
            os.environ.pop("ADMIN_USER_IDS", None)
        else:
            os.environ["ADMIN_USER_IDS"] = self.previous_admin_ids
        os.environ.pop("ADMIN_EMAILS", None)

    def test_admin_authority_uses_user_id_not_email(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 51
            session["user_email"] = "admin@example.com"
        os.environ["ADMIN_EMAILS"] = "admin@example.com"
        self.assertEqual(self.client.get("/match/new").status_code, 403)
        os.environ["ADMIN_USER_IDS"] = "51"
        self.assertEqual(self.client.get("/match/new").status_code, 200)

    def test_admin_routes_reject_anonymous_requests(self):
        cases = [
            ("POST", "/sync/fixtures"),
            ("POST", "/sync/history/start"),
            ("POST", "/sync/history/step"),
            ("GET", "/match/new"),
            ("POST", "/match/new"),
            ("POST", "/match/1/status"),
            ("POST", "/match/1/result"),
        ]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                response = self.client.open(path, method=method, json={} if path.endswith("/step") else None, headers={"X-CSRF-Token": "test-csrf-token"})
                self.assertEqual(response.status_code, 403)

    def test_unsafe_request_without_csrf_token_is_rejected(self):
        response = self.client.post("/auth/logout")
        self.assertEqual(response.status_code, 400)

    def test_live_refresh_rejects_get_and_requires_csrf_for_post(self):
        self.assertEqual(self.client.get("/sync/live/refresh").status_code, 405)
        response = self.client.post("/sync/live/refresh", json={"date": "2026-11-23"})
        self.assertEqual(response.status_code, 400)

    def test_saved_page_redirects_anonymous_users_to_login(self):
        response = self.client.get("/saved")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_history_sync_status_is_admin_only(self):
        self.assertEqual(self.client.get("/sync/history/status").status_code, 403)


if __name__ == "__main__":
    unittest.main()
