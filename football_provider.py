import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE_URL = "https://sportscore.com/api/v1/fixtures/"

class ProviderError(Exception):
    pass

class FootballProvider:
    def __init__(self, api_key=None):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "SRC-Match-Analyst/1.0"})
        if api_key:
            self.session.headers["X-Api-Key"] = api_key

    @staticmethod
    def pick(obj, *keys, default=None):
        if isinstance(obj, dict):
            for key in keys:
                value = obj.get(key)
                if value is not None and value != "":
                    return value
        return default

    @staticmethod
    def team_name(value, fallback):
        if isinstance(value, dict):
            return value.get("name") or value.get("short_name") or fallback
        return value or fallback

    @staticmethod
    def local_kickoff(value):
        if value in (None, ""):
            return ""
        if isinstance(value, (int, float)):
            return str(value)
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            # SportScore fixture timestamps are UTC. Some serialized values omit
            # the trailing Z, so interpret naive ISO timestamps as UTC as well.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(ZoneInfo("Europe/Istanbul")).isoformat()
        except (ValueError, TypeError):
            pass
        return text

    def fixtures(self, day, status=None):
        params = {"sport": "football", "date": day, "limit": 200}
        if status in ("live", "finished", "upcoming"):
            params["status"] = status
        try:
            # Keep each date-sized request comfortably below Render's 30s worker timeout.
            response = self.session.get(BASE_URL, params=params, timeout=10)
        except requests.RequestException as exc:
            raise ProviderError(f"SportScore bağlantı hatası: {exc}") from exc
        if response.status_code == 429:
            raise ProviderError("SportScore istek sınırı doldu. Biraz sonra tekrar deneyin.")
        if response.status_code in (401, 403):
            raise ProviderError("SportScore erişimi reddetti. API anahtarını kontrol edin.")
        if not response.ok:
            raise ProviderError(f"SportScore HTTP {response.status_code}: {response.text[:250]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("SportScore geçerli JSON döndürmedi.") from exc

        rows = payload if isinstance(payload, list) else self.pick(payload, "fixtures", "matches", "results", "data", "response", default=[]) if isinstance(payload, dict) else []
        if isinstance(rows, dict):
            rows = self.pick(rows, "fixtures", "matches", "results", "data", default=[])
        if not isinstance(rows, list):
            raise ProviderError("SportScore yanıtında maç listesi bulunamadı.")

        output = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            home = self.pick(item, "home", "home_team", "team_home", default={})
            away = self.pick(item, "away", "away_team", "team_away", default={})
            if isinstance(home, dict) and isinstance(home.get("team"), dict): home = home["team"]
            if isinstance(away, dict) and isinstance(away.get("team"), dict): away = away["team"]
            home_name = self.team_name(home, "Ev sahibi")
            away_name = self.team_name(away, "Deplasman")
            if home_name == "Ev sahibi" or away_name == "Deplasman": continue

            league_obj = self.pick(item, "competition", "league", "tournament", default={})
            league = self.team_name(league_obj, "") if isinstance(league_obj, dict) else str(league_obj or "")
            country_obj = item.get("country") or (league_obj.get("country") if isinstance(league_obj, dict) else "")
            country = self.team_name(country_obj, "") if isinstance(country_obj, dict) else str(country_obj or "")
            if country and league: league = f"{country} · {league}"

            kickoff = self.pick(item, "kickoff", "date", "start_time", "time", "datetime", default="")
            if isinstance(kickoff, dict): kickoff = self.pick(kickoff, "utc", "datetime", "date", "timestamp", default="")
            status_obj = self.pick(item, "status", "match_status", "state", default="upcoming")
            if isinstance(status_obj, dict):
                raw_status = str(self.pick(status_obj, "long", "name", "label", "description", "status", "type", default="")).lower()
                code = str(self.pick(status_obj, "short", "code", "status_code", default=self.pick(item, "status_short", "status_code", "period", default=""))).upper()
            else:
                raw_status = str(self.pick(item, "status_text", "state", default=status_obj)).lower()
                code = str(self.pick(item, "status_short", "status_code", "period", default="")).upper()
            if any(s in raw_status for s in ("live", "in_progress", "in-play", "in play")) or code in ("1H","HT","2H","ET","P","LIVE","INT"):
                state = "live"
            elif any(s in raw_status for s in ("finished", "complete", "ended", "full_time", "full time", "match over")) or code in ("FT","AET","PEN","FINISHED","ENDED","FULL_TIME","FULLTIME"):
                state = "finished"
            else:
                state = "scheduled"

            score = item.get("score") if isinstance(item.get("score"), dict) else {}
            scores = item.get("scores") if isinstance(item.get("scores"), dict) else {}
            full = self.pick(score, "full_time", "fulltime", "ft", default=self.pick(scores, "full_time", "fulltime", "ft", default={}))
            if not isinstance(full, dict): full = {}
            hg = self.pick(item, "home_score", "home_goals", default=self.pick(score, "home", "home_score", default=self.pick(full, "home", "home_score", default=self.pick(scores, "home", "home_score"))))
            ag = self.pick(item, "away_score", "away_goals", default=self.pick(score, "away", "away_score", default=self.pick(full, "away", "away_score", default=self.pick(scores, "away", "away_score"))))
            ht = item.get("halftime") if isinstance(item.get("halftime"), dict) else item.get("half_time") if isinstance(item.get("half_time"), dict) else scores.get("halftime", {}) if isinstance(scores.get("halftime"), dict) else score.get("halftime", {}) if isinstance(score.get("halftime"), dict) else score.get("half_time", {}) if isinstance(score.get("half_time"), dict) else {}
            ht_home = self.pick(item, "ht_home", "home_ht", default=self.pick(ht, "home", "home_score"))
            ht_away = self.pick(item, "ht_away", "away_ht", default=self.pick(ht, "away", "away_score"))
            def score_int(value):
                try:
                    return int(value) if value is not None and str(value).strip() != "" else None
                except (TypeError, ValueError):
                    return None
            output.append({
                "id": str(self.pick(item, "id", "fixture_id", "match_id", "slug", default="")),
                "home": str(home_name), "away": str(away_name), "league": league,
                "kickoff": self.local_kickoff(kickoff), "status": state, "status_short": code or raw_status.upper()[:12],
                "home_goals": score_int(hg), "away_goals": score_int(ag),
                "elapsed_minute": self.pick(item, "elapsed", "elapsed_minute", "minute", "match_minute", default=self.pick(item.get("time", {}) if isinstance(item.get("time"), dict) else {}, "elapsed", "minute")),
                "ht_home": score_int(ht_home), "ht_away": score_int(ht_away)
            })
        return output
