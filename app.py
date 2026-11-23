import os, sqlite3, json, time, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from football_provider import FootballProvider, ProviderError
from prediction_engine import predict, prepare_history
from auth import auth_bp, current_user_id, init_auth_db, login_required, admin_required

app=Flask(__name__); app.secret_key=os.environ.get("SECRET_KEY","change-this-before-deploy")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER") or os.environ.get("FLASK_ENV")=="production"))
DB_PATH=os.environ.get("DATABASE_PATH",os.path.join(os.path.dirname(__file__),"src_match_analyst.db"))
MARKETS=[("match_result","Maç Sonucu"),("htft","İY/MS"),("over25","2,5 Gol"),("btts","KG"),("first_half_btts","1. Yarı KG"),("second_half_btts","2. Yarı KG"),("corners","Korner Alt/Üst")]

class HybridRow(dict):
 def __init__(self, columns, values):
  super().__init__(zip(columns, values))
  self._columns=tuple(columns)
 def __getitem__(self, key):
  if isinstance(key, int): key=self._columns[key]
  return super().__getitem__(key)

def postgres_row_factory(cursor):
 columns=[item.name for item in cursor.description] if cursor.description else []
 return lambda values: HybridRow(columns, values)

def translate_postgres_sql(query):
 sql=query.replace("?", "%s")
 if re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", sql, flags=re.IGNORECASE):
  sql=re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", sql, flags=re.IGNORECASE).rstrip().rstrip(";")
  sql += " ON CONFLICT DO NOTHING"
 sql=re.sub(r"datetime\(([^)]+)\)\s*>=\s*datetime\('now'\)", r"CAST(\1 AS timestamptz) >= NOW()", sql, flags=re.IGNORECASE)
 return sql

class PostgresConnection:
 def __init__(self, url):
  import psycopg
  self._connection=psycopg.connect(url, connect_timeout=10, row_factory=postgres_row_factory)
 def __enter__(self):
  self._connection.__enter__()
  return self
 def __exit__(self, exc_type, exc_value, traceback):
  return self._connection.__exit__(exc_type, exc_value, traceback)
 def execute(self, query, params=()):
  return self._connection.execute(translate_postgres_sql(query), params)

def db():
 database_url=os.environ.get("DATABASE_URL","").strip()
 if database_url:
  return PostgresConnection(database_url)
 c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c


