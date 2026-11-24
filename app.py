import os, sqlite3, json, time, re, secrets, hmac
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort
from football_provider import FootballProvider, ProviderError
from prediction_engine import predict, prepare_history
from auth import auth_bp, current_user_id, init_auth_db, login_required, admin_required, is_admin_user
from result_sync import actual_market_values, evaluate_prediction, prefer_saved_match, matching_fixture_rows

app=Flask(__name__)
secret_key=os.environ.get("SECRET_KEY","").strip()
if (os.environ.get("RENDER") or os.environ.get("FLASK_ENV")=="production") and len(secret_key)<32:
 raise RuntimeError("Production SECRET_KEY must be configured with at least 32 characters.")
app.secret_key=secret_key or secrets.token_urlsafe(48)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER") or os.environ.get("FLASK_ENV")=="production"))

def _is_placeholder_team_name(value):
 import re
 text=str(value or "").strip()
 if not text:
  return False
 if text.casefold() in {"tea","tbd","tba","unknown","unknown team","team unknown","team not found","to be announced","n/a"}:
  return True
 return bool(re.fullmatch(r"(?i)team[\s:_-]+[a-z0-9][a-z0-9_-]{7,}",text))


def csrf_token():
 token=session.get("_csrf_token")
 if not token:
  token=secrets.token_urlsafe(32)
  session["_csrf_token"]=token
 return token

@app.context_processor
def inject_template_security():
 return {"csrf_token":csrf_token,"is_admin":is_admin_user()}

@app.template_filter("team_display")
def team_display(value):
 if _is_placeholder_team_name(value):
  return "Takım adı doğrulanamadı"
 return str(value or "Takım adı belirtilmedi")

@app.before_request
def enforce_csrf():
 if request.method not in ("POST","PUT","PATCH","DELETE"):
  return
 if request.endpoint in ("refresh_results","refresh_missing_predictions"):
  expected_token=os.environ.get("RESULT_SYNC_TOKEN","").strip()
  supplied=request.headers.get("Authorization","")
  if expected_token and hmac.compare_digest(supplied, "Bearer "+expected_token):
   return
  abort(401, description="Sonuç senkronizasyon yetkisi geçersiz.")
 expected=session.get("_csrf_token")
 supplied=request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
 if not expected or not supplied or not hmac.compare_digest(expected,supplied):
  abort(400, description="CSRF doğrulaması başarısız.")

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
 sql=re.sub(r"datetime\(([^)]+)\)\s*(>=|<=|<|>)\s*datetime\('now'\)", r"CAST(\1 AS timestamptz) \2 NOW()", sql, flags=re.IGNORECASE)
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

class SQLiteConnectionContext:
 def __init__(self, connection):
  self.connection=connection
 def __enter__(self):
  return self.connection
 def __exit__(self, exc_type, exc_value, traceback):
  try:
   return self.connection.__exit__(exc_type, exc_value, traceback)
  finally:
   self.connection.close()


def db():
 database_url=os.environ.get("DATABASE_URL","").strip()
 if database_url:
  return PostgresConnection(database_url)
 c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON")
 return SQLiteConnectionContext(c)


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
   # PostgreSQL expression indexes for the date-filtered match list.
   c.execute("CREATE INDEX IF NOT EXISTS idx_matches_status_kickoff_day ON matches(status, (substring(kickoff, 1, 10)), kickoff)")
   c.execute("CREATE INDEX IF NOT EXISTS idx_matches_dedupe_key ON matches(lower(trim(home)), lower(trim(away)), lower(trim(league)), kickoff)")
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
  # SQLite equivalents of the PostgreSQL match-list indexes.
  c.execute("CREATE INDEX IF NOT EXISTS idx_matches_status_kickoff_day ON matches(status, substr(kickoff, 1, 10), kickoff)")
  c.execute("CREATE INDEX IF NOT EXISTS idx_matches_dedupe_key ON matches(lower(trim(home)), lower(trim(away)), lower(trim(league)), kickoff)")
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

def upsert_fixture_record(c, fixture, provider="sportscore"):
 # Never let provider-generated "Team <opaque-id>" labels overwrite a real club name.
 external_id=str(fixture.get("id") or "").strip()
 provider_name=str(fixture.get("provider") or provider)
 home_placeholder=_is_placeholder_team_name(fixture.get("home"))
 away_placeholder=_is_placeholder_team_name(fixture.get("away"))

 def update_existing(existing):
  home=existing["home"] if home_placeholder else fixture["home"]
  away=existing["away"] if away_placeholder else fixture["away"]
  c.execute("""UPDATE matches SET home=?,away=?,league=?,status=?,home_goals=?,away_goals=?,ht_home=?,ht_away=?,
   status_short=?,elapsed_minute=? WHERE id=?""",
   (home,away,fixture.get("league",""),fixture["status"],fixture.get("home_goals"),fixture.get("away_goals"),
    fixture.get("ht_home"),fixture.get("ht_away"),fixture.get("status_short",""),
    fixture.get("elapsed_minute"),existing["id"]))
  return existing["id"]

 if external_id:
  existing=c.execute("SELECT id,home,away FROM matches WHERE provider=? AND external_id=? ORDER BY id LIMIT 1",
   (provider_name,external_id)).fetchone()
  if existing:
   return update_existing(existing)
 else:
  candidates=c.execute("""SELECT id,home,away FROM matches
   WHERE provider=? AND COALESCE(external_id,'')='' AND kickoff=?
    AND lower(trim(COALESCE(league,'')))=lower(trim(?))
   ORDER BY id""",(provider_name,fixture.get("kickoff",""),fixture.get("league",""))).fetchall()
  exact=[row for row in candidates
   if str(row["home"] or "").strip().casefold()==str(fixture["home"] or "").strip().casefold()
   and str(row["away"] or "").strip().casefold()==str(fixture["away"] or "").strip().casefold()]
  if exact:
   return update_existing(exact[0])
  if home_placeholder or away_placeholder:
   compatible=[row for row in candidates
    if (home_placeholder or str(row["home"] or "").strip().casefold()==str(fixture["home"] or "").strip().casefold())
    and (away_placeholder or str(row["away"] or "").strip().casefold()==str(fixture["away"] or "").strip().casefold())]
   if len(compatible)==1:
    return update_existing(compatible[0])

 cur=c.execute("""INSERT INTO matches(home,away,league,kickoff,status,created_at,provider,external_id,
  home_goals,away_goals,ht_home,ht_away,status_short,elapsed_minute)
  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  ON CONFLICT(provider,external_id) WHERE external_id <> '' DO UPDATE SET
  home=CASE WHEN ? THEN matches.home ELSE excluded.home END,
  away=CASE WHEN ? THEN matches.away ELSE excluded.away END,
  league=excluded.league,kickoff=excluded.kickoff,
  status=excluded.status,home_goals=excluded.home_goals,away_goals=excluded.away_goals,
  ht_home=excluded.ht_home,ht_away=excluded.ht_away,status_short=excluded.status_short,
  elapsed_minute=excluded.elapsed_minute RETURNING id""",
  (fixture["home"],fixture["away"],fixture.get("league",""),fixture.get("kickoff",""),
   fixture["status"],datetime.utcnow().isoformat(),provider_name,external_id,
   fixture.get("home_goals"),fixture.get("away_goals"),fixture.get("ht_home"),fixture.get("ht_away"),
   fixture.get("status_short",""),fixture.get("elapsed_minute"),home_placeholder,away_placeholder))
 row=cur.fetchone()
 return row["id"] if row else None

