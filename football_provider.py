import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE_URL = "https://sportscore.com/api/v1/fixtures/"
SEARCH_URL = "https://sportscore.com/api/v1/search/"
TEAM_URL = "https://sportscore.com/api/widget/team/"
LIVE_MATCHES_URL = "https://sportscore.com/api/widget/matches/"

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
    def elapsed_minute(item):
        """Read the match clock from SportScore fixture and widget payload variants."""
        import re

        def normalize(value):
            if isinstance(value, dict):
                minute = FootballProvider.pick(
                    value, "elapsed", "elapsed_minute", "elapsed_minutes", "minute",
                    "minutes", "value", "current", "timer", "clock"
                )
                extra = FootballProvider.pick(value, "extra", "added", "injury_time", "stoppage")
                if minute is None:
                    return None
                base = normalize(minute)
                if base is None:
                    return None
                return f"{base}+{extra}" if extra not in (None, "", 0, "0") else base
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value) if float(value).is_integer() else value
            if not isinstance(value, str):
                return None
            text = value.strip()
            if not text:
                return None
            match = re.fullmatch(
                r"(\d{1,3})(?:\s*\+\s*(\d{1,2}))?\s*(?:['′’]|m|min|mins|minute|minutes)?",
                text, flags=re.IGNORECASE
            )
            if match:
                return f"{match.group(1)}+{match.group(2)}" if match.group(2) else int(match.group(1))
            clock = re.fullmatch(r"(\d{1,3}):(\d{2})", text)
            if clock:
                return int(clock.group(1))
            return None

        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        time_obj = item.get("time") if isinstance(item.get("time"), dict) else {}
        timer_obj = item.get("timer") if isinstance(item.get("timer"), dict) else {}
        candidates = [
            item.get(key) for key in (
                "elapsed", "elapsed_minute", "elapsed_minutes", "minute",
                "match_minute", "elapsed_time", "timer", "clock", "time_elapsed"
            )
        ]
        candidates.extend([
            time_obj.get(key) for key in ("elapsed", "elapsed_minute", "elapsed_minutes", "minute", "timer", "clock")
        ])
        candidates.extend([
            status.get(key) for key in ("elapsed", "elapsed_minute", "elapsed_minutes", "minute", "timer", "clock")
        ])
        candidates.extend([
            timer_obj.get(key) for key in ("elapsed", "minute", "minutes", "value", "current")
        ])
        # SportScore can publish the match clock as status_text (e.g. "37'"
        # or "45+2") while the primary status remains simply "live".
        candidates.extend([item.get("status_text"), item.get("time_text"), item.get("minute_text")])
        for candidate in candidates:
            value = normalize(candidate)
            if value is not None:
                return value
        return None

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
            normalized_status = raw_status.strip().replace("-", "_").replace(" ", "_")
            live_codes = {"1H", "HT", "2H", "ET", "P", "LIVE", "INT", "INPLAY", "IN_PLAY",
                          "1ST_HALF", "2ND_HALF", "BREAK", "BT", "EXTRA_TIME"}
            finished_codes = {"FT", "AET", "PEN", "FINISHED", "ENDED", "FULL_TIME", "FULLTIME"}
            live_words = ("live", "in_progress", "inprogress", "in_play", "playing",
                          "first_half", "1st_half", "second_half", "2nd_half",
                          "half_time", "halftime", "extra_time", "penalty_shootout")
            finished_words = ("finished", "complete", "ended", "full_time", "fulltime",
                              "match_over", "after_extra_time", "after_penalties")
            elapsed_value = self.elapsed_minute(item)
            if any(word in normalized_status for word in live_words) or code in live_codes:
                state = "live"
            # SportScore sometimes sends compact status text (FT/AET/PEN)
            # without a separate status code.
            elif normalized_status in ("ft", "aet", "pen", "finished", "ended") or any(word in normalized_status for word in finished_words) or code in finished_codes:
                state = "finished"
            # A positive elapsed minute is stronger evidence of a live match
            # than an unrecognized provider label. Do not infer status from
            # score values alone: some feeds populate score fields prematurely.
            elif elapsed_value not in (None, "", 0, "0"):
                state = "live"
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
                "elapsed_minute": elapsed_value,
                "ht_home": score_int(ht_home), "ht_away": score_int(ht_away)
            })
        return output


    @staticmethod
    def _extract_rows(payload):
        """Accept both fixture API and team-widget response envelopes."""
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        keys = ("fixtures", "matches", "results", "data", "response", "schedule",
                "past_fixtures", "upcoming_fixtures", "events")
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = FootballProvider._extract_rows(value)
                if nested:
                    return nested
        # Some envelopes put the schedule one level under "team".
        for value in payload.values():
            if isinstance(value, dict):
                nested = FootballProvider._extract_rows(value)
                if nested:
                    return nested
        return []

    @staticmethod
    def _normalize_search_name(value):
        import re
        import unicodedata
        raw = str(value or "").casefold().translate(str.maketrans({
            "ı": "i", "ç": "c", "ğ": "g", "ö": "o", "ş": "s", "ü": "u"
        }))
        raw = unicodedata.normalize("NFKD", raw)
        raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
        words = re.findall(r"[a-z0-9]+", raw)
        removable = {"fc", "afc", "cf", "sc", "fk", "sk", "club", "football",
                     "futbol", "kulubu", "belediye", "belediyespor", "spor", "istanbul", "arca"}
        return " ".join(word for word in words if word not in removable)

    def search_team_slug(self, team_name):
        """Resolve a club to a SportScore team slug; never guess an ambiguous result."""
        try:
            response = self.session.get(
                SEARCH_URL,
                params={"sport": "football", "q": team_name, "limit": 20},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"SportScore takım arama bağlantı hatası: {exc}") from exc
        if response.status_code == 429:
            raise ProviderError("SportScore istek sınırı doldu.")
        if response.status_code in (401, 403):
            raise ProviderError("SportScore takım arama erişimi reddedildi.")
        if not response.ok:
            raise ProviderError(f"SportScore takım arama HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("SportScore takım araması geçerli JSON döndürmedi.") from exc

        target = self._normalize_search_name(team_name)
        candidates = []
        seen = set()

        def walk(value):
            if isinstance(value, dict):
                name = self.pick(value, "name", "team_name", "short_name", "display_name")
                slug = self.pick(value, "slug", "team_slug")
                if name and slug:
                    key = (str(name), str(slug))
                    if key not in seen:
                        seen.add(key)
                        candidates.append((self._normalize_search_name(name), str(slug)))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        exact = [slug for name, slug in candidates if name == target]
        if len(set(exact)) == 1:
            return exact[0]
        # Search results can use a formal name while our fixture uses a short
        # club name. Only accept a unique strong token match.
        target_tokens = set(target.split())
        scored = []
        for name, slug in candidates:
            tokens = set(name.split())
            overlap = len(target_tokens & tokens) / max(1, len(target_tokens | tokens))
            if overlap >= 0.75:
                scored.append((overlap, slug))
        if scored:
            best = max(score for score, _ in scored)
            best_slugs = {slug for score, slug in scored if score == best}
            if len(best_slugs) == 1:
                return next(iter(best_slugs))
        return None

    def parse_fixtures_payload(self, payload):
        """Parse serialized matches from either /fixtures or /team."""
        rows = self._extract_rows(payload)
        output = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            home = self.pick(item, "home", "home_team", "team_home", default={})
            away = self.pick(item, "away", "away_team", "team_away", default={})
            if isinstance(home, dict) and isinstance(home.get("team"), dict):
                home = home["team"]
            if isinstance(away, dict) and isinstance(away.get("team"), dict):
                away = away["team"]
            home_name = self.team_name(home, "Ev sahibi")
            away_name = self.team_name(away, "Deplasman")
            if home_name == "Ev sahibi" or away_name == "Deplasman":
                continue
            league_obj = self.pick(item, "competition", "league", "tournament", default={})
            league = self.team_name(league_obj, "") if isinstance(league_obj, dict) else str(league_obj or "")
            country_obj = item.get("country") or (league_obj.get("country") if isinstance(league_obj, dict) else "")
            country = self.team_name(country_obj, "") if isinstance(country_obj, dict) else str(country_obj or "")
            if country and league:
                league = f"{country} · {league}"
            kickoff = self.pick(item, "kickoff", "date", "start_time", "time", "datetime", default="")
            if isinstance(kickoff, dict):
                kickoff = self.pick(kickoff, "utc", "datetime", "date", "timestamp", default="")
            status_obj = self.pick(item, "status", "match_status", "state", default="upcoming")
            if isinstance(status_obj, dict):
                raw_status = str(self.pick(status_obj, "long", "name", "label", "description", "status", "type", default="")).lower()
                code = str(self.pick(status_obj, "short", "code", "status_code", default=self.pick(item, "status_short", "status_code", "period", default=""))).upper()
            else:
                raw_status = str(self.pick(item, "status_text", "state", default=status_obj)).lower()
                code = str(self.pick(item, "status_short", "status_code", "period", default="")).upper()
            elapsed_value = self.elapsed_minute(item)
            if any(s in raw_status for s in ("live", "in_progress", "in-play", "in play")) or code in ("1H", "HT", "2H", "ET", "P", "LIVE", "INT") or elapsed_value is not None:
                state = "live"
            elif raw_status.strip() in ("ft", "aet", "pen", "finished", "ended") or any(s in raw_status for s in ("finished", "complete", "ended", "full_time", "full time", "match over")) or code in ("FT", "AET", "PEN", "FINISHED", "ENDED", "FULL_TIME", "FULLTIME"):
                state = "finished"
            else:
                state = "scheduled"
            score = item.get("score") if isinstance(item.get("score"), dict) else {}
            scores = item.get("scores") if isinstance(item.get("scores"), dict) else {}
            full = self.pick(score, "full_time", "fulltime", "ft", default=self.pick(scores, "full_time", "fulltime", "ft", default={}))
            if not isinstance(full, dict):
                full = {}
            hg = self.pick(item, "home_score", "home_goals", default=self.pick(home if isinstance(home, dict) else {}, "score", default=self.pick(score, "home", "home_score", default=self.pick(full, "home", "home_score", default=self.pick(scores, "home", "home_score")))))
            ag = self.pick(item, "away_score", "away_goals", default=self.pick(away if isinstance(away, dict) else {}, "score", default=self.pick(score, "away", "away_score", default=self.pick(full, "away", "away_score", default=self.pick(scores, "away", "away_score")))))
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
                "kickoff": self.local_kickoff(kickoff), "status": state,
                "status_short": code or raw_status.upper()[:12],
                "home_goals": score_int(hg), "away_goals": score_int(ag),
                "elapsed_minute": elapsed_value,
                "ht_home": score_int(ht_home), "ht_away": score_int(ht_away)
            })
        return output

    def live_matches(self, limit=50):
        """Fetch the dedicated SportScore live/recent feed, which carries status_text clocks."""
        try:
            response = self.session.get(
                LIVE_MATCHES_URL,
                params={"sport": "football", "limit": min(50, max(1, int(limit)))},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"SportScore canlı maç bağlantı hatası: {exc}") from exc
        if response.status_code == 429:
            raise ProviderError("SportScore canlı maç istek sınırı doldu.")
        if response.status_code in (401, 403):
            raise ProviderError("SportScore canlı maç erişimi reddedildi.")
        if not response.ok:
            raise ProviderError(f"SportScore canlı maç HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("SportScore canlı maç yanıtı geçerli JSON değil.") from exc
        return [
            fixture for fixture in self.parse_fixtures_payload(payload)
            if fixture.get("status") == "live"
        ]

    def team_fixtures_for_team(self, team_name, limit=30):
        """Fetch up to 30 recent/upcoming fixtures for one named club."""
        slug = self.search_team_slug(team_name)
        if not slug:
            return []
        try:
            response = self.session.get(
                TEAM_URL,
                params={"sport": "football", "slug": slug, "limit": min(30, max(1, int(limit)))},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"{team_name} fikstür bağlantı hatası: {exc}") from exc
        if response.status_code == 429:
            raise ProviderError("SportScore istek sınırı doldu.")
        if response.status_code in (401, 403):
            raise ProviderError("SportScore takım fikstürü erişimi reddedildi.")
        if not response.ok:
            raise ProviderError(f"SportScore takım fikstürü HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("SportScore takım fikstürü geçerli JSON döndürmedi.") from exc
        return self.parse_fixtures_payload(payload)
