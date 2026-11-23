"""Session-based account routes for SRC Match Analyst."""
import os
import sqlite3
from datetime import datetime
from functools import wraps

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
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

def current_user_id():
    return session.get("user_id")


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
        with _db() as conn:
            user = _execute(conn, "SELECT id,email,password_hash FROM users WHERE email=?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            flash("E-posta veya şifre hatalı.", "error")
            return render_template("auth.html", mode="login")
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