def store_fixtures(c, fixtures):
 stored=0
 for f in fixtures:
  # Historical sync must only persist completed matches with a real full-time score.
  if f["status"] != "finished" or f["home_goals"] is None or f["away_goals"] is None:
   continue
  upsert_fixture_record(c,f)
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
# The saved page orders each user's rows by saved_at; this composite index avoids a per-user sort.
with db() as _index_db:
 _index_db.execute("CREATE INDEX IF NOT EXISTS idx_user_saved_matches_user_saved_at ON user_saved_matches(user_id, saved_at DESC)")

_TEAM_LOGO_CACHE = {}

@app.get("/api/team-logo")
def team_logo():
 team_name=(request.args.get("name") or "").strip()[:100]
 if not team_name:
  return jsonify({"logo":None}),400
 if _is_placeholder_team_name(team_name):
  return jsonify({"logo":None})
 cache_key=team_name.casefold()
 cached=_TEAM_LOGO_CACHE.get(cache_key)
 if cached and cached[0] > time.time():
  return jsonify({"logo":cached[1]})
 try:
  logo=FootballProvider(os.environ.get("SPORTSCORE_API_KEY")).search_team_logo(team_name)
  _TEAM_LOGO_CACHE[cache_key]=(time.time()+86400,logo)
  return jsonify({"logo":logo})
 except ProviderError:
  return jsonify({"logo":None}),503


def _daily_top_ten(rows):
 """Rank the day's strongest model picks, spreading them across at least five fixtures."""
 groups = {}
 def read(record, key):
  try:
   return record[key]
  except (KeyError, IndexError, TypeError):
   return None
 def pct(value):
  try:
   number=float(value)
  except (TypeError,ValueError):
   return None
  if 1 < number <= 100:
   pass
  elif 0 <= number <= 1:
   number *= 100
  else:
   return None
  return max(0,min(100,round(number)))
 def add(group, market, prediction, probability):
  score=pct(probability)
  if score is None or not str(prediction or "").strip():
   return
  group["picks"].append({
   "id":group["id"],"home":group["home"],"away":group["away"],
   "league":group["league"],"kickoff":group["kickoff"],
   "market":market,"prediction":str(prediction).strip(),"confidence":score
  })
 for row in rows:
  match_id=read(row,"id")
  try:
   probs=json.loads(read(row,"market_probabilities") or "{}")
  except (TypeError,ValueError):
   probs={}
  try:
   confidence=json.loads(read(row,"prediction_confidence") or "{}")
  except (TypeError,ValueError):
   confidence={}
  group={"id":match_id,"home":read(row,"home") or "Ev sahibi",
   "away":read(row,"away") or "Deplasman","league":read(row,"league") or "",
   "kickoff":read(row,"kickoff") or "","picks":[]}
  result=probs.get("match_result")
  if isinstance(result,dict) and result:
   key=max(result,key=lambda item:pct(result[item]) if pct(result[item]) is not None else -1)
   add(group,"Maç Sonucu",{"1":"MS 1","X":"MS X","2":"MS 2"}.get(str(key),str(key)),result[key])
  first_result=probs.get("first_half_result")
  if isinstance(first_result,dict) and first_result:
   key=max(first_result,key=lambda item:pct(first_result[item]) if pct(first_result[item]) is not None else -1)
   add(group,"İlk Yarı Sonucu",{"1":"İY 1","X":"İY X","2":"İY 2"}.get(str(key),str(key)),first_result[key])
  goals=probs.get("goals")
  if isinstance(goals,dict):
   for line in ("1.5","2.5","3.5","4.5"):
    values=goals.get(line) or goals.get(line.replace(".",","))
    if not isinstance(values,dict):
     continue
    over=pct(values.get("over")); under=pct(values.get("under"))
    if over is None or under is None:
     continue
    add(group,line.replace(".",",")+" Gol","Üst" if over>=under else "Alt",max(over,under))
  for key,label in (("btts","KG"),("first_half_btts","İlk Yarı KG"),("second_half_btts","İkinci Yarı KG")):
   values=probs.get(key)
   if isinstance(values,dict) and values:
    selection=max(values,key=lambda item:pct(values[item]) if pct(values[item]) is not None else -1)
    add(group,label,selection,values[selection])
  htft=probs.get("htft")
  if isinstance(htft,dict) and htft:
   selection=max(htft,key=lambda item:pct(htft[item]) if pct(htft[item]) is not None else -1)
   add(group,"İY/MS",selection,htft[selection])
  # Older rows may have confidence/prediction columns but no full probability map.
  fallback=(("match_result","Maç Sonucu"),("over25","2,5 Gol"),("btts","KG"),
   ("first_half_btts","İlk Yarı KG"),("second_half_btts","İkinci Yarı KG"),("htft","İY/MS"))
  present={pick["market"] for pick in group["picks"]}
  for key,label in fallback:
   prediction=read(row,key)
   if label in present or not prediction:
    continue
   add(group,label,prediction,confidence.get(key))
  if group["picks"]:
   # Match the same real fixture even if the provider inserted duplicate DB rows.
   fixture_key=tuple(str(group[key] or "").strip().casefold() for key in ("home","away","league","kickoff"))
   existing=groups.get(fixture_key)
   if existing:
    for pick in group["picks"]:
     pick["id"]=existing["id"]
    merged={pick["market"]:pick for pick in existing["picks"]}
    for pick in group["picks"]:
     previous=merged.get(pick["market"])
     if previous is None or pick["confidence"]>previous["confidence"]:
      merged[pick["market"]]=pick
    existing["picks"]=list(merged.values())
   else:
    unique={pick["market"]:pick for pick in group["picks"]}
    group["picks"]=list(unique.values())
    groups[fixture_key]=group
 for group in groups.values():
  group["picks"].sort(key=lambda item:(-item["confidence"],item["market"]))
 # If five distinct eligible fixtures do not exist, do not publish a misleading 10-pick slate.
 if len(groups)<5:
  return [],len(groups)
 ordered=sorted(groups.values(),key=lambda group:(-group["picks"][0]["confidence"],group["kickoff"],str(group["id"])))
 selected=[]
 # First pass: one best pick from each fixture, ensuring maximum match diversity.
 for group in ordered:
  if len(selected)>=10:
   break
  selected.append(group["picks"][0])
 # Second pass: add only one additional pick per fixture, strongest options first.
 second=[group["picks"][1] for group in ordered if len(group["picks"])>1]
 second.sort(key=lambda item:(-item["confidence"],item["kickoff"],str(item["id"])))
 selected.extend(second[:max(0,10-len(selected))])
 return selected[:10],len(groups)


