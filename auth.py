"""Session-based account routes for SRC Match Analyst."""
import os
import sqlite3
import hashlib
import hmac
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for, current_app
from werkzeug.security import check_password_hash, generate_password_hash

auth_bp = Blueprint("auth", __name__)


def _db():
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(url, connect_timeout=10, row_factory=dict_row)
        return conn
    path = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "src_match_analyst.db"))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _execute(conn, "PRAGMA foreign_keys=ON")
    return conn


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


def _record_auth_failure(email):
    now = datetime.utcnow().isoformat()
    with _db() as conn:
        for kind, value in (("email", email), ("ip", request.remote_addr or "unknown")):
            _execute(conn, "INSERT INTO auth_attempts(key_hash,attempted_at) VALUES(?,?)",
                     (_rate_key(kind, value), now))


def _clear_auth_failures(email):
    with _db() as conn:
        for kind, value in (("email", email), ("ip", request.remote_addr or "unknown")):
            _execute(conn, "DELETE FROM auth_attempts WHERE key_hash=?", (_rate_key(kind, value),))


def _init_rate_limit_table():
    with _db() as conn:
        if os.environ.get("DATABASE_URL", "").strip():
            _execute(conn, """CREATE TABLE IF NOT EXISTS auth_attempts(
                id BIGSERIAL PRIMARY KEY, key_hash TEXT NOT NULL, attempted_at TEXT NOT NULL)""")
        else:
            _execute(conn, """CREATE TABLE IF NOT EXISTS auth_attempts(
                id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT NOT NULL, attempted_at TEXT NOT NULL)""")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_auth_attempts_key_time ON auth_attempts(key_hash,attempted_at)")



def current_user_id():
    return session.get("user_id")


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        email = (session.get("user_email") or "").casefold()
        allowed = {item.strip().casefold() for item in os.environ.get("ADMIN_EMAILS", "").split(",") if item.strip()}
        if not current_user_id() or not allowed or email not in allowed:
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
        if len(email) > 254 or "@" not in email:
            flash("Geçerli bir e-posta adresi girin.", "error")
            return render_template("auth.html", mode="register")
        if len(password) < 10:
            flash("Şifre en az 10 karakter olmalı.", "error")
            return render_template("auth.html", mode="register")
        with _db() as conn:
            existing = _execute(conn, "SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if existing:
                flash("Bu e-posta zaten kayıtlı.", "error")
                return render_template("auth.html", mode="register")
            cur = _execute(conn, 
                "INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?) RETURNING id",
                (email, generate_password_hash(password), datetime.utcnow().isoformat()),
            )
            user = cur.fetchone()
            user_id = user["id"] if isinstance(user, dict) else user[0]
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
        if not user or not check_password_hash(user["password_hash"], password):
            _record_auth_failure(email)
            flash("E-posta veya şifre hatalı.", "error")
            return render_template("auth.html", mode="login")
        _clear_auth_failures(email)
        session.clear()
        session["user_id"] = user["id"]
        session["user_email"] = user["email"]
        target = request.args.get("next", "")
        if not target.startswith("/") or target.startswith("//"):
            target = url_for("index")
        return redirect(target)
    return render_template("auth.html", mode="login")


@auth_bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
