import os
import sqlite3
import tempfile
import unittest

import app as application


class DatabaseConnectionLifecycleTests(unittest.TestCase):
    def test_sqlite_connection_closes_after_context_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            old_path = application.DB_PATH
            application.DB_PATH = os.path.join(directory, "lifecycle.db")
            try:
                with application.db() as conn:
                    conn.execute("CREATE TABLE lifecycle_check(value INTEGER)")
                    conn.execute("INSERT INTO lifecycle_check(value) VALUES(1)")
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")
            finally:
                application.DB_PATH = old_path


if __name__ == "__main__":
    unittest.main()


import unittest

import app as application


class StaticAssetPerformanceTests(unittest.TestCase):
    def test_static_assets_are_cached_for_one_day(self):
        self.assertEqual(application.app.get_send_file_max_age("style.css"), 86400)


if __name__ == "__main__":
    unittest.main()