@app.route("/")
def index():
 # Render's host clock may be UTC; the bulletin date must follow Turkish local time.
 now_local=datetime.now(ZoneInfo("Europe/Istanbul"))
 day=request.args.get("date") or now_local.date().isoformat()
 view=request.args.get("view","upcoming")
 if view not in ("upcoming","unverified","live","completed"): view="upcoming"
 status_filter={"completed":"finished","live":"live","upcoming":"scheduled","unverified":"scheduled"}[view]
 time_filter="datetime(m.kickoff) >= datetime('now')" if view=="upcoming" else (
  "datetime(m.kickoff) < datetime('now')" if view=="unverified" else "1=1")
 # Duplicate provider rows are hidden at read time, not deleted. Keep the
 # oldest row as the canonical display row so saved links and history survive.
 with db() as c:
  uid=current_user_id() or 0
  ms=c.execute("""WITH ranked AS (
    SELECT m.*,s.id saved,
     ROW_NUMBER() OVER (
      PARTITION BY lower(trim(m.home)),lower(trim(m.away)),lower(trim(m.league)),m.kickoff
      ORDER BY CASE WHEN s.id IS NOT NULL THEN 0 ELSE 1 END,m.id
     ) AS duplicate_rank
    FROM matches m
    LEFT JOIN user_saved_matches s ON s.match_id=m.id AND s.user_id=?
    WHERE substr(m.kickoff,1,10)=? AND m.status=? AND """+time_filter+"""
   )
   SELECT * FROM ranked WHERE duplicate_rank=1 ORDER BY kickoff""",(uid,day,status_filter)).fetchall()
  saved=c.execute("SELECT COUNT(*) FROM user_saved_matches WHERE user_id=?",(uid,)).fetchone()[0]
  done=c.execute("SELECT COUNT(*) FROM matches WHERE status='finished'").fetchone()[0]
  today_total=c.execute("""SELECT COUNT(*) FROM (SELECT ROW_NUMBER() OVER (PARTITION BY lower(trim(home)),lower(trim(away)),lower(trim(league)),kickoff ORDER BY id) rank_no FROM matches WHERE substr(kickoff,1,10)=?) ranked WHERE rank_no=1""",(day,)).fetchone()[0]
  settled=c.execute("SELECT COUNT(*) total,SUM(CASE WHEN outcome='won' THEN 1 ELSE 0 END) won FROM results WHERE outcome IN ('won','lost')").fetchone()
  success_rate=round((settled['won'] or 0)*100/settled['total']) if settled['total'] else 0
  live_featured=c.execute("""SELECT id,home,away,league,kickoff,home_goals,away_goals,
   ht_home,ht_away,elapsed_minute,status_short FROM matches
   WHERE status='live' ORDER BY kickoff DESC,id DESC LIMIT 1""").fetchone()
  upcoming=c.execute("""SELECT COUNT(*) FROM (
   SELECT ROW_NUMBER() OVER (
    PARTITION BY lower(trim(home)),lower(trim(away)),lower(trim(league)),kickoff ORDER BY id
   ) AS duplicate_rank
   FROM matches WHERE substr(kickoff,1,10)=? AND status='scheduled'
    AND datetime(kickoff) >= datetime('now')
  ) ranked WHERE duplicate_rank=1""",(day,)).fetchone()[0]
  unverified=c.execute("""SELECT COUNT(*) FROM (
   SELECT ROW_NUMBER() OVER (
    PARTITION BY lower(trim(home)),lower(trim(away)),lower(trim(league)),kickoff ORDER BY id
   ) AS duplicate_rank
   FROM matches WHERE substr(kickoff,1,10)=? AND status='scheduled'
    AND datetime(kickoff) < datetime('now')
  ) ranked WHERE duplicate_rank=1""",(day,)).fetchone()[0]
  live_count=c.execute("""SELECT COUNT(*) FROM (
   SELECT ROW_NUMBER() OVER (
    PARTITION BY lower(trim(home)),lower(trim(away)),lower(trim(league)),kickoff ORDER BY id
   ) AS duplicate_rank
   FROM matches WHERE substr(kickoff,1,10)=? AND status='live'
  ) ranked WHERE duplicate_rank=1""",(day,)).fetchone()[0]
  daily_candidate_rows=c.execute("""SELECT id,home,away,league,kickoff,status,
   match_result,over25,btts,first_half_btts,second_half_btts,htft,
   prediction_confidence,market_probabilities
   FROM matches WHERE substr(kickoff,1,10)=? AND status='scheduled'
    AND COALESCE(prediction_message,'')=''
    AND COALESCE(market_probabilities,'{}') NOT IN ('','{}')
    AND datetime(kickoff)>=datetime('now')
   ORDER BY kickoff,id""",(day,)).fetchall()
  daily_picks,daily_match_count=_daily_top_ten(daily_candidate_rows)
  last=c.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
 return render_template("index.html",matches=ms,saved_count=saved,done_count=done,day=day,
  provider_ready=provider_ready(),last_sync=last,view=view,upcoming_count=upcoming,
  unverified_count=unverified,live_count=live_count,today_total=today_total,
  success_rate=success_rate,
  daily_picks=daily_picks,daily_pick_count=len(daily_picks),daily_match_count=daily_match_count,
  live_featured=live_featured)

@app.post("/sync/fixtures")
@admin_required
def sync_fixtures():
 day=request.form.get("date") or date.today().isoformat()
 try:
  provider=FootballProvider(os.environ.get("SPORTSCORE_API_KEY"))
  # Fetch only the selected day in this request. The previous 61-day loop
  # made sequential external calls (25s timeout each) inside one sync worker.
  fixtures=provider.fixtures(day)
  with db() as c:
   for f in fixtures:
    upsert_fixture_record(c,f)
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

