import unittest

from auth import _execute


class AuthSqlCompatibilityTests(unittest.TestCase):
    def test_sqlite_parameter_binding_and_user_tables(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _execute(conn, "CREATE TABLE users(id INTEGER PRIMARY KEY, email TEXT)")
        _execute(conn, "INSERT INTO users(email) VALUES(?)", ("test@example.com",))
        row = _execute(conn, "SELECT email FROM users WHERE email=?", ("test@example.com",)).fetchone()
        self.assertEqual(row["email"], "test@example.com")
        conn.close()


if __name__ == "__main__":
    unittest.main()
