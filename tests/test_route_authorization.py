import os
import unittest

import app as app_module


class RouteAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.previous_admin_emails = os.environ.get("ADMIN_EMAILS")
        os.environ.pop("ADMIN_EMAILS", None)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        if self.previous_admin_emails is None:
            os.environ.pop("ADMIN_EMAILS", None)
        else:
            os.environ["ADMIN_EMAILS"] = self.previous_admin_emails

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
                response = self.client.open(path, method=method, json={} if path.endswith("/step") else None)
                self.assertEqual(response.status_code, 403)

    def test_login_required_routes_redirect_anonymous_requests(self):
        for path in ("/saved", "/sync/history/status"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