@app.post("/sync/live/refresh")
def refresh_live_fixtures():
 payload=request.get_json(silent=True) or {}
 day=(request.form.get("date") or payload.get("date") or datetime.now(ZoneInfo("Europe/Istanbul")).date().isoformat())
 try:
  provider=FootballProvider(os.environ.get("SPORTSCORE_API_KEY"))
  # Dedicated SportScore widget feed contains the live clock (e.g. 67' / 45+2).
  live_fixtures=provider.live_matches(limit=50)
  def team_key(value):
   normalized=provider._normalize_search_name(value)
   return " ".join(sorted(normalized.split()))
  with db() as c:
   # Include scheduled rows too: a match must be promoted to live when the
   # provider confirms kickoff, not left in "time passed / verify" forever.
   rows=[dict(row) for row in c.execute("""SELECT id,home,away,league,kickoff,status_short,elapsed_minute,home_goals,away_goals
    FROM matches WHERE substr(kickoff,1,10)=? AND status IN ('scheduled','live')
    ORDER BY kickoff""",(day,)).fetchall()]
   updated_ids=set()
   for fixture in live_fixtures:
    if not fixture.get("kickoff") or str(fixture["kickoff"])[:10] != day:
     continue
    # Match by both teams, league and kickoff proximity; team names alone are unsafe.
    matched_rows=matching_fixture_rows(rows,fixture,provider._normalize_search_name)
    for row in matched_rows:
     c.execute("""UPDATE matches SET status='live',status_short=?,
       elapsed_minute=COALESCE(?,elapsed_minute),
       home_goals=COALESCE(?,home_goals),away_goals=COALESCE(?,away_goals)
       WHERE id=?""",
       (fixture.get("status_short") or "LIVE",fixture.get("elapsed_minute"),
        fixture.get("home_goals"),fixture.get("away_goals"),row["id"]))
     updated_ids.add(row["id"])
   # Expire old live flags without deleting the match or its prediction/history rows.
   stale_cutoff=(datetime.now(ZoneInfo("Europe/Istanbul"))-timedelta(hours=4)).isoformat()
   c.execute("""UPDATE matches SET status='scheduled',status_short='UNVERIFIED',
      elapsed_minute=NULL
      WHERE substr(kickoff,1,10)=? AND status='live' AND kickoff < ?""",(day,stale_cutoff))
   rows=c.execute("""SELECT id,status_short,elapsed_minute,home_goals,away_goals
    FROM matches WHERE substr(kickoff,1,10)=? AND status='live' ORDER BY kickoff""",(day,)).fetchall()
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
    label="Canlı · Dakika bilgisi bekleniyor"
   result.append({"id":row["id"],"minute_label":label,
    "minute":minute,
    "score":f"{row['home_goals']} – {row['away_goals']}" if row["home_goals"] is not None and row["away_goals"] is not None else "—"})
  return jsonify({"matches":result,"updated_at":datetime.now(ZoneInfo("Europe/Istanbul")).isoformat(),
   "provider_confirmed_live":len(updated_ids)})
 except ProviderError as exc:
  return jsonify({"error":str(exc)}),502
 except Exception as exc:
  app.logger.exception("Live fixture refresh failed")
  return jsonify({"error":str(exc)}),500

