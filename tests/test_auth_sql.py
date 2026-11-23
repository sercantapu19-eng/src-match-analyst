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


class RegistrationSuccessRateLimitTests(unittest.TestCase):
    def test_successful_signups_are_limited_per_ip(self):
        import os
        import tempfile
        import auth
        from app import app

        with tempfile.TemporaryDirectory() as directory:
            previous_path = os.environ.get("DATABASE_PATH")
            os.environ["DATABASE_PATH"] = os.path.join(directory, "signup-limit.db")
            try:
                auth.init_auth_db()
                for number in range(10):
                    client = app.test_client()
                    with client.session_transaction() as session:
                        session["_csrf_token"] = "csrf-test"
                    response = client.post("/register", data={
                        "email": f"user{number}@example.com",
                        "password": "long-enough-password",
                        "csrf_token": "csrf-test",
                    })
                    self.assertEqual(response.status_code, 302)
                blocked_client = app.test_client()
                with blocked_client.session_transaction() as session:
                    session["_csrf_token"] = "csrf-test"
                blocked = blocked_client.post("/register", data={
                    "email": "eleventh@example.com",
                    "password": "long-enough-password",
                    "csrf_token": "csrf-test",
                })
                self.assertEqual(blocked.status_code, 429)
            finally:
                if previous_path is None:
                    os.environ.pop("DATABASE_PATH", None)
                else:
                    os.environ["DATABASE_PATH"] = previous_path


class LoginRateLimitTests(unittest.TestCase):
    def test_registration_is_throttled_after_ten_invalid_attempts(self):
        import os
        import tempfile
        import auth
        from app import app

        with tempfile.TemporaryDirectory() as directory:
            previous_path = os.environ.get("DATABASE_PATH")
            os.environ["DATABASE_PATH"] = os.path.join(directory, "register-test.db")
            try:
                auth.init_auth_db()
                client = app.test_client()
                with client.session_transaction() as session:
                    session["_csrf_token"] = "csrf-test"
                for _ in range(10):
                    response = client.post("/register", data={
                        "email": "not-an-email",
                        "password": "long-enough-password",
                        "csrf_token": "csrf-test",
                    })
                    self.assertEqual(response.status_code, 200)
                blocked = client.post("/register", data={
                    "email": "not-an-email",
                    "password": "long-enough-password",
                    "csrf_token": "csrf-test",
                })
                self.assertEqual(blocked.status_code, 429)
            finally:
                if previous_path is None:
                    os.environ.pop("DATABASE_PATH", None)
                else:
                    os.environ["DATABASE_PATH"] = previous_path

    def test_redirect_target_rejects_external_and_backslash_urls(self):
        from auth import _safe_next_target
        from app import app

        with app.test_request_context("/login"):
            self.assertEqual(_safe_next_target("https://example.com"), "/")
            self.assertEqual(_safe_next_target("//example.com"), "/")
            self.assertEqual(_safe_next_target("/\\\\example.com"), "/")
            self.assertEqual(_safe_next_target("/saved?view=all"), "/saved?view=all")

    def test_login_is_throttled_after_ten_failed_attempts(self):
        import os
        import tempfile
        import auth
        from app import app

        with tempfile.TemporaryDirectory() as directory:
            previous_path = os.environ.get("DATABASE_PATH")
            os.environ["DATABASE_PATH"] = os.path.join(directory, "auth-test.db")
            try:
                auth.init_auth_db()
                client = app.test_client()
                with client.session_transaction() as session:
                    session["_csrf_token"] = "csrf-test"
                for _ in range(10):
                    response = client.post("/login", data={
                        "email": "unknown@example.com",
                        "password": "wrong-password",
                        "csrf_token": "csrf-test",
                    })
                    self.assertEqual(response.status_code, 200)
                blocked = client.post("/login", data={
                    "email": "unknown@example.com",
                    "password": "wrong-password",
                    "csrf_token": "csrf-test",
                })
                self.assertEqual(blocked.status_code, 429)
            finally:
                if previous_path is None:
                    os.environ.pop("DATABASE_PATH", None)
                else:
                    os.environ["DATABASE_PATH"] = previous_path


if __name__ == "__main__":
    unittest.main()
