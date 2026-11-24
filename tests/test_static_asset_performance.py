import unittest

import app as application


class StaticAssetPerformanceTests(unittest.TestCase):
    def test_static_assets_are_cached_for_one_day(self):
        with application.app.app_context():
            self.assertEqual(application.app.get_send_file_max_age("style.css"), 86400)


if __name__ == "__main__":
    unittest.main()