@app.post("/sync/results/refresh")
def refresh_results():
 """Token-protected, date-scoped sync for fixtures and verified results."""
 token=os.environ.get("RESULT_SYNC_TOKEN","").strip()
 if not token:
  return jsonify({"error":"RESULT_SYNC_TOKEN yapılandırılmamış."}),503
 supplied=request.headers.get("Authorization","")
 if not hmac.compare_digest(supplied,"Bearer "+token):
  return jsonify({"error":"Yetkisiz."}),401

 today=datetime.now(ZoneInfo("Europe/Istanbul")).date()
 requested_day=(request.args.get("date") or "").strip()
 if requested_day:
  try:
   target_date=date.fromisoformat(requested_day)
  except ValueError:
   return jsonify({"error":"Tarih YYYY-MM-DD biçiminde olmalı."}),400
  if target_date < today-timedelta(days=14) or target_date > today:
   return jsonify({"error":"Senkronizasyon tarihi son 14 gün ile bugün arasında olmalı."}),400
  days=[target_date.isoformat()]
 else:
  days=[(today-timedelta(days=offset)).isoformat() for offset in (2,1,0)]

 provider=FootballProvider(os.environ.get("SPORTSCORE_API_KEY"))
 fetched=0
 matched=0
 finished=0
 evaluated=0
 upcoming_added=0
 upcoming_ready=0

 def key(value):
  return " ".join(sorted(provider._normalize_search_name(value).split()))

 try:
  # Process and commit one day at a time. The scheduled GitHub workflow calls
  # this endpoint once per day to stay well below the web worker timeout.
  for target_day in days:
   fixtures=provider.fixtures(target_day)
   fetched+=len(fixtures)
   with db() as c:
    candidates=[dict(row) for row in c.execute("""SELECT matches.*, EXISTS(SELECT 1 FROM user_saved_matches s WHERE s.match_id=matches.id) AS is_saved
      FROM matches
      WHERE substr(kickoff,1,10)=?
      ORDER BY id""",(target_day,)).fetchall()]
    day_results=c.execute("""SELECT r.match_id,r.market,r.prediction,r.actual,r.outcome
      FROM results r JOIN matches m ON m.id=r.match_id
      WHERE substr(m.kickoff,1,10)=?""",(target_day,)).fetchall()
    results_by_match={}
    for result in day_results:
     results_by_match.setdefault(result["match_id"],[]).append(dict(result))
    candidate_by_key={}
    candidate_by_pair={}
    for candidate in candidates:
     pair=(key(candidate["home"]),key(candidate["away"]))
     triple=pair+(key(candidate["league"]),)
     if triple not in candidate_by_key or candidate.get("is_saved"):
      candidate_by_key[triple]=candidate
     candidate_by_pair.setdefault(pair,[]).append(candidate)

    for fixture in fixtures:
     state=fixture.get("status")
     kickoff_day=str(fixture.get("kickoff") or "")[:10]
     if not kickoff_day or kickoff_day!=target_day:
      continue
     pair=(key(fixture.get("home")),key(fixture.get("away")))
     triple=pair+(key(fixture.get("league")),)
     possible=candidate_by_pair.get(pair,[])
     identity_matches=matching_fixture_rows(possible,fixture,provider._normalize_search_name)
     row=prefer_saved_match(identity_matches)
     if row is None and identity_matches:
      # Exact duplicate rows are safe to sync together; choose a stable representative.
      row=sorted(identity_matches,key=lambda item:int(item["id"]))[0]
     # Never create a second fixture when a same-team candidate exists but its
     # league/kickoff cannot verify that it is the same match.
     identity_ambiguous=bool(possible) and not identity_matches

     if state=="scheduled":
      if kickoff_day < today.isoformat():
       continue
      if row:
       if row["status"]=="scheduled":
        upcoming_ready+=1
       continue
      if identity_ambiguous:
       continue
      mid=upsert_fixture_record(c,fixture)
      if mid:
       row={
        "id":mid,"home":fixture.get("home",""),"away":fixture.get("away",""),
        "league":fixture.get("league",""),"status":"scheduled",
        **{market:"" for market,_label in MARKETS}
       }
       candidate_by_key[triple]=row
       candidate_by_pair.setdefault(pair,[]).append(row)
       upcoming_added+=1
       upcoming_ready+=1
      continue

     if state not in ("live","finished"):
      continue
     if state=="finished" and (fixture.get("home_goals") is None or fixture.get("away_goals") is None):
      continue

     if row is None:
      # Import only when no same-team local candidate exists. Ambiguous identity
      # is skipped instead of creating a duplicate fixture row.
      if identity_ambiguous:
       continue
      mid=upsert_fixture_record(c,fixture)
      if not mid:
       continue
      row={
       "id":mid,"home":fixture.get("home",""),"away":fixture.get("away",""),
       "league":fixture.get("league",""),"status":state,
       **{market:"" for market,_label in MARKETS}
      }
      candidate_by_key[triple]=row
      candidate_by_pair.setdefault(pair,[]).append(row)

     mid=row["id"]
     c.execute("""UPDATE matches SET status=?,status_short=?,
       home_goals=COALESCE(?,home_goals),away_goals=COALESCE(?,away_goals),
       ht_home=COALESCE(?,ht_home),ht_away=COALESCE(?,ht_away),
       elapsed_minute=CASE WHEN ?='finished' THEN NULL ELSE COALESCE(?,elapsed_minute) END
       WHERE id=?""",
       (state,fixture.get("status_short") or ("FT" if state=="finished" else "LIVE"),
        fixture.get("home_goals"),fixture.get("away_goals"),fixture.get("ht_home"),fixture.get("ht_away"),
        state,fixture.get("elapsed_minute"),mid))
     row["status"]=state
     # Keep exact duplicate rows (including a user's saved copy) synchronized.
     duplicate_rows=matching_fixture_rows(candidates,fixture,provider._normalize_search_name)
     for twin in duplicate_rows:
      if twin["id"]==mid:
       continue
      c.execute("""UPDATE matches SET status=?,status_short=?,
       home_goals=COALESCE(?,home_goals),away_goals=COALESCE(?,away_goals),
       ht_home=COALESCE(?,ht_home),ht_away=COALESCE(?,ht_away),
       elapsed_minute=CASE WHEN ?='finished' THEN NULL ELSE COALESCE(?,elapsed_minute) END
       WHERE id=?""",
       (state,fixture.get("status_short") or ("FT" if state=="finished" else "LIVE"),
        fixture.get("home_goals"),fixture.get("away_goals"),fixture.get("ht_home"),fixture.get("ht_away"),
        state,fixture.get("elapsed_minute"),twin["id"]))
     matched+=1
     if state!="finished":
      continue

     finished+=1
     actuals=actual_market_values(fixture)
     actual_by_label={
      label:actuals[market]
      for market,label in MARKETS
      if market in actuals
     }
     match_results=results_by_match.setdefault(mid,[])
     existing_markets={result["market"] for result in match_results}
     for market,label in MARKETS:
      prediction=row.get(market)
      if prediction and label not in existing_markets:
       c.execute("INSERT INTO results(match_id,market,prediction) VALUES(?,?,?) ON CONFLICT(match_id,market) DO UPDATE SET prediction=excluded.prediction",
        (mid,label,prediction))
       match_results.append({"market":label,"prediction":prediction,"actual":"","outcome":"pending"})
       existing_markets.add(label)
     for result_row in match_results:
      actual=actual_by_label.get(result_row["market"])
      prediction=result_row["prediction"]
      if actual is None or not prediction:
       continue
      outcome=evaluate_prediction(prediction,actual)
      if result_row.get("actual")!=actual or result_row.get("outcome")!=outcome:
       c.execute("UPDATE results SET actual=?,outcome=? WHERE match_id=? AND market=?",
        (actual,outcome,mid,result_row["market"]))
       result_row["actual"]=actual
       result_row["outcome"]=outcome
       evaluated+=1

    c.execute("INSERT INTO sync_log(provider,day,ran_at,fixtures_count,status,message) VALUES(?,?,?,?,?,?)",
     ("sportscore",target_day,datetime.utcnow().isoformat(),len(fixtures),"success",
      f"{target_day} senkronizasyonu: {len(fixtures)} sağlayıcı maçı, {upcoming_added} yeni yaklaşan maç, {upcoming_ready} yaklaşan maç, {matched} canlı/biten eşleşme, {finished} biten maç, {evaluated} pazar sonucu."))

  return jsonify({"ok":True,"days":days,"fetched":fetched,"upcoming_added":upcoming_added,
   "upcoming_ready":upcoming_ready,"matched":matched,"finished":finished,"evaluated_markets":evaluated,
   "updated_at":datetime.now(ZoneInfo("Europe/Istanbul")).isoformat()})
 except ProviderError as exc:
  app.logger.warning("Result sync provider error: %s",exc)
  with db() as c:
   c.execute("INSERT INTO sync_log(provider,day,ran_at,status,message) VALUES(?,?,?,?,?)",
    ("sportscore",requested_day or today.isoformat(),datetime.utcnow().isoformat(),"error",str(exc)))
  return jsonify({"error":str(exc)}),502
 except Exception as exc:
  app.logger.exception("Result sync failed")
  return jsonify({"error":"Sonuç senkronizasyonu başarısız."}),500


