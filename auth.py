"""Session-based account routes for SRC Match Analyst."""
import os
import sqlite3
import hashlib
import hmac
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for, current_app
from werkzeug.security import check_password_hash, generate_password_hash

auth_bp = Blueprint("auth", __name__)


class _SQLiteConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return self.connection.__exit__(exc_type, exc_value, traceback)
        finally:
            self.connection.close()


def _db():
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(url, connect_timeout=10, row_factory=dict_row)
    path = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "src_match_analyst.db"))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _execute(conn, "PRAGMA foreign_keys=ON")
    return _SQLiteConnectionContext(conn)


def _execute(conn, query, params=()):
    if os.environ.get("DATABASE_URL", "").strip():
        query = query.replace("?", "%s")
    return conn.execute(query, params)


def init_auth_db():
    with _db() as conn:
        if os.environ.get("DATABASE_URL", "").strip():
            _execute(conn, """CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL)""")
            _execute(conn, """CREATE TABLE IF NOT EXISTS user_saved_matches (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                match_id BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
                saved_at TEXT NOT NULL, UNIQUE(user_id, match_id))""")
        else:
            _execute(conn, """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL)""")
            _execute(conn, """CREATE TABLE IF NOT EXISTS user_saved_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
                saved_at TEXT NOT NULL, UNIQUE(user_id, match_id))""")
    _init_rate_limit_table()


def _rate_key(kind, value):
    secret = current_app.secret_key or "missing-secret"
    raw = f"{kind}:{value}".encode("utf-8")
    return hmac.new(str(secret).encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _auth_rate_limited(email):
    cutoff = (datetime.utcnow() - timedelta(minutes=15)).isoformat()
    keys = (_rate_key("email", email), _rate_key("ip", request.remote_addr or "unknown"))
    with _db() as conn:
        for key in keys:
            count = _execute(conn,
                "SELECT COUNT(*) AS attempts FROM auth_attempts WHERE key_hash=? AND attempted_at>=?",
                (key, cutoff)).fetchone()["attempts"]
            if count >= 10:
                return True
    return False


def _record_auth_attempt(email):
    now = datetime.utcnow().isoformat()
    prune_before = (datetime.utcnow() - timedelta(days=1)).isoformat()
    with _db() as conn:
        _execute(conn, "DELETE FROM auth_attempts WHERE attempted_at<?", (prune_before,))
        for kind, value in (("email", email), ("ip", request.remote_addr or "unknown")):
            _execute(conn, "INSERT INTO auth_attempts(key_hash,attempted_at) VALUES(?,?)",
                     (_rate_key(kind, value), now))


def _clear_auth_failures(email):
    # Keep the IP counter so a successful login to another account cannot reset it.
    with _db() as conn:
        _execute(conn, "DELETE FROM auth_attempts WHERE key_hash=?", (_rate_key("email", email),))


def _init_rate_limit_table():
    with _db() as conn:
        if os.environ.get("DATABASE_URL", "").strip():
            _execute(conn, """CREATE TABLE IF NOT EXISTS auth_attempts(
                id BIGSERIAL PRIMARY KEY, key_hash TEXT NOT NULL, attempted_at TEXT NOT NULL)""")
        else:
            _execute(conn, """CREATE TABLE IF NOT EXISTS auth_attempts(
                id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT NOT NULL, attempted_at TEXT NOT NULL)""")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_auth_attempts_key_time ON auth_attempts(key_hash,attempted_at)")




def _safe_next_target(target):
    if not target or "\\" in target or any(ord(char) < 32 for char in target):
        return url_for("index")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return url_for("index")
    return target


def current_user_id():
    return session.get("user_id")


def is_admin_user():
    user_id = current_user_id()
    if user_id is None:
        return False
    try:
        user_id = int(user_id)
        allowed = {
            int(item.strip())
            for item in os.environ.get("ADMIN_USER_IDS", "").split(",")
            if item.strip().isdigit()
        }
    except (TypeError, ValueError):
        return False
    return user_id in allowed


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin_user():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user_id():
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user_id():
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        if _auth_rate_limited(email):
            flash("Çok fazla deneme yapıldı. 15 dakika sonra tekrar deneyin.", "error")
            return render_template("auth.html", mode="register"), 429
        local, separator, domain = email.partition("@")
        valid_email = bool(separator and local and domain and "." in domain and not any(ch.isspace() for ch in email))
        if len(email) > 254 or not valid_email:
            _record_auth_attempt(email)
            flash("Geçerli bir e-posta adresi girin.", "error")
            return render_template("auth.html", mode="register")
        if len(password) < 10 or len(password) > 1024:
            _record_auth_attempt(email)
            flash("Şifre 10 ile 1024 karakter arasında olmalı.", "error")
            return render_template("auth.html", mode="register")
        # Count successful registrations as attempts to limit account creation per IP.
        _record_auth_attempt(email)
        with _db() as conn:
            existing = _execute(conn, "SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if not existing:
                cur = _execute(conn,
                    "INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?) RETURNING id",
                    (email, generate_password_hash(password), datetime.utcnow().isoformat()),
                )
                user = cur.fetchone()
                user_id = user["id"] if isinstance(user, dict) else user[0]
        if existing:
            _record_auth_failure(email)
            flash("Bu e-posta zaten kayıtlı.", "error")
            return render_template("auth.html", mode="register")
        session.clear()
        session["user_id"] = user_id
        session["user_email"] = email
        return redirect(url_for("index"))
    return render_template("auth.html", mode="register")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user_id():
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        if _auth_rate_limited(email):
            flash("Çok fazla başarısız deneme yapıldı. 15 dakika sonra tekrar deneyin.", "error")
            return render_template("auth.html", mode="login"), 429
        with _db() as conn:
            user = _execute(conn, "SELECT id,email,password_hash FROM users WHERE email=?", (email,)).fetchone()
        password_valid = bool(user and len(password) <= 1024 and check_password_hash(user["password_hash"], password))
        if not password_valid:
            _record_auth_failure(email)
            flash("E-posta veya şifre hatalı.", "error")
            return render_template("auth.html", mode="login")
        _clear_auth_failures(email)
        session.clear()
        session["user_id"] = user["id"]
        session["user_email"] = user["email"]
        target = _safe_next_target(request.args.get("next", ""))
        return redirect(target)
    return render_template("auth.html", mode="login")


@auth_bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