def migrate_legacy_sqlite_to_postgres():
 legacy_path=DB_PATH
 if not legacy_path or not os.path.isfile(legacy_path):
  app.logger.warning("Neon is enabled, but no legacy SQLite file was found at DATABASE_PATH; legacy records were not migrated.")
  return {"status":"legacy_file_not_found","rows":{}}
 source=sqlite3.connect(f"file:{legacy_path}?mode=ro",uri=True)
 source.row_factory=sqlite3.Row
 try:
  source_tables={row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
  if "matches" not in source_tables:
   app.logger.warning("Legacy SQLite file has no matches table; no legacy data was migrated.")
   return {"status":"legacy_schema_not_found","rows":{}}
  with db() as target:
   target.execute("""CREATE TABLE IF NOT EXISTS data_migrations(
    migration_key TEXT PRIMARY KEY,completed_at TEXT NOT NULL,details TEXT DEFAULT '{}')""")
   done=target.execute("SELECT 1 FROM data_migrations WHERE migration_key=?",("sqlite_to_neon_v1",)).fetchone()
   if done:
    return {"status":"already_migrated","rows":{}}
   counts={}
   for table in ("matches","saved_matches","results","sync_log","sync_jobs"):
    if table not in source_tables:
     continue
    source_columns=[row[1] for row in source.execute(f"PRAGMA table_info({table})").fetchall()]
    target_columns=[row["column_name"] for row in target.execute(
     "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=? ORDER BY ordinal_position",
     (table,)).fetchall()]
    columns=[name for name in source_columns if name in target_columns]
    if not columns:
     continue
    quoted=",".join('"'+name.replace('"','""')+'"' for name in columns)
    placeholders=",".join(["%s"]*len(columns))
    insert=f"INSERT INTO {table}({quoted}) VALUES({placeholders}) ON CONFLICT DO NOTHING"
    cursor=source.execute(f"SELECT {quoted} FROM {table}")
    copied=0
    while True:
     batch=cursor.fetchmany(500)
     if not batch:
      break
     for record in batch:
      target.execute(insert,tuple(record[name] for name in columns))
      copied+=1
    counts[table]=copied
    if "id" in columns:
     target.execute(
      f"SELECT setval(pg_get_serial_sequence('{table}','id'), GREATEST(COALESCE(MAX(id),1),1), COUNT(*)>0) FROM {table}"
     )
   target.execute(
    "INSERT INTO data_migrations(migration_key,completed_at,details) VALUES(?,?,?) ON CONFLICT(migration_key) DO NOTHING",
    ("sqlite_to_neon_v1",datetime.utcnow().isoformat(),json.dumps(counts,ensure_ascii=False))
   )
  app.logger.info("Legacy SQLite import to Neon completed: %s",counts)
  return {"status":"completed","rows":counts}
 finally:
  source.close()


def init_db():
 if os.environ.get("DATABASE_URL","").strip():
  with db() as c:
   c.execute("""CREATE TABLE IF NOT EXISTS matches(
    id BIGSERIAL PRIMARY KEY,home TEXT NOT NULL,away TEXT NOT NULL,league TEXT DEFAULT '',
    kickoff TEXT DEFAULT '',status TEXT DEFAULT 'scheduled',odds_home TEXT DEFAULT '',odds_draw TEXT DEFAULT '',
    odds_away TEXT DEFAULT '',htft TEXT DEFAULT '',over25 TEXT DEFAULT '',btts TEXT DEFAULT '',
    first_half_btts TEXT DEFAULT '',second_half_btts TEXT DEFAULT '',corners TEXT DEFAULT '',
    notes TEXT DEFAULT '',created_at TEXT NOT NULL,provider TEXT DEFAULT '',external_id TEXT DEFAULT '',
    home_goals INTEGER,away_goals INTEGER,ht_home INTEGER,ht_away INTEGER,status_short TEXT DEFAULT '',
    elapsed_minute TEXT,match_result TEXT DEFAULT '',prediction_confidence TEXT DEFAULT '{}',
    market_probabilities TEXT DEFAULT '{}',prediction_message TEXT DEFAULT '',analysis_notes TEXT DEFAULT '')""")
   c.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS elapsed_minute TEXT")
   c.execute("ALTER TABLE matches ALTER COLUMN elapsed_minute TYPE TEXT USING elapsed_minute::TEXT")
   c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_fixture ON matches(provider,external_id) WHERE external_id <> ''")
   c.execute("""CREATE TABLE IF NOT EXISTS saved_matches(
    id BIGSERIAL PRIMARY KEY,match_id BIGINT NOT NULL UNIQUE REFERENCES matches(id) ON DELETE CASCADE,saved_at TEXT NOT NULL)""")
   c.execute("""CREATE TABLE IF NOT EXISTS results(
    id BIGSERIAL PRIMARY KEY,match_id BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    market TEXT NOT NULL,prediction TEXT NOT NULL,actual TEXT DEFAULT '',outcome TEXT DEFAULT 'pending',
    UNIQUE(match_id,market))""")
   c.execute("""CREATE TABLE IF NOT EXISTS sync_log(
    id BIGSERIAL PRIMARY KEY,provider TEXT,day TEXT,ran_at TEXT,fixtures_count INTEGER DEFAULT 0,status TEXT,message TEXT)""")
   c.execute("""CREATE TABLE IF NOT EXISTS sync_jobs(
    id BIGSERIAL PRIMARY KEY,phase TEXT NOT NULL,status TEXT NOT NULL,start_date TEXT NOT NULL,end_date TEXT NOT NULL,
    cursor_date TEXT NOT NULL,total_days INTEGER NOT NULL,processed_days INTEGER DEFAULT 0,fixtures_count INTEGER DEFAULT 0,
    predictions_total INTEGER DEFAULT 0,predictions_processed INTEGER DEFAULT 0,fetched_count INTEGER DEFAULT 0,
    skipped_count INTEGER DEFAULT 0,attempts INTEGER DEFAULT 0,last_error TEXT DEFAULT '',
    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,team_names TEXT DEFAULT '[]',team_index INTEGER DEFAULT 0)""")
   c.execute("ALTER TABLE sync_jobs ADD COLUMN IF NOT EXISTS team_names TEXT DEFAULT '[]'")
   c.execute("ALTER TABLE sync_jobs ADD COLUMN IF NOT EXISTS team_index INTEGER DEFAULT 0")
  migration=migrate_legacy_sqlite_to_postgres()
  if migration["status"] == "legacy_file_not_found":
   app.logger.warning("Legacy data transfer requires a reachable SQLite file or a separate export.")
  return
 with db() as c:
  c.execute("""CREATE TABLE IF NOT EXISTS matches(
   id INTEGER PRIMARY KEY AUTOINCREMENT,home TEXT NOT NULL,away TEXT NOT NULL,league TEXT DEFAULT '',
   kickoff TEXT DEFAULT '',status TEXT DEFAULT 'scheduled',odds_home TEXT DEFAULT '',odds_draw TEXT DEFAULT '',
   odds_away TEXT DEFAULT '',htft TEXT DEFAULT '',over25 TEXT DEFAULT '',btts TEXT DEFAULT '',
   first_half_btts TEXT DEFAULT '',second_half_btts TEXT DEFAULT '',corners TEXT DEFAULT '',
   notes TEXT DEFAULT '',created_at TEXT NOT NULL,provider TEXT DEFAULT '',external_id TEXT DEFAULT '',
   home_goals INTEGER,away_goals INTEGER,ht_home INTEGER,ht_away INTEGER,status_short TEXT DEFAULT '',elapsed_minute INTEGER,match_result TEXT DEFAULT '',prediction_confidence TEXT DEFAULT '{}',market_probabilities TEXT DEFAULT '{}',prediction_message TEXT DEFAULT '',analysis_notes TEXT DEFAULT '')""")
  cols={r[1] for r in c.execute("PRAGMA table_info(matches)").fetchall()}
  for name,typ in [("provider","TEXT DEFAULT ''"),("external_id","TEXT DEFAULT ''"),("home_goals","INTEGER"),("away_goals","INTEGER"),("ht_home","INTEGER"),("ht_away","INTEGER"),("status_short","TEXT DEFAULT ''"),("elapsed_minute","INTEGER"),("match_result","TEXT DEFAULT ''"),("prediction_confidence","TEXT DEFAULT '{}'"),("market_probabilities","TEXT DEFAULT '{}'"),("prediction_message","TEXT DEFAULT ''"),("analysis_notes","TEXT DEFAULT ''")]:
   if name not in cols: c.execute(f"ALTER TABLE matches ADD COLUMN {name} {typ}")
  c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_fixture ON matches(provider,external_id) WHERE external_id <> ''")
  c.execute("""CREATE TABLE IF NOT EXISTS saved_matches(id INTEGER PRIMARY KEY AUTOINCREMENT,match_id INTEGER NOT NULL UNIQUE,saved_at TEXT NOT NULL,FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE)""")
  c.execute("""CREATE TABLE IF NOT EXISTS results(id INTEGER PRIMARY KEY AUTOINCREMENT,match_id INTEGER NOT NULL,market TEXT NOT NULL,prediction TEXT NOT NULL,actual TEXT DEFAULT '',outcome TEXT DEFAULT 'pending',FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,UNIQUE(match_id,market))""")
  c.execute("""CREATE TABLE IF NOT EXISTS sync_log(id INTEGER PRIMARY KEY AUTOINCREMENT,provider TEXT,day TEXT,ran_at TEXT,fixtures_count INTEGER DEFAULT 0,status TEXT,message TEXT)""")
  c.execute("""CREATE TABLE IF NOT EXISTS sync_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,phase TEXT NOT NULL,status TEXT NOT NULL,start_date TEXT NOT NULL,end_date TEXT NOT NULL,cursor_date TEXT NOT NULL,total_days INTEGER NOT NULL,processed_days INTEGER DEFAULT 0,fixtures_count INTEGER DEFAULT 0,predictions_total INTEGER DEFAULT 0,predictions_processed INTEGER DEFAULT 0,fetched_count INTEGER DEFAULT 0,skipped_count INTEGER DEFAULT 0,attempts INTEGER DEFAULT 0,last_error TEXT DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
  job_cols={r[1] for r in c.execute("PRAGMA table_info(sync_jobs)").fetchall()}
  for name,typ in [("fetched_count","INTEGER DEFAULT 0"),("skipped_count","INTEGER DEFAULT 0"),("team_names","TEXT DEFAULT '[]'"),("team_index","INTEGER DEFAULT 0")]:
   if name not in job_cols: c.execute(f"ALTER TABLE sync_jobs ADD COLUMN {name} {typ}")

def sync_prediction_rows(c,mid):
 m=c.execute("SELECT * FROM matches WHERE id=?",(mid,)).fetchone()
 for k,label in MARKETS:
  if m[k]:
   c.execute("INSERT INTO results(match_id,market,prediction) VALUES(?,?,?) ON CONFLICT(match_id,market) DO UPDATE SET prediction=excluded.prediction",(mid,label,m[k]))


def generate_prediction(c, mid):
 m=c.execute("SELECT * FROM matches WHERE id=?",(mid,)).fetchone()
 if not m or m["status"] != "scheduled": return
 history=c.execute("SELECT id,home,away,league,status,home_goals,away_goals,ht_home,ht_away FROM matches WHERE status='finished' AND home_goals IS NOT NULL AND away_goals IS NOT NULL ORDER BY kickoff").fetchall()
 data,message=predict(dict(m),[dict(x) for x in history])
 if not data:
  c.execute("UPDATE matches SET prediction_message=?,prediction_confidence='{}',market_probabilities='{}',analysis_notes='' WHERE id=?",(message,mid))
  return
 for key,value in data["predictions"].items():
  c.execute("UPDATE matches SET "+key+"=? WHERE id=?",(value,mid))
  label=dict(MARKETS).get(key)
  if label:
   c.execute("INSERT INTO results(match_id,market,prediction) VALUES(?,?,?) ON CONFLICT(match_id,market) DO UPDATE SET prediction=excluded.prediction",(mid,label,value))
 c.execute("UPDATE matches SET prediction_confidence=?,market_probabilities=?,prediction_message='',analysis_notes=? WHERE id=?",(json.dumps(data["confidence"],ensure_ascii=False),json.dumps(data.get("market_probabilities",{}),ensure_ascii=False),data["notes"],mid))

def provider_ready():
 return True  # SportScore supports public access; API key is optional

def store_fixtures(c, fixtures):
 stored=0
 for f in fixtures:
  # Historical sync must only persist completed matches with a real full-time score.
  if f["status"] != "finished" or f["home_goals"] is None or f["away_goals"] is None:
   continue
  c.execute("""INSERT INTO matches(home,away,league,kickoff,status,created_at,provider,external_id,
    home_goals,away_goals,ht_home,ht_away,status_short,elapsed_minute)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(provider,external_id) WHERE external_id <> '' DO UPDATE SET
    home=excluded.home,away=excluded.away,league=excluded.league,kickoff=excluded.kickoff,
    status=excluded.status,home_goals=excluded.home_goals,away_goals=excluded.away_goals,
    ht_home=excluded.ht_home,ht_away=excluded.ht_away,status_short=excluded.status_short,elapsed_minute=excluded.elapsed_minute""",
    (f["home"],f["away"],f["league"],f["kickoff"],f["status"],datetime.utcnow().isoformat(),
     "sportscore",str(f["id"]),f["home_goals"],f["away_goals"],f["ht_home"],f["ht_away"],f["status_short"],f.get("elapsed_minute")))
  stored+=1
 return {"fetched":len(fixtures),"stored":stored,"skipped":len(fixtures)-stored}

def rebuild_one_prediction(c, target, history):
 try:
  data,message=predict(dict(target),history)
  if not data:
   c.execute("""UPDATE matches SET match_result='',htft='',over25='',btts='',first_half_btts='',second_half_btts='',
    prediction_message=?,prediction_confidence='{}',market_probabilities='{}',analysis_notes='' WHERE id=?""",(message,target["id"]))
   c.execute("DELETE FROM results WHERE match_id=? AND outcome='pending'",(target["id"],))
   return
  c.execute("""UPDATE matches SET match_result='',htft='',over25='',btts='',first_half_btts='',second_half_btts='',
    prediction_message='',prediction_confidence=?,market_probabilities=?,analysis_notes=? WHERE id=?""",
    (json.dumps(data["confidence"],ensure_ascii=False),json.dumps(data.get("market_probabilities",{}),ensure_ascii=False),data["notes"],target["id"]))
  c.execute("DELETE FROM results WHERE match_id=? AND outcome='pending'",(target["id"],))
  for key,value in data["predictions"].items():
   if key not in dict(MARKETS): continue
   c.execute("UPDATE matches SET "+key+"=? WHERE id=?",(value,target["id"]))
   c.execute("INSERT INTO results(match_id,market,prediction) VALUES(?,?,?) ON CONFLICT(match_id,market) DO UPDATE SET prediction=excluded.prediction,actual='',outcome='pending'",
    (target["id"],dict(MARKETS)[key],value))
 except Exception as exc:
  c.execute("UPDATE matches SET prediction_message=? WHERE id=?",(f"Tahmin üretilemedi: {exc}",target["id"]))

def history_job_payload(job):
 teams=json.loads(job["team_names"] or "[]") if "team_names" in job.keys() else []
 total=max(1,job["total_days"]+len(teams)+job["predictions_total"])
 done=job["processed_days"]+job["team_index"]+job["predictions_processed"]
 percent=100 if job["status"]=="completed" else min(99,round(done*100/total))
 return {"job_id":job["id"],"status":job["status"],"phase":job["phase"],
  "processed_days":job["processed_days"],"total_days":job["total_days"],
  "team_index":job["team_index"] if "team_index" in job.keys() else 0,"teams_total":len(teams),
  "predictions_processed":job["predictions_processed"],"predictions_total":job["predictions_total"],
  "fixtures_count":job["fixtures_count"],"fetched_count":job["fetched_count"],"skipped_count":job["skipped_count"],"percent":percent,"last_error":job["last_error"]}


init_db()
app.register_blueprint(auth_bp)
init_auth_db()

@app.route("/")
def index():
 day=request.args.get("date") or date.today().isoformat()
 view=request.args.get("view","upcoming")
 if view not in ("upcoming","live","completed"): view="upcoming"
 status_filter={"completed":"finished","live":"live","upcoming":"scheduled"}[view]
 with db() as c:
  uid=current_user_id() or 0
  ms=c.execute("""SELECT m.*,s.id saved FROM matches m LEFT JOIN user_saved_matches s ON s.match_id=m.id AND s.user_id=?
   WHERE substr(m.kickoff,1,10)=? AND m.status=? ORDER BY m.kickoff""",(uid,day,status_filter)).fetchall()
  saved=c.execute("SELECT COUNT(*) FROM user_saved_matches WHERE user_id=?",(uid,)).fetchone()[0]
  done=c.execute("SELECT COUNT(*) FROM matches WHERE status='finished'").fetchone()[0]
  upcoming=c.execute("SELECT COUNT(*) FROM matches WHERE substr(kickoff,1,10)=? AND status='scheduled'",(day,)).fetchone()[0]
  live_count=c.execute("SELECT COUNT(*) FROM matches WHERE substr(kickoff,1,10)=? AND status='live'",(day,)).fetchone()[0]
  last=c.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
 return render_template("index.html",matches=ms,saved_count=saved,done_count=done,day=day,
  provider_ready=provider_ready(),last_sync=last,view=view,upcoming_count=upcoming,live_count=live_count,\n  is_admin=bool(current_user_id() and session.get("user_email", "").casefold() in {x.strip().casefold() for x in os.environ.get("ADMIN_EMAILS", "").split(",") if x.strip()}))

@admin_required
@app.post("/sync/fixtures")
def sync_fixtures():
 day=request.form.get("date") or date.today().isoformat()
 try:
  provider=FootballProvider(os.environ.get("SPORTSCORE_API_KEY"))
  # Fetch only the selected day in this request. The previous 61-day loop
  # made sequential external calls (25s timeout each) inside one sync worker.
  fixtures=provider.fixtures(day)
  with db() as c:
   for f in fixtures:
    c.execute("""INSERT INTO matches(home,away,league,kickoff,status,created_at,provider,external_id,
      home_goals,away_goals,ht_home,ht_away,status_short,elapsed_minute)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(provider,external_id) WHERE external_id <> '' DO UPDATE SET
      home=excluded.home,away=excluded.away,league=excluded.league,kickoff=excluded.kickoff,
      status=excluded.status,home_goals=excluded.home_goals,away_goals=excluded.away_goals,
      ht_home=excluded.ht_home,ht_away=excluded.ht_away,status_short=excluded.status_short,elapsed_minute=excluded.elapsed_minute""",
      (f["home"],f["away"],f["league"],f["kickoff"],f["status"],datetime.utcnow().isoformat(),
       "sportscore",str(f["id"]),f["home_goals"],f["away_goals"],f["ht_home"],f["ht_away"],f["status_short"],f.get("elapsed_minute")))
   # Read finished-match history once and reuse it for all predictions.
   history=[dict(x) for x in c.execute(
    "SELECT id,home,away,league,status,home_goals,away_goals,ht_home,ht_away FROM matches WHERE status='finished' AND home_goals IS NOT NULL AND away_goals IS NOT NULL ORDER BY kickoff"
   ).fetchall()]
   history_index=prepare_history(history)
   targets=c.execute("SELECT * FROM matches WHERE substr(kickoff,1,10)=? AND status='scheduled'",(day,)).fetchall()
   for target in targets:
    try:
     data,message=predict(dict(target),history_index)
     if not data:
      c.execute("UPDATE matches SET prediction_message=?,prediction_confidence='{}',analysis_notes='' WHERE id=?",(message,target["id"]))
      continue
     for key,value in data["predictions"].items():
      if key not in dict(MARKETS): continue
      c.execute("UPDATE matches SET "+key+"=? WHERE id=?",(value,target["id"]))
      label=dict(MARKETS)[key]
      c.execute("INSERT INTO results(match_id,market,prediction) VALUES(?,?,?) ON CONFLICT(match_id,market) DO UPDATE SET prediction=excluded.prediction",(target["id"],label,value))
     c.execute("UPDATE matches SET prediction_confidence=?,market_probabilities=?,prediction_message='',analysis_notes=? WHERE id=?",(json.dumps(data["confidence"],ensure_ascii=False),json.dumps(data.get("market_probabilities",{}),ensure_ascii=False),data["notes"],target["id"]))
    except Exception as prediction_error:
     # A single bad fixture must not abort the whole daily import.
     c.execute("UPDATE matches SET prediction_message=? WHERE id=?",(f"Tahmin üretilemedi: {prediction_error}",target["id"]))
   c.execute("INSERT INTO sync_log(provider,day,ran_at,fixtures_count,status,message) VALUES(?,?,?,?,?,?)",
    ("sportscore",day,datetime.utcnow().isoformat(),len(fixtures),"success",f"SportScore: {len(fixtures)} maç kaydı"))
  flash(f"{len(fixtures)} maç otomatik olarak içe aktarıldı/güncellendi.", "success")
 except ProviderError as e:
  with db() as c:
   c.execute("INSERT INTO sync_log(provider,day,ran_at,status,message) VALUES(?,?,?,?,?)",
    ("sportscore",day,datetime.utcnow().isoformat(),"error",str(e)))
  flash(f"Veri sağlayıcı hatası: {e}", "error")
 except Exception as e:
  with db() as c:
   c.execute("INSERT INTO sync_log(provider,day,ran_at,status,message) VALUES(?,?,?,?,?)",
    ("sportscore",day,datetime.utcnow().isoformat(),"error",str(e)))
  flash(f"Beklenmeyen senkronizasyon hatası: {e}", "error")
 return redirect(url_for("index",date=day))

@app.get("/sync/live/refresh")
def refresh_live_fixtures():
 day=request.args.get("date") or date.today().isoformat()
 try:
  provider=FootballProvider(os.environ.get("SPORTSCORE_API_KEY"))
  # The fixtures endpoint supplies scores/status but often omits the running
  # clock. SportScore's dedicated widget feed includes status_text such as 67'.
  live_fixtures=provider.live_matches(limit=50)
  def team_key(value):
   normalized=provider._normalize_search_name(value)
   return " ".join(sorted(normalized.split()))
  with db() as c:
   rows=c.execute("""SELECT id,home,away,status_short,elapsed_minute,home_goals,away_goals
    FROM matches WHERE substr(kickoff,1,10)=? AND status='live' ORDER BY kickoff""",(day,)).fetchall()
   by_pair={(team_key(row["home"]),team_key(row["away"])):row for row in rows}
   for fixture in live_fixtures:
    pair=(team_key(fixture["home"]),team_key(fixture["away"]))
    row=by_pair.get(pair)
    if not row:
     continue
    c.execute("""UPDATE matches SET status='live',status_short=?,
      elapsed_minute=?,home_goals=COALESCE(?,home_goals),
      away_goals=COALESCE(?,away_goals) WHERE id=?""",
      (fixture.get("status_short") or "",fixture.get("elapsed_minute"),
       fixture.get("home_goals"),fixture.get("away_goals"),row["id"]))
   rows=c.execute("""SELECT id,status_short,elapsed_minute,home_goals,away_goals
    FROM matches WHERE substr(kickoff,1,10)=? AND status='live' ORDER BY id""",(day,)).fetchall()
  result=[]
  for row in rows:
   code=str(row["status_short"] or "").upper()
   minute=row["elapsed_minute"]
   if code in ("HT","INT","BREAK","BT"):
    label="Devre Arası"
   elif minute is not None and str(minute).strip():
    label=f"Canlı {minute}′"
   elif code in ("1H","1ST_HALF"):
    label="Canlı · 1. Yarı"
   elif code in ("2H","2ND_HALF"):
    label="Canlı · 2. Yarı"
   else:
    label="Canlı"
   result.append({"id":row["id"],"minute_label":label,
    "score":f"{row['home_goals']} – {row['away_goals']}" if row["home_goals"] is not None and row["away_goals"] is not None else "—"})
  return jsonify({"matches":result,"updated_at":datetime.utcnow().isoformat()})
 except ProviderError as exc:
  return jsonify({"error":str(exc)}),502
 except Exception as exc:
  app.logger.exception("Live fixture refresh failed")
  return jsonify({"error":str(exc)}),500

@admin_required
@app.post("/sync/history/start")
def start_history_sync():
 today=date.today()
 start=today-timedelta(days=180)
 end=today-timedelta(days=1)
 now=datetime.utcnow().isoformat()
 with db() as c:
  active=c.execute("SELECT * FROM sync_jobs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
  if active:
   return jsonify(history_job_payload(active))
  cur=c.execute("""INSERT INTO sync_jobs(phase,status,start_date,end_date,cursor_date,total_days,team_names,team_index,created_at,updated_at)
   VALUES('history','running',?,?,?,?,?,?,?,?) RETURNING id""",(start.isoformat(),end.isoformat(),start.isoformat(),(end-start).days+1,"[]",0,now,now))
  job=c.execute("SELECT * FROM sync_jobs WHERE id=?",(cur.fetchone()["id"],)).fetchone()
 return jsonify(history_job_payload(job))

@login_required
@app.get("/sync/history/status")
def history_sync_status():
 job_id=request.args.get("job_id",type=int)
 with db() as c:
  job=c.execute("SELECT * FROM sync_jobs WHERE id=?",(job_id,)).fetchone() if job_id else c.execute("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT 1").fetchone()
 if not job:return jsonify({"status":"idle","percent":0})
 return jsonify(history_job_payload(job))

@admin_required
@app.post("/sync/history/step")
def history_sync_step():
 payload=request.get_json(silent=True) or {}
 job_id=payload.get("job_id")
 # Read only the job state, then close the database connection BEFORE any
 # potentially slow SportScore network request.
 with db() as c:
  job=c.execute("SELECT * FROM sync_jobs WHERE id=?",(job_id,)).fetchone()
  if not job:return jsonify({"status":"error","last_error":"Senkronizasyon işi bulunamadı."}),404
  if job["status"]!="running":return jsonify(history_job_payload(job))
  phase=job["phase"]
  day=job["cursor_date"] if phase=="history" else None
  history_days=[]
  if phase=="history":
   cursor=date.fromisoformat(day)
   end=date.fromisoformat(job["end_date"])
   while cursor<=end and len(history_days)<4:
    history_days.append(cursor.isoformat())
    cursor+=timedelta(days=1)
  team_names=json.loads(job["team_names"] or "[]") if phase=="team_history" else []
  team_index=job["team_index"] if phase=="team_history" else 0
  team_name=team_names[team_index] if phase=="team_history" and team_index<len(team_names) else None

 # External calls happen with NO open PostgreSQL transaction/connection.
 fixtures=None
 fetch_error=None
 if phase=="history":
  try:
   # Fetch up to four adjacent days concurrently. This cuts the number of
   # browser round-trips while keeping provider load modest and bounded.
   history_fixtures={}
   with ThreadPoolExecutor(max_workers=4) as pool:
    futures={pool.submit(FootballProvider(os.environ.get("SPORTSCORE_API_KEY")).fixtures, target_day, status="finished"):target_day for target_day in history_days}
    for future in as_completed(futures):
     target_day=futures[future]
     history_fixtures[target_day]=future.result()
  except Exception as exc:
   fetch_error=exc
 elif phase=="team_history" and team_name:
  try:
   fixtures=FootballProvider(os.environ.get("SPORTSCORE_API_KEY")).team_fixtures_for_team(team_name,limit=30)
  except Exception as exc:
   fetch_error=exc

 with db() as c:
  job=c.execute("SELECT * FROM sync_jobs WHERE id=?",(job_id,)).fetchone()
  if not job:return jsonify({"status":"error","last_error":"Senkronizasyon işi bulunamadı."}),404
  if job["status"]!="running":return jsonify(history_job_payload(job))
  # Do not apply a response if another step has already advanced this job.
  if job["phase"]!=phase or (phase=="history" and job["cursor_date"]!=day) or (phase=="team_history" and job["team_index"]!=team_index):
   return jsonify(history_job_payload(job))

  if phase=="history":
   if fetch_error is not None:
    attempts=job["attempts"]+1
    state="error" if attempts>=3 else "running"
    c.execute("UPDATE sync_jobs SET attempts=?,last_error=?,status=?,updated_at=? WHERE id=?",
     (attempts,str(fetch_error),state,datetime.utcnow().isoformat(),job_id))
   else:
    totals={"fetched":0,"stored":0,"skipped":0}
    for target_day in history_days:
     day_stats=store_fixtures(c,history_fixtures.get(target_day,[]))
     for key in totals: totals[key]+=day_stats[key]
    stats=totals
    next_day=(date.fromisoformat(history_days[-1])+timedelta(days=1)).isoformat()
    processed=job["processed_days"]+len(history_days)
    names=json.loads(job["team_names"] or "[]")
    if next_day>job["end_date"]:
     rows=c.execute("SELECT home,away FROM matches WHERE status='scheduled' AND datetime(kickoff)>=datetime('now') ORDER BY kickoff").fetchall()
     seen={" ".join(str(n or "").casefold().split()) for n in names}
     for row in rows:
      for name in (row["home"],row["away"]):
       key=" ".join(str(name or "").casefold().split())
       if key and key not in seen:
        seen.add(key); names.append(str(name))
     next_phase="team_history" if names else "predictions"
    else:
     next_phase="history"
    c.execute("""UPDATE sync_jobs SET phase=?,status='running',cursor_date=?,processed_days=?,team_names=?,team_index=0,
      fixtures_count=fixtures_count+?,fetched_count=fetched_count+?,skipped_count=skipped_count+?,
      attempts=0,last_error='',updated_at=? WHERE id=?""",
     (next_phase,next_day,processed,json.dumps(names,ensure_ascii=False),stats["stored"],stats["fetched"],stats["skipped"],datetime.utcnow().isoformat(),job_id))

  elif phase=="team_history":
   if not team_name:
    c.execute("UPDATE sync_jobs SET phase='predictions',updated_at=? WHERE id=?",(datetime.utcnow().isoformat(),job_id))
   else:
    stats=store_fixtures(c,fixtures or []) if fetch_error is None else {"fetched":0,"stored":0,"skipped":0}
    next_index=team_index+1
    is_last=next_index>=len(team_names)
    pred_total=c.execute("SELECT COUNT(*) FROM matches WHERE status='scheduled' AND datetime(kickoff)>=datetime('now')").fetchone()[0] if is_last else job["predictions_total"]
    next_phase=("predictions" if pred_total else "completed") if is_last else "team_history"
    skipped=stats["skipped"]+(1 if fetch_error is not None or not fixtures else 0)
    note=f"{team_name}: {fetch_error}" if fetch_error is not None else ""
    c.execute("""UPDATE sync_jobs SET phase=?,status=?,team_index=?,fixtures_count=fixtures_count+?,
      fetched_count=fetched_count+?,skipped_count=skipped_count+?,predictions_total=?,
      last_error=?,updated_at=? WHERE id=?""",
     (next_phase,"running" if next_phase!="completed" else "completed",next_index,stats["stored"],stats["fetched"],skipped,
      pred_total,note,datetime.utcnow().isoformat(),job_id))
    if next_phase=="completed":
     c.execute("INSERT INTO sync_log(provider,day,ran_at,fixtures_count,status,message) VALUES(?,?,?,?,?,?)",
      ("sportscore",date.today().isoformat(),datetime.utcnow().isoformat(),job["fixtures_count"]+stats["stored"],"success",
       f"Geçmiş veri tamamlandı; {job['fetched_count']+stats['fetched']} kayıt alındı, takım geçmişi dahil {job['fixtures_count']+stats['stored']} tamamlanmış maç kaydedildi."))

  elif phase=="predictions":
   history=[dict(x) for x in c.execute("""SELECT id,home,away,league,status,home_goals,away_goals,ht_home,ht_away
    FROM matches WHERE status='finished' AND home_goals IS NOT NULL AND away_goals IS NOT NULL ORDER BY kickoff""").fetchall()]
   history_index=prepare_history(history)
   batch=c.execute("""SELECT * FROM matches WHERE status='scheduled' AND datetime(kickoff)>=datetime('now')
    ORDER BY kickoff,id LIMIT 8 OFFSET ?""",(job["predictions_processed"],)).fetchall()
   for target in batch:rebuild_one_prediction(c,target,history_index)
   processed=job["predictions_processed"]+len(batch)
   done=processed>=job["predictions_total"] or not batch
   c.execute("UPDATE sync_jobs SET predictions_processed=?,status=?,updated_at=? WHERE id=?",
    (processed,"completed" if done else "running",datetime.utcnow().isoformat(),job_id))
   if done:
    c.execute("INSERT INTO sync_log(provider,day,ran_at,fixtures_count,status,message) VALUES(?,?,?,?,?,?)",
     ("sportscore",date.today().isoformat(),datetime.utcnow().isoformat(),job["fixtures_count"],"success",
      f"Geçmiş veri tamamlandı; {job['fetched_count']} kayıt alındı, {job['fixtures_count']} tamamlanmış maç kaydedildi, {job['skipped_count']} kayıt atlandı; {processed} yaklaşan maç yeniden tahmin edildi."))
  job=c.execute("SELECT * FROM sync_jobs WHERE id=?",(job_id,)).fetchone()
 return jsonify(history_job_payload(job))

@admin_required
@app.route("/match/new",methods=["GET","POST"])
def new_match():
 if request.method=="POST":
  keys=["home","away","league","kickoff","odds_home","odds_draw","odds_away","htft","over25","btts","first_half_btts","second_half_btts","corners","notes"]
  d={k:request.form.get(k,"").strip() for k in keys}
  if not d["home"] or not d["away"]: flash("İki takım adı zorunludur.","error"); return render_template("form.html",data=d)
  with db() as c:
   cur=c.execute("INSERT INTO matches(home,away,league,kickoff,status,odds_home,odds_draw,odds_away,htft,over25,btts,first_half_btts,second_half_btts,corners,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",(d["home"],d["away"],d["league"],d["kickoff"],"scheduled",d["odds_home"],d["odds_draw"],d["odds_away"],d["htft"],d["over25"],d["btts"],d["first_half_btts"],d["second_half_btts"],d["corners"],d["notes"],datetime.utcnow().isoformat()))
   sync_prediction_rows(c,cur.fetchone()["id"])
  flash("Maç kaydedildi.","success"); return redirect("/")
 return render_template("form.html",data={})

@app.post("/match/<int:mid>/save")
@login_required
def save(mid):
 uid=current_user_id()
 with db() as c:
  if c.execute("SELECT 1 FROM matches WHERE id=?",(mid,)).fetchone():
   c.execute("INSERT INTO user_saved_matches(user_id,match_id,saved_at) VALUES(?,?,?) ON CONFLICT(user_id,match_id) DO NOTHING",(uid,mid,datetime.utcnow().isoformat()))
 return redirect(request.referrer or "/")
@app.post("/match/<int:mid>/unsave")
@login_required
def unsave(mid):
 with db() as c:c.execute("DELETE FROM user_saved_matches WHERE user_id=? AND match_id=?",(current_user_id(),mid))
 return redirect(request.referrer or "/")
@app.route("/match/<int:mid>")
def detail(mid):
 with db() as c:
  m=c.execute("SELECT m.*,s.id saved FROM matches m LEFT JOIN user_saved_matches s ON s.match_id=m.id AND s.user_id=? WHERE m.id=?",(current_user_id() or 0,mid)).fetchone()
  # Lazily rebuild probabilities for fixtures imported before the detailed-market field existed.
  if m and m["status"] == "scheduled" and not json.loads(m["market_probabilities"] or "{}"):
   generate_prediction(c, mid)
   m=c.execute("SELECT m.*,s.id saved FROM matches m LEFT JOIN saved_matches s ON s.match_id=m.id WHERE m.id=?",(mid,)).fetchone()
  rs=c.execute("SELECT * FROM results WHERE match_id=? ORDER BY id",(mid,)).fetchall()
  confidence=json.loads(m["prediction_confidence"] or "{}")
  market_probabilities=json.loads(m["market_probabilities"] or "{}")
 if not m:return "Maç bulunamadı",404
 return render_template("detail.html",m=m,results=rs,markets=MARKETS,confidence=confidence,market_probabilities=market_probabilities)
@admin_required
@app.post("/match/<int:mid>/status")
def status(mid):
 st=request.form.get("status","scheduled")
 if st not in ("scheduled","live","finished"):st="scheduled"
 with db() as c:c.execute("UPDATE matches SET status=? WHERE id=?",(st,mid))
 return redirect(url_for("detail",mid=mid))
@admin_required
@app.post("/match/<int:mid>/result")
def result(mid):
 with db() as c:
  for key,label in MARKETS:
   actual=request.form.get("actual_"+key,"").strip(); row=c.execute("SELECT prediction FROM results WHERE match_id=? AND market=?",(mid,label)).fetchone()
   if row and actual:
    outcome="won" if row["prediction"].casefold()==actual.casefold() else "lost"
    c.execute("UPDATE results SET actual=?,outcome=? WHERE match_id=? AND market=?",(actual,outcome,mid,label))
  c.execute("UPDATE matches SET status='finished' WHERE id=?",(mid,))
 flash("Tahminler sonuçlarla karşılaştırıldı.","success"); return redirect(url_for("detail",mid=mid))
@app.route("/saved")
@login_required
def saved():
 with db() as c:ms=c.execute("SELECT m.* FROM matches m JOIN user_saved_matches s ON s.match_id=m.id WHERE s.user_id=? ORDER BY s.saved_at DESC",(current_user_id(),)).fetchall()
 return render_template("saved.html",matches=ms)
@app.route("/results")
def results():
 with db() as c:
  rows=c.execute("SELECT m.home,m.away,m.league,m.kickoff,r.market,r.prediction,r.actual,r.outcome FROM results r JOIN matches m ON m.id=r.match_id WHERE m.status='finished' ORDER BY m.kickoff DESC").fetchall()
  stats=c.execute("SELECT outcome,COUNT(*) n FROM results WHERE outcome IN ('won','lost') GROUP BY outcome").fetchall()
 counts={r["outcome"]:r["n"] for r in stats}; won=counts.get("won",0); lost=counts.get("lost",0)
 return render_template("results.html",rows=rows,won=won,lost=lost,pct=round(won*100/(won+lost)) if won+lost else 0)
if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)),debug=True)