@app.post("/sync/predictions/refresh")
def refresh_missing_predictions():
 """Token-protected small batches of predictions for today's verified fixtures."""
 token=os.environ.get("RESULT_SYNC_TOKEN","").strip()
 if not token:
  return jsonify({"error":"RESULT_SYNC_TOKEN yapılandırılmamış."}),503
 supplied=request.headers.get("Authorization","")
 if not hmac.compare_digest(supplied,"Bearer "+token):
  return jsonify({"error":"Yetkisiz."}),401

 today=datetime.now(ZoneInfo("Europe/Istanbul")).date()
 requested_day=(request.args.get("date") or today.isoformat()).strip()
 if requested_day!=today.isoformat():
  return jsonify({"error":"Bu görev yalnızca bugünün yaklaşan maçlarını işler."}),400

 try:
  limit=max(1,min(8,int(request.args.get("limit","8"))))
 except (TypeError,ValueError):
  limit=8

 now_local=datetime.now(ZoneInfo("Europe/Istanbul"))
 with db() as c:
  history=[dict(row) for row in c.execute("""SELECT id,home,away,league,status,
    home_goals,away_goals,ht_home,ht_away FROM matches
    WHERE status='finished' AND home_goals IS NOT NULL AND away_goals IS NOT NULL
    ORDER BY kickoff""").fetchall()]
  candidates=[dict(row) for row in c.execute("""SELECT * FROM matches
    WHERE substr(kickoff,1,10)=? AND status='scheduled'
    ORDER BY kickoff,id""",(requested_day,)).fetchall()]

  eligible=[]
  for match in candidates:
   try:
    kickoff=datetime.fromisoformat(str(match.get("kickoff") or "").replace("Z","+00:00"))
    if kickoff.tzinfo is None:
     kickoff=kickoff.replace(tzinfo=ZoneInfo("Europe/Istanbul"))
    else:
     kickoff=kickoff.astimezone(ZoneInfo("Europe/Istanbul"))
   except (TypeError,ValueError):
    continue
   has_prediction=bool(match.get("market_probabilities") and match["market_probabilities"]!="{}")
   has_market=any(match.get(key) for key in ("match_result","htft","over25","btts","first_half_btts","second_half_btts"))
   if kickoff>=now_local and not has_prediction and not has_market and not match.get("prediction_message"):
    eligible.append(match)

  batch=eligible[:limit]
  history_index=prepare_history(history)
  for target in batch:
   rebuild_one_prediction(c,target,history_index)

  remaining=max(0,len(eligible)-len(batch))
  generated=sum(
   1 for target in batch
   if c.execute("SELECT market_probabilities FROM matches WHERE id=?",(target["id"],)).fetchone()["market_probabilities"] not in ("","{}")
  )
 return jsonify({
  "ok":True,"date":requested_day,"processed":len(batch),
  "generated":generated,"remaining":remaining,
  "updated_at":datetime.now(ZoneInfo("Europe/Istanbul")).isoformat()
 })


@app.post("/sync/history/start")
@admin_required
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

@app.get("/sync/history/status")
@admin_required
def history_sync_status():
 job_id=request.args.get("job_id",type=int)
 with db() as c:
  job=c.execute("SELECT * FROM sync_jobs WHERE id=?",(job_id,)).fetchone() if job_id else c.execute("SELECT * FROM sync_jobs ORDER BY id DESC LIMIT 1").fetchone()
 if not job:return jsonify({"status":"idle","percent":0})
 return jsonify(history_job_payload(job))

@app.post("/sync/history/step")
@admin_required
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


@app.route("/coupon")
def coupon_center():
    """Filter real, stored pre-match probabilities; never fabricate bookmaker prices."""
    market = request.args.get("market", "all")
    try:
        min_probability = max(0.0, min(100.0, float(request.args.get("min_probability", "70"))))
        min_odds = max(1.01, float(request.args.get("min_odds", "1.40")))
        max_odds = max(min_odds, min(1000.0, float(request.args.get("max_odds", "30"))))
    except (TypeError, ValueError):
        min_probability, min_odds, max_odds = 70.0, 1.40, 30.0
    league_filter = request.args.get("league", "all")
    risk = request.args.get("risk", "balanced")
    if risk not in ("conservative", "balanced", "aggressive"):
        risk = "balanced"
    risk_floor = {"conservative": 80.0, "balanced": 70.0, "aggressive": 60.0}[risk]
    effective_floor = max(min_probability, risk_floor)
    market_labels = {"match_result":"Maç Sonucu","goals":"2,5 Gol","btts":"KG Var/Yok",
        "htft":"İY/MS","first_half_btts":"1. Yarı KG","second_half_btts":"2. Yarı KG","corners":"Korner"}
    with db() as c:
        uid = current_user_id() or 0
        saved_predictions = {(row["match_id"], row["market_key"], row["selection_key"]) for row in c.execute("SELECT match_id,market_key,selection_key FROM user_saved_predictions WHERE user_id=?", (uid,)).fetchall()} if uid else set()
        fixtures = c.execute("""SELECT * FROM matches
            WHERE status='scheduled' AND datetime(kickoff)>=datetime('now')
            ORDER BY kickoff,id""").fetchall()
        leagues = sorted({str(row["league"] or "").strip() for row in fixtures if str(row["league"] or "").strip()}, key=str.casefold)
    ready, waiting, seen = [], [], set()
    for match in fixtures:
        identity=(str(match["home"]).strip().casefold(),str(match["away"]).strip().casefold(),
            str(match["league"] or "").strip().casefold(),str(match["kickoff"] or ""))
        if identity in seen: continue
        seen.add(identity)
        if league_filter != "all" and (match["league"] or "") != league_filter: continue
        try: probabilities=json.loads(match["market_probabilities"] or "{}")
        except (TypeError,ValueError): probabilities={}
        selections=[]
        for option,label,odd_key in (("1","MS 1","odds_home"),("X","MS X","odds_draw"),("2","MS 2","odds_away")):
            value=(probabilities.get("match_result") or {}).get(option)
            if value is not None: selections.append(("match_result",option,label,value,match[odd_key]))
        goals=(probabilities.get("goals") or {}).get("2.5") or {}
        for option,label in (("over","Üst 2,5"),("under","Alt 2,5")):
            if goals.get(option) is not None: selections.append(("goals",option,label,goals[option],None))
        for option,label in (("Var","KG Var"),("Yok","KG Yok")):
            value=(probabilities.get("btts") or {}).get(option)
            if value is not None: selections.append(("btts",option,label,value,None))
        for key,label in (("htft","İY/MS"),("first_half_btts","1. Yarı KG"),("second_half_btts","2. Yarı KG")):
            for option,value in (probabilities.get(key) or {}).items():
                selections.append((key,option,option if key=="htft" else label+" "+option,value,None))
        for key,selection_key,label,probability,raw_odds in selections:
            if market not in ("all",key): continue
            try: probability=float(probability)
            except (TypeError,ValueError): continue
            if not 0 <= probability <= 1 or probability*100 < effective_floor: continue
            try: odds=float(str(raw_odds).replace(",",".")) if raw_odds not in (None,"") else None
            except (TypeError,ValueError): odds=None
            row={"id":match["id"],"home":match["home"],"away":match["away"],
                "league":match["league"] or "Lig belirtilmedi","kickoff":match["kickoff"],
                "market":market_labels.get(key,key),"selection":label,
                "probability":round(probability*100,1),"odds":odds,"saved":(match["id"],key,str(selection_key)) in saved_predictions,
                "market_key":key,"selection_key":str(selection_key)}
            if odds is None: waiting.append(row)
            elif min_odds <= odds <= max_odds: ready.append(row)
    ready.sort(key=lambda row:(-row["probability"],row["kickoff"]))
    waiting.sort(key=lambda row:(-row["probability"],row["kickoff"]))
    return render_template("coupon.html",ready=ready,waiting=waiting,leagues=leagues,
        filters={"market":market,"min_probability":min_probability,"min_odds":min_odds,
            "max_odds":max_odds,"league":league_filter,"risk":risk},effective_floor=effective_floor)

