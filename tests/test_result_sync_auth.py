import os
import unittest

import app as app_module


class ScheduledResultSyncAuthTests(unittest.TestCase):
    def setUp(self):
        self.previous = os.environ.pop("RESULT_SYNC_TOKEN", None)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        os.environ.pop("RESULT_SYNC_TOKEN", None)
        if self.previous is not None:
            os.environ["RESULT_SYNC_TOKEN"] = self.previous

    def test_missing_token_is_rejected_before_provider_access(self):
        response = self.client.post("/sync/results/refresh")
        self.assertEqual(response.status_code, 401)

    def test_wrong_token_is_rejected_before_provider_access(self):
        os.environ["RESULT_SYNC_TOKEN"] = "expected-secret"
        response = self.client.post(
            "/sync/results/refresh",
            headers={"Authorization": "Bearer wrong-secret"},
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()