@app.route("/match/new",methods=["GET","POST"])
@admin_required
def new_match():
 if request.method=="POST":
  keys=["home","away","league","kickoff","odds_home","odds_draw","odds_away","htft","over25","btts","first_half_btts","second_half_btts","corners","notes"]
  d={k:request.form.get(k,"").strip() for k in keys}
  if not d["home"] or not d["away"]: flash("İki takım adı zorunludur.","error"); return render_template("form.html",data=d)
  with db() as c:
   cur=c.execute("INSERT INTO matches(home,away,league,kickoff,status,odds_home,odds_draw,odds_away,htft,over25,btts,first_half_btts,second_half_btts,corners,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",(d["home"],d["away"],d["league"],d["kickoff"],"scheduled",d["odds_home"],d["odds_draw"],d["odds_away"],d["htft"],d["over25"],d["btts"],d["first_half_btts"],d["second_half_btts"],d["corners"],d["notes"],datetime.utcnow().isoformat()))
   sync_prediction_rows(c,cur.fetchone()["id"])
  flash("Maç eklendi.","success")
  return redirect(url_for("index"))
 return render_template("form.html",data={})

@app.post("/match/<int:mid>/save-prediction")
@login_required
def save_prediction(mid):
    market_key = (request.form.get("market_key") or "").strip()
    selection_key = (request.form.get("selection_key") or "").strip()
    allowed = {"match_result", "goals", "btts", "htft", "first_half_btts", "second_half_btts"}
    if market_key not in allowed or not selection_key:
        return jsonify({"ok": False, "error": "Geçersiz tahmin seçimi."}), 400
    with db() as c:
        match = c.execute("SELECT market_probabilities,status FROM matches WHERE id=?", (mid,)).fetchone()
        if not match:
            return jsonify({"ok": False, "error": "Maç bulunamadı."}), 404
        try:
            probabilities = json.loads(match["market_probabilities"] or "{}")
        except (TypeError, ValueError):
            probabilities = {}
        market_data = probabilities.get(market_key) or {}
        if market_key == "goals":
            market_data = market_data.get("2.5") or {}
        if selection_key not in market_data:
            return jsonify({"ok": False, "error": "Bu tahmin maçta bulunamadı."}), 400
        c.execute("""INSERT INTO user_saved_predictions(user_id,match_id,market_key,selection_key,saved_at)
            VALUES(?,?,?,?,?) ON CONFLICT(user_id,match_id,market_key,selection_key) DO NOTHING""",
            (current_user_id(), mid, market_key, selection_key, datetime.utcnow().isoformat()))
    return jsonify({"ok": True, "saved": True, "match_id": mid, "market_key": market_key, "selection_key": selection_key})


@app.post("/match/<int:mid>/unsave-prediction")
@login_required
def unsave_prediction(mid):
    market_key = (request.form.get("market_key") or "").strip()
    selection_key = (request.form.get("selection_key") or "").strip()
    with db() as c:
        c.execute("DELETE FROM user_saved_predictions WHERE user_id=? AND match_id=? AND market_key=? AND selection_key=?",
            (current_user_id(), mid, market_key, selection_key))
    return jsonify({"ok": True, "saved": False})


@app.post("/match/<int:mid>/save")
@login_required
def save(mid):
 uid=current_user_id()
 with db() as c:
  if c.execute("SELECT 1 FROM matches WHERE id=?",(mid,)).fetchone():
   c.execute("INSERT INTO user_saved_matches(user_id,match_id,saved_at) VALUES(?,?,?) ON CONFLICT(user_id,match_id) DO NOTHING",(uid,mid,datetime.utcnow().isoformat()))
 flash("Maç kaydedildi. Kaydedilen maçlar bölümünden her zaman ulaşabilirsin.","success")
 if request.args.get("next") == "coupon": return redirect(url_for("coupon_center"))
 return redirect(url_for("detail",mid=mid))
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
   m=c.execute("SELECT m.*,s.id saved FROM matches m LEFT JOIN user_saved_matches s ON s.match_id=m.id AND s.user_id=? WHERE m.id=?",(current_user_id() or 0,mid)).fetchone()
  rs=c.execute("SELECT * FROM results WHERE match_id=? ORDER BY id",(mid,)).fetchall()
  confidence=json.loads(m["prediction_confidence"] or "{}")
  market_probabilities=json.loads(m["market_probabilities"] or "{}")
 if not m:return "Maç bulunamadı",404
 return render_template("detail.html",m=m,results=rs,markets=MARKETS,confidence=confidence,market_probabilities=market_probabilities)

@app.get("/match/<int:mid>/live-data")
def match_live_data(mid):
 # Read-only: live feed lookup and match detail requests do not write to the database.
 with db() as c:
  row=c.execute("SELECT id,home,away,kickoff,status,status_short,elapsed_minute,home_goals,away_goals,ht_home,ht_away FROM matches WHERE id=?",(mid,)).fetchone()
 if not row: return jsonify({"error":"Maç bulunamadı."}),404
 match=dict(row)
 if match["status"]!="live":
  return jsonify({"status":match["status"],"events":[],"minute":match["elapsed_minute"]})
 try:
  provider=FootballProvider(os.environ.get("SPORTSCORE_API_KEY"))
  # Resolve the provider's actual fixture slug from its current live feed.
  # The database external_id is an import identifier, not assumed to be a slug.
  live_fixtures=provider.live_matches()
  fixture=provider.find_matching_live_fixture(live_fixtures,match.get("home"),match.get("away"),match.get("kickoff"))
  # Fallback to the date-scoped fixtures endpoint if the live widget omits this match.
  if not fixture and match.get("kickoff"):
   try:
    match_day=datetime.fromisoformat(str(match["kickoff"]).replace("Z","+00:00")).date().isoformat()
    daily_fixtures=provider.fixtures(match_day,status="live")
    fixture=provider.find_matching_live_fixture(daily_fixtures,match.get("home"),match.get("away"),match.get("kickoff"))
    app.logger.info("Live detail fallback match=%s widget_count=%s daily_count=%s matched=%s",
     mid,len(live_fixtures),len(daily_fixtures),bool(fixture))
   except (ValueError,TypeError) as date_error:
    app.logger.warning("Live detail invalid kickoff match=%s: %s",mid,date_error)
  if not fixture:
   app.logger.warning("Live detail fixture not matched: match_id=%s home=%r away=%r kickoff=%r widget_count=%s",
    mid,match.get("home"),match.get("away"),match.get("kickoff"),len(live_fixtures))
   return jsonify({"status":"live","minute":match.get("elapsed_minute"),
    "minute_label":"Canlı dakika bekleniyor","score":(
     f"{match['home_goals']} – {match['away_goals']}" if match["home_goals"] is not None else None),
    "ht_score":None,"events":[],"events_available":False,
    "message":"Canlı veri sağlayıcısında bu maç henüz eşleştirilemedi."})
  provider_slug=fixture.get("slug") or fixture.get("id")
  payload=provider.match_detail(provider_slug)
  events=provider.parse_match_events(payload)
  minute=fixture.get("elapsed_minute")
  code=str(fixture.get("status_short") or "").upper().replace(" ","_")
  period="halftime" if code in ("HT","INT","BREAK","BT") else "finished" if code in ("FT","AET","PEN","FINISHED") else "second_half" if code in ("2H","2ND_HALF","SECOND_HALF") else "first_half" if code in ("1H","1ST_HALF","FIRST_HALF") else "live"
  hg,ag=fixture.get("home_goals"),fixture.get("away_goals")
  hh,ha=fixture.get("ht_home"),fixture.get("ht_away")
  if hg is None: hg=match.get("home_goals")
  if ag is None: ag=match.get("away_goals")
  if hh is None: hh=match.get("ht_home")
  if ha is None: ha=match.get("ht_away")
  return jsonify({"status":period,"minute":minute,
   "minute_label":"Devre Arası" if period=="halftime" else "Maç Tamamlandı" if period=="finished" else
    (f"{minute}' · {'2. Yarı' if period=='second_half' else '1. Yarı' if period=='first_half' else 'Canlı'}" if minute is not None else
     ("2. Yarı" if period=="second_half" else "1. Yarı" if period=="first_half" else "Canlı dakika bekleniyor")),
   "score":f"{hg} – {ag}" if hg is not None and ag is not None else None,
   "ht_score":f"{hh} – {ha}" if hh is not None and ha is not None else None,
   "events":events,"events_available":bool(events),
   "message":None if events else "Sağlayıcı bu karşılaşma için henüz olay listesi döndürmedi."})
 except ProviderError as exc:
  app.logger.warning("Live detail unavailable for match %s: %s",mid,exc)
  return jsonify({"status":"unavailable","minute":match.get("elapsed_minute"),"events":[],
   "events_available":False,"message":"Canlı veri sağlayıcısına şu anda ulaşılamıyor."}),502

@app.post("/match/<int:mid>/status")
@admin_required
def status(mid):
 st=request.form.get("status","scheduled")
 if st not in ("scheduled","live","finished"):st="scheduled"
 with db() as c:c.execute("UPDATE matches SET status=? WHERE id=?",(st,mid))
 return redirect(url_for("detail",mid=mid))
@app.post("/match/<int:mid>/result")
@admin_required
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
 with db() as c:
  uid=current_user_id()
  ms=c.execute("SELECT m.* FROM matches m JOIN user_saved_matches s ON s.match_id=m.id WHERE s.user_id=? ORDER BY s.saved_at DESC",(uid,)).fetchall()
  rows=c.execute("""SELECT r.* FROM results r JOIN user_saved_matches s ON s.match_id=r.match_id WHERE s.user_id=? ORDER BY r.match_id,r.id""",(uid,)).fetchall()
  selected=c.execute("""SELECT m.id,m.home,m.away,m.league,m.kickoff,m.status,m.home_goals,m.away_goals,
      r.market,r.prediction,r.actual,r.outcome,sp.market_key,sp.selection_key
      FROM user_saved_predictions sp JOIN matches m ON m.id=sp.match_id
      LEFT JOIN results r ON r.match_id=sp.match_id AND
       ((sp.market_key='match_result' AND r.market='Maç Sonucu') OR
        (sp.market_key='goals' AND r.market='2,5 Gol') OR
        (sp.market_key='btts' AND r.market='KG') OR
        (sp.market_key='htft' AND r.market='İY/MS') OR
        (sp.market_key='first_half_btts' AND r.market='1. Yarı KG') OR
        (sp.market_key='second_half_btts' AND r.market='2. Yarı KG'))
      WHERE sp.user_id=? ORDER BY sp.saved_at DESC""",(uid,)).fetchall()
 results_by_match={}
 for row in rows: results_by_match.setdefault(row["match_id"],[]).append(dict(row))
 return render_template("saved.html",matches=ms,results_by_match=results_by_match,selected_predictions=selected)
def _goal_side_label(market, selection):
 """Hide repeated goal-line value in selection labels on the results screen."""
 market_key="".join(str(market or "").casefold().replace(",",".").split())
 if market_key not in {"1.5gol","2.5gol","3.5gol","4.5gol"}:
  return selection or "—"
 value=str(selection or "").strip().casefold()
 if value.startswith(("üst","over")):
  return "Üst"
 if value.startswith(("alt","under")):
  return "Alt"
 return selection or "—"


@app.route("/results")
def results():
 with db() as c:
  rows=c.execute("""SELECT m.id AS match_id,m.home,m.away,m.league,m.kickoff,
   m.ht_home,m.ht_away,m.home_goals,m.away_goals,
   r.market,r.prediction,r.actual,r.outcome
   FROM results r JOIN matches m ON m.id=r.match_id
   WHERE m.status='finished'
   ORDER BY m.kickoff DESC,m.id DESC,r.id ASC""").fetchall()
  stats=c.execute("SELECT outcome,COUNT(*) n FROM results WHERE outcome IN ('won','lost') GROUP BY outcome").fetchall()
 counts={r["outcome"]:r["n"] for r in stats}; won=counts.get("won",0); lost=counts.get("lost",0)
 grouped={}
 for row in rows:
  match_id=row["match_id"]
  if match_id not in grouped:
   kickoff_date=str(row["kickoff"] or "")[:10]
   date_label="—"
   if len(kickoff_date)==10 and kickoff_date[4:5]=="-" and kickoff_date[7:8]=="-":
    date_label=f"{kickoff_date[8:10]}.{kickoff_date[5:7]}.{kickoff_date[0:4]}"
   grouped[match_id]={
    "date":date_label,"home":row["home"],"away":row["away"],
    "ht_score":f"{row['ht_home']} – {row['ht_away']}" if row["ht_home"] is not None and row["ht_away"] is not None else "—",
    "ft_score":f"{row['home_goals']} – {row['away_goals']}" if row["home_goals"] is not None and row["away_goals"] is not None else "—",
    "markets":[]}
  grouped[match_id]["markets"].append({
   "market":row["market"],
   "prediction":row["prediction"],
   "actual":row["actual"],
   "prediction_display":_goal_side_label(row["market"],row["prediction"]),
   "actual_display":_goal_side_label(row["market"],row["actual"]),
   "outcome":row["outcome"]})
 return render_template("results.html",rows=list(grouped.values()),won=won,lost=lost,
  pct=round(won*100/(won+lost)) if won+lost else 0)
if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)),debug=True)
