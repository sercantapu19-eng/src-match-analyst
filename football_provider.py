import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE_URL = "https://sportscore.com/api/v1/fixtures/"
SEARCH_URL = "https://sportscore.com/api/v1/search/"
TEAM_URL = "https://sportscore.com/api/widget/team/"
LIVE_MATCHES_URL = "https://sportscore.com/api/widget/matches/"
MATCH_DETAIL_URL = "https://sportscore.com/api/widget/match/"
_TEAM_NAME_LOOKUP_CACHE = {}

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
        """Read elapsed match time from SportScore fixture/widget payload variants."""
        import re

        def normalize(value):
            if isinstance(value, dict):
                minute = FootballProvider.pick(
                    value, "elapsed", "elapsed_minute", "elapsed_minutes",
                    "elapsed_time", "elapsedTime", "match_minute", "matchMinute",
                    "minute", "minutes", "current_minute", "currentMinute",
                    "match_time", "matchTime", "value", "current", "timer", "clock"
                )
                extra = FootballProvider.pick(value, "extra", "added", "injury_time", "stoppage")
                if minute is None:
                    # Widget feeds sometimes expose a display string instead of
                    # a numeric elapsed field.
                    minute = FootballProvider.pick(value, "display", "display_time", "displayTime", "text", "label")
                    if minute is None:
                        return None
                    return normalize(minute)
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
            # Accept labels such as "Live 37'", "1st Half · 37:18",
            # "45+2" and "37 min", while requiring an explicit time marker.
            patterns = (
                r"(?:live|in[ -]?play|1st\s+half|first\s+half|2nd\s+half|second\s+half|minute|dakika)[^0-9]{0,12}(\d{1,3})(?:\s*\+\s*(\d{1,2}))?\s*(?:['′’]|m|min|mins|minute|minutes)?",
                r"(?<!\d)(\d{1,3})\s*\+\s*(\d{1,2})(?!\d)",
                r"^(\d{1,3})\s*(?:['′’]|m|min|mins|minute|minutes)$",
                r"^(\d{1,3}):(\d{2})$"
            )
            for index, pattern in enumerate(patterns):
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match:
                    if index in (0, 1) and match.group(2):
                        return f"{match.group(1)}+{match.group(2)}"
                    return int(match.group(1))
            return None

        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        time_obj = item.get("time") if isinstance(item.get("time"), dict) else {}
        timer_obj = item.get("timer") if isinstance(item.get("timer"), dict) else {}
        candidates = [
            item.get(key) for key in (
                "elapsed", "elapsed_minute", "elapsed_minutes", "elapsed_time",
                "elapsedTime", "minute", "minutes", "match_minute", "matchMinute",
                "match_time", "matchTime", "current_minute", "currentMinute",
                "timer", "clock", "time_elapsed", "timeElapsed", "status_text",
                "time_text", "minute_text", "display_time", "displayTime"
            )
        ]
        candidates.extend(
            time_obj.get(key) for key in (
                "elapsed", "elapsed_minute", "elapsed_minutes", "minute",
                "minutes", "timer", "clock", "match_time", "matchTime",
                "display", "text"
            )
        )
        candidates.extend(
            status.get(key) for key in (
                "elapsed", "elapsed_minute", "elapsed_minutes", "minute",
                "minutes", "timer", "clock", "elapsed_time", "elapsedTime",
                "match_time", "matchTime", "status_text", "text", "label"
            )
        )
        candidates.extend(
            timer_obj.get(key) for key in (
                "elapsed", "minute", "minutes", "value", "current",
                "display", "text", "clock"
            )
        )
        for candidate in candidates:
            value = normalize(candidate)
            if value is not None:
                return value
        return None

    @staticmethod
    def is_placeholder_team_name(value):
        """Identify provider-generated team labels that contain only an opaque ID."""
        import re

        text = str(value or "").strip()
        if not text:
            return False
        if text.casefold() in {"tea", "tbd", "tba", "unknown", "unknown team", "team unknown",
                               "team not found", "to be announced", "n/a"}:
            return True
        return bool(re.fullmatch(r"(?i)team[\s:_-]+[a-z0-9][a-z0-9_-]{7,}", text))

    @staticmethod
    def team_name(value, fallback):
        """Read common team display-name fields, preferring a usable name over a placeholder."""
        if isinstance(value, dict):
            keys = (
                "display_name", "displayName", "full_name", "fullName",
                "official_name", "officialName", "localized_name", "localizedName",
                "name", "team_name", "teamName", "short_name", "shortName",
            )
            candidates = [value.get(key) for key in keys
                          if value.get(key) not in (None, "")]
            for candidate in candidates:
                if not FootballProvider.is_placeholder_team_name(candidate):
                    return str(candidate).strip()
            if candidates:
                return str(candidates[0]).strip()
            nested = value.get("team")
            if isinstance(nested, dict):
                return FootballProvider.team_name(nested, fallback)
            return fallback
        return str(value).strip() if value not in (None, "") else fallback

    @staticmethod
    def team_reference(value):
        """Extract a stable provider team slug/ID from a team object or placeholder label."""
        import re

        if isinstance(value, dict):
            nested = value.get("team")
            if isinstance(nested, dict):
                reference = FootballProvider.team_reference(nested)
                if reference:
                    return reference
            for key in ("slug", "team_slug", "teamSlug", "team_id", "teamId",
                        "id", "external_id", "externalId", "uuid", "identifier"):
                candidate = value.get(key)
                if candidate not in (None, ""):
                    return str(candidate).strip()
            for key in ("name", "display_name", "displayName", "team_name", "teamName"):
                reference = FootballProvider.team_reference(value.get(key))
                if reference:
                    return reference
        elif isinstance(value, str):
            match = re.fullmatch(r"(?i)\s*team[\s:_-]+([a-z0-9][a-z0-9_-]{7,})\s*", value)
            if match:
                return match.group(1)
        return None

    def resolve_team_name(self, reference):
        """Resolve a provider ID only when search returns a unique exact ID/slug match."""
        import time

        reference = str(reference or "").strip()
        if not reference:
            return None
        cached = _TEAM_NAME_LOOKUP_CACHE.get(reference)
        now = time.time()
        if cached and cached[0] > now:
            return cached[1]

        candidates = set()
        try:
            response = self.session.get(
                SEARCH_URL,
                params={"sport": "football", "q": reference, "limit": 20},
                timeout=3,
            )
            if response.ok:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None

                def walk(value):
                    if isinstance(value, dict):
                        identifiers = {
                            str(value.get(key)).strip()
                            for key in ("id", "team_id", "teamId", "slug", "team_slug",
                                        "teamSlug", "external_id", "externalId", "uuid")
                            if value.get(key) not in (None, "")
                        }
                        name = FootballProvider.team_name(value, "")
                        if reference in identifiers and name and not FootballProvider.is_placeholder_team_name(name):
                            candidates.add(name)
                        for child in value.values():
                            if isinstance(child, (dict, list)):
                                walk(child)
                    elif isinstance(value, list):
                        for child in value:
                            if isinstance(child, (dict, list)):
                                walk(child)

                walk(payload)
        except Exception:
            # When lookup is unavailable, the caller keeps a safe placeholder label.
            pass

        resolved = next(iter(candidates)) if len(candidates) == 1 else None
        _TEAM_NAME_LOOKUP_CACHE[reference] = (now + (86400 if resolved else 300), resolved)
        return resolved

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
        lookup_attempts = set()

        def fixture_team_name(value, fallback):
            while isinstance(value, dict) and isinstance(value.get("team"), dict):
                value = value["team"]
            name = self.team_name(value, fallback)
            if self.is_placeholder_team_name(name):
                reference = self.team_reference(value) or self.team_reference(name)
                if reference and reference not in lookup_attempts and len(lookup_attempts) < 2:
                    lookup_attempts.add(reference)
                    resolved = self.resolve_team_name(reference)
                    if resolved:
                        return resolved
            return name

        for item in rows:
            if not isinstance(item, dict):
                continue
            home = self.pick(item, "home", "home_team", "team_home", "homeTeam", default={})
            away = self.pick(item, "away", "away_team", "team_away", "awayTeam", default={})
            if isinstance(home, dict) and isinstance(home.get("team"), dict):
                home = home["team"]
            if isinstance(away, dict) and isinstance(away.get("team"), dict):
                away = away["team"]
            if not isinstance(home, dict):
                home_ref = self.pick(item, "home_team_id", "homeTeamId", "home_id", "home_team_slug", "homeTeamSlug")
                home = {"name": home, "id": home_ref} if home_ref else home
            if not isinstance(away, dict):
                away_ref = self.pick(item, "away_team_id", "awayTeamId", "away_id", "away_team_slug", "awayTeamSlug")
                away = {"name": away, "id": away_ref} if away_ref else away
            home_name = fixture_team_name(home, "Ev sahibi")
            away_name = fixture_team_name(away, "Deplasman")
            if home_name == "Ev sahibi" or away_name == "Deplasman": continue

            league_obj = self.pick(item, "competition", "league", "tournament", default={})
            league = self.team_name(league_obj, "") if isinstance(league_obj, dict) else str(league_obj or "")
            country_obj = item.get("country") or (league_obj.get("country") if isinstance(league_obj, dict) else "")
            country = self.team_name(country_obj, "") if isinstance(country_obj, dict) else str(country_obj or "")
            if country and league: league = f"{country} · {league}"

            kickoff = self.pick(item, "kickoff", "date", "start_time", "time", "datetime", default="")
            if isinstance(kickoff, dict): kickoff = self.pick(kickoff, "utc", "datetime", "date", "timestamp", default="")
            status_obj = self.pick(item, "status", "match_status", "state", default=None)
            if isinstance(status_obj, dict):
                raw_status = str(self.pick(status_obj, "long", "name", "label", "description", "status", "type", default="")).lower()
                code = str(self.pick(status_obj, "short", "code", "status_code", default=self.pick(item, "status_short", "status_code", "period", default=""))).upper()
            else:
                raw_status = str(status_obj if status_obj not in (None, "") else self.pick(item, "status_text", "state", default="upcoming")).lower()
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
                "id": self.fixture_identity(item)[0] or self.fixture_identity(item)[1],
                "slug": self.fixture_identity(item)[1],
                "home": str(home_name), "away": str(away_name), "league": league,
                "home_team_ref": self.team_reference(home), "away_team_ref": self.team_reference(away),
                "home_name_placeholder": self.is_placeholder_team_name(home_name),
                "away_name_placeholder": self.is_placeholder_team_name(away_name),
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
                name = self.team_name(value, "")
                slug = self.pick(value, "slug", "team_slug", "teamSlug", "id", "team_id")
                if name and slug and not self.is_placeholder_team_name(name):
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

    @staticmethod
    def fixture_identity(item):
        """Extract match-only identifiers from flat or nested SportScore fixture rows."""
        import re

        slug_keys = ("slug", "match_slug", "matchSlug", "fixture_slug", "fixtureSlug",
                     "event_slug", "eventSlug", "url_slug", "urlSlug")
        id_keys = ("fixture_id", "fixtureId", "match_id", "matchId", "event_id", "eventId", "id")
        containers = ("match", "fixture", "event", "game", "details", "data", "source")

        def scalar(value):
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return str(value)
            return ""

        def find(value, keys, depth=0):
            if not isinstance(value, dict) or depth > 4:
                return ""
            for key in keys:
                found = scalar(value.get(key))
                if found:
                    return found
            excluded = {"home", "away", "home_team", "away_team", "team_home", "team_away",
                        "hometeam", "awayteam", "league", "competition", "country"}
            for key, child in value.items():
                if str(key).casefold() in excluded:
                    continue
                if isinstance(child, dict):
                    found = find(child, keys, depth + 1)
                    if found:
                        return found
                elif isinstance(child, list):
                    for row in child:
                        found = find(row, keys, depth + 1)
                        if found:
                            return found
            return ""

        slug = find(item, slug_keys)
        identifier = find(item, id_keys)
        if not slug:
            # Some widget variants expose a match page path rather than a slug field.
            for key in ("match_url", "matchUrl", "fixture_url", "fixtureUrl", "url", "href", "link"):
                raw = scalar(item.get(key)) if isinstance(item, dict) else ""
                if not raw:
                    continue
                path = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")
                candidate = path.rsplit("/", 1)[-1]
                if candidate and ("-vs-" in candidate or re.search(r"\\bmatch\\b", path, re.I)):
                    slug = candidate
                    break
        return identifier, slug

    def parse_fixtures_payload(self, payload, resolve_placeholders=True, max_team_lookups=2):
        """Parse fixtures and resolve only uniquely identifiable provider placeholder teams."""
        rows = self._extract_rows(payload)
        output = []
        lookup_attempts = set()

        def unwrap_team(value):
            while isinstance(value, dict) and isinstance(value.get("team"), dict):
                value = value["team"]
            return value

        def side_name(value, fallback):
            value = unwrap_team(value)
            name = self.team_name(value, fallback)
            if not self.is_placeholder_team_name(name) or not resolve_placeholders:
                return name
            reference = self.team_reference(value) or self.team_reference(name)
            if reference and reference not in lookup_attempts and len(lookup_attempts) < max_team_lookups:
                lookup_attempts.add(reference)
                resolved = self.resolve_team_name(reference)
                if resolved:
                    return resolved
            return name

        for item in rows:
            if not isinstance(item, dict):
                continue
            home = self.pick(item, "home", "home_team", "team_home", "homeTeam", default={})
            away = self.pick(item, "away", "away_team", "team_away", "awayTeam", default={})
            home = unwrap_team(home)
            away = unwrap_team(away)
            if not isinstance(home, dict):
                home_ref = self.pick(item, "home_team_id", "homeTeamId", "home_id", "home_team_slug", "homeTeamSlug")
                home = {"name": home, "id": home_ref} if home_ref else home
            if not isinstance(away, dict):
                away_ref = self.pick(item, "away_team_id", "awayTeamId", "away_id", "away_team_slug", "awayTeamSlug")
                away = {"name": away, "id": away_ref} if away_ref else away
            home_name = side_name(home, "Ev sahibi")
            away_name = side_name(away, "Deplasman")
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
            status_obj = self.pick(item, "status", "match_status", "state", default=None)
            if isinstance(status_obj, dict):
                raw_status = str(self.pick(status_obj, "long", "name", "label", "description", "status", "type", default="")).lower()
                code = str(self.pick(status_obj, "short", "code", "status_code", default=self.pick(item, "status_short", "status_code", "period", default=""))).upper()
            else:
                raw_status = str(status_obj if status_obj not in (None, "") else self.pick(item, "status_text", "state", default="upcoming")).lower()
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
                "id": self.fixture_identity(item)[0] or self.fixture_identity(item)[1],
                "slug": self.fixture_identity(item)[1],
                "home": str(home_name), "away": str(away_name), "league": league,
                "home_team_ref": self.team_reference(home),
                "away_team_ref": self.team_reference(away),
                "home_name_placeholder": self.is_placeholder_team_name(home_name),
                "away_name_placeholder": self.is_placeholder_team_name(away_name),
                "kickoff": self.local_kickoff(kickoff), "status": state,
                "status_short": code or raw_status.upper()[:12],
                "home_goals": score_int(hg), "away_goals": score_int(ag),
                "elapsed_minute": elapsed_value,
                "ht_home": score_int(ht_home), "ht_away": score_int(ht_away)
            })
        return output

    @staticmethod
    def is_current_live_fixture(fixture, now=None, max_age_hours=4):
        """Reject stale/recent-feed rows; only a dated, currently live fixture qualifies."""
        if not isinstance(fixture, dict) or fixture.get("status") != "live":
            return False
        code = str(fixture.get("status_short") or "").strip().upper()
        if code in {"FT", "AET", "PEN", "FINISHED", "ENDED", "FULL_TIME", "FULLTIME",
                    "CANC", "CANCELLED", "PST", "POSTPONED", "ABD", "ABANDONED", "WO"}:
            return False
        kickoff = str(fixture.get("kickoff") or "").strip()
        if not kickoff:
            return False
        try:
            parsed = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            age_seconds = (current - parsed.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return False
        return -30 * 60 <= age_seconds <= max_age_hours * 60 * 60

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
            if self.is_current_live_fixture(fixture)
        ]


    @staticmethod
    def find_matching_live_fixture(fixtures, home, away, kickoff=None, allow_missing_identifier=False):
        """Prefer exact team names; optionally match identifier-less date-feed rows for slug recovery."""
        def tokens(value):
            return set(FootballProvider._normalize_search_name(value).split())
        target_home, target_away = tokens(home), tokens(away)
        if not target_home or not target_away:
            return None
        scored = []
        for fixture in fixtures or []:
            if not isinstance(fixture, dict) or fixture.get("status") != "live":
                continue
            if not allow_missing_identifier and not (fixture.get("slug") or fixture.get("id")):
                continue
            feed_home, feed_away = tokens(fixture.get("home", "")), tokens(fixture.get("away", ""))
            if not feed_home or not feed_away:
                continue
            if not (feed_home == target_home or feed_home.issubset(target_home) or target_home.issubset(feed_home)):
                continue
            if not (feed_away == target_away or feed_away.issubset(target_away) or target_away.issubset(feed_away)):
                continue
            score = int(feed_home == target_home) + int(feed_away == target_away)
            scored.append((score, fixture))
        if not scored:
            return None
        best_score = max(score for score, _ in scored)
        candidates = [fixture for score, fixture in scored if score == best_score]
        if kickoff:
            try:
                target_time = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
                if target_time.tzinfo is None:
                    target_time = target_time.replace(tzinfo=timezone.utc)
                dated = []
                for fixture in candidates:
                    value = fixture.get("kickoff")
                    if not value: continue
                    try:
                        fixture_time = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                        if fixture_time.tzinfo is None:
                            fixture_time = fixture_time.replace(tzinfo=timezone.utc)
                        delta = abs((fixture_time - target_time).total_seconds())
                        if delta <= 4 * 60 * 60: dated.append((delta, fixture))
                    except (TypeError, ValueError, OverflowError):
                        continue
                if dated:
                    dated.sort(key=lambda pair: pair[0])
                    if len(dated) == 1 or dated[0][0] < dated[1][0]:
                        return dated[0][1]
                    return None
            except (TypeError, ValueError, OverflowError):
                pass
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def parse_match_events(payload):
        """Normalize SportScore match incidents without inventing missing event facts."""
        import hashlib
        import json
        import re

        keys = {"events", "timeline", "incidents", "match_events", "matchevents"}
        rows = []

        def walk(value, depth=0):
            if depth > 7:
                return
            if isinstance(value, dict):
                for key, child in value.items():
                    if str(key).casefold() in keys and isinstance(child, list):
                        rows.extend(item for item in child if isinstance(item, dict))
                    elif isinstance(child, (dict, list)):
                        walk(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    if isinstance(child, (dict, list)):
                        walk(child, depth + 1)

        walk(payload)
        output, seen = [], set()

        def person_name(value):
            if isinstance(value, dict):
                return str(FootballProvider.pick(value, "name", "display_name", "displayName", "short_name", default="") or "")
            return str(value or "")

        for item in rows:
            raw_type = str(FootballProvider.pick(
                item, "type", "event_type", "eventType", "incident", "kind", "name", "title",
                "incident_type", "incidentType", default=""
            )).strip()
            detail = str(FootballProvider.pick(
                item, "detail", "description", "comment", "label", "sub_type", "subType",
                "reason", "outcome", default=""
            )).strip()
            value = (raw_type + " " + detail).casefold().replace("_", " ").replace("-", " ")
            compact = re.sub(r"[^a-z0-9ğüşöçıİ]+", " ", value).strip()

            cancellation_words = ("cancel", "canceled", "cancelled", "disallowed", "iptal", "overturned")
            if (("var" in compact or "goal" in compact or "gol" in compact)
                    and any(word in compact for word in cancellation_words)):
                kind, label, icon = "goal_cancelled", "Gol VAR ile iptal edildi", "📺"
            elif "corner" in compact or "korner" in compact:
                kind, label, icon = "corner", "Korner", "🚩"
            elif "offside" in compact or "ofsayt" in compact:
                kind, label, icon = "offside", "Ofsayt", "🚫"
            elif any(x in compact for x in ("substitution", "substitute", "oyuncu değiş")):
                kind, label, icon = "substitution", "Oyuncu değişikliği", "🔄"
            elif any(x in compact for x in ("red card", "redcard", "kırmızı kart")):
                kind, label, icon = "red_card", "Kırmızı kart", "🟥"
            elif any(x in compact for x in ("second yellow", "second_yellow", "ikinci sarı")):
                kind, label, icon = "second_yellow", "İkinci sarı kart", "🟨🟥"
            elif any(x in compact for x in ("yellow", "sarı kart")):
                kind, label, icon = "yellow_card", "Sarı kart", "🟨"
            elif any(x in compact for x in ("var", "cancelled", "canceled", "disallowed", "iptal")):
                cancelled = any(x in compact for x in ("cancel", "canceled", "cancelled", "disallowed", "iptal", "overturned"))
                kind, label, icon = ("goal_cancelled", "Gol VAR ile iptal edildi", "📺") if cancelled else ("var_review", "VAR incelemesi", "📺")
            elif any(x in compact for x in ("penalty missed", "missed penalty", "penalty saved", "kaçan penaltı", "kaçırılan penaltı")):
                kind, label, icon = "penalty_missed", "Kaçırılan penaltı", "❌"
            elif "penalty" in compact and not any(x in compact for x in ("scored", "goal", "converted", "missed", "saved")):
                kind, label, icon = "penalty_awarded", "Penaltı kararı", "⚽"
            elif any(x in compact for x in ("own goal", "autogol", "kendi kalesine")):
                kind, label, icon = "own_goal", "Kendi kalesine gol", "⚽"
            elif "penalty" in compact and any(x in compact for x in ("scored", "goal", "converted", "gol")):
                kind, label, icon = "penalty_scored", "Penaltı golü", "⚽"
            elif any(x in compact for x in ("half time", "halftime", "devre arası")):
                kind, label, icon = "halftime", "İlk yarı sona erdi", "⏸️"
            elif any(x in compact for x in ("second half", "2nd half", "ikinci yarı başladı")):
                kind, label, icon = "second_half", "İkinci yarı başladı", "▶️"
            elif any(x in compact for x in ("full time", "fulltime", "maç sona erdi")):
                kind, label, icon = "fulltime", "Maç sona erdi", "🏁"
            elif any(x in compact for x in ("first half", "1st half", "maç başladı")):
                kind, label, icon = "kickoff", "Maç başladı", "🕐"
            elif "goal" in compact or "gol" in compact:
                kind, label, icon = "goal", "Gol", "⚽"
            else:
                continue

            raw_minute = FootballProvider.pick(
                item, "minute", "time", "elapsed", "elapsed_minute", "elapsedTime",
                "time_elapsed", "display", "minute_label", default=""
            )
            minute_value = FootballProvider.elapsed_minute({"elapsed": raw_minute}) if raw_minute not in (None, "") else ""
            if minute_value is None:
                minute_value = str(raw_minute) if isinstance(raw_minute, (int, float, str)) else ""
            minute_text = str(minute_value or "").strip()
            base_minute, extra_minute = None, None
            minute_match = re.fullmatch(r"(\d{1,3})(?:\s*\+\s*(\d{1,2}))?", minute_text)
            if minute_match:
                base_minute = int(minute_match.group(1))
                extra_minute = int(minute_match.group(2)) if minute_match.group(2) else None
            extra = FootballProvider.pick(item, "extra_minute", "added_time", "injury_time", "stoppage_time")
            if extra not in (None, "", 0, "0"):
                try:
                    extra_minute = int(extra)
                except (TypeError, ValueError):
                    pass
            if base_minute is not None and extra_minute is not None:
                minute_text = f"{base_minute}+{extra_minute}"
            elif base_minute is not None:
                minute_text = str(base_minute)

            player = person_name(FootballProvider.pick(item, "player", "player_name", "playerName", "scorer", "athlete", default=""))
            assist = person_name(FootballProvider.pick(item, "assist", "assist_player", "assistName", "assisted_by", default=""))
            team_value = FootballProvider.pick(item, "team", "team_name", "teamName", "team_side", "side", default="")
            team = person_name(team_value)
            incoming = person_name(FootballProvider.pick(item, "player_in", "playerIn", "in_player", "sub_in", "incoming_player", default=""))
            outgoing = person_name(FootballProvider.pick(item, "player_out", "playerOut", "out_player", "sub_out", "outgoing_player", default=""))
            if kind == "substitution":
                detail_text = "↓ " + (outgoing or "Bilinmiyor") + "  ↑ " + (incoming or "Bilinmiyor")
            elif kind in ("var_review", "goal_cancelled"):
                detail_text = detail or player
            else:
                detail_text = player + (f" · Asist: {assist}" if assist else "") if player else detail

            provider_id = FootballProvider.pick(
                item, "id", "event_id", "eventId", "incident_id", "incidentId", "uuid", "uid"
            )
            related_id = FootballProvider.pick(
                item, "related_event_id", "relatedEventId", "parent_event_id", "parentEventId",
                "goal_event_id", "goalEventId"
            )
            event_status = "reviewing" if kind == "var_review" else "confirmed"
            team_side = str(FootballProvider.pick(item, "team_side", "side", default="") or "").casefold()
            if team_side not in ("home", "away"):
                team_side = ""
            canonical = {
                "kind": kind, "minute": minute_text, "team": team, "player": player,
                "incoming": incoming, "outgoing": outgoing, "detail": detail_text,
            }
            if provider_id not in (None, ""):
                event_uid = "sportscore:" + str(provider_id).strip()
            else:
                fingerprint = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                event_uid = "fallback:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:40]
            if event_uid in seen:
                continue
            seen.add(event_uid)
            sort_key = (base_minute * 100 + (extra_minute or 0)) if base_minute is not None else -1
            counts_for_score = kind in ("goal", "own_goal", "penalty_scored") and event_status == "confirmed"
            raw_meta = {
                "provider_id": str(provider_id) if provider_id not in (None, "") else None,
                "related_event_id": str(related_id) if related_id not in (None, "") else None,
                "raw_type": raw_type,
                "raw_detail": detail,
            }
            output.append({
                "event_uid": event_uid,
                "related_event_uid": (str(related_id).strip() if str(related_id).strip().startswith("sportscore:") else "sportscore:" + str(related_id).strip()) if related_id not in (None, "") else "",
                "event_status": event_status,
                "minute": minute_text,
                "extra_minute": extra_minute,
                "sort_key": sort_key,
                "minute_label": f"{minute_text}′" if minute_text else "—",
                "kind": kind,
                "label": label,
                "icon": icon,
                "detail": detail_text,
                "team": team,
                "team_side": team_side,
                "primary_player": player or incoming,
                "secondary_player": assist or outgoing,
                "counts_for_score": counts_for_score,
                "raw_meta": raw_meta,
            })

        return sorted(output, key=lambda event: (event["sort_key"], event["event_uid"]))

    @staticmethod
    def _match_team_slug(value):
        """Create a conservative URL slug from a team name."""
        return "-".join(FootballProvider._normalize_search_name(value).split())

    def match_slug_candidates(self, home, away):
        """Build candidate match slugs from verified team slugs and normalized names.

        Candidates are only hints: callers must validate the returned match detail
        against both team names before using any timeline data.
        """
        import re

        candidates = []
        home_ref = self.search_team_slug(home)
        away_ref = self.search_team_slug(away)
        slug_pattern = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$", re.I)
        if (home_ref and away_ref and slug_pattern.fullmatch(str(home_ref))
                and slug_pattern.fullmatch(str(away_ref))
                and not str(home_ref).isdigit() and not str(away_ref).isdigit()):
            candidates.append(f"{home_ref}-vs-{away_ref}")

        home_name_slug = self._match_team_slug(home)
        away_name_slug = self._match_team_slug(away)
        if home_name_slug and away_name_slug:
            candidates.append(f"{home_name_slug}-vs-{away_name_slug}")
        return list(dict.fromkeys(candidates))[:2]

    @staticmethod
    def match_detail_matches_teams(payload, home, away):
        """Accept detail data only when it explicitly identifies the requested home/away pair."""
        target_home = set(FootballProvider._normalize_search_name(home).split())
        target_away = set(FootballProvider._normalize_search_name(away).split())
        if not target_home or not target_away:
            return False

        def same_team(actual, target):
            actual_tokens = set(FootballProvider._normalize_search_name(
                FootballProvider.team_name(actual, "") if isinstance(actual, dict) else actual
            ).split())
            return bool(actual_tokens and actual_tokens == target)

        def walk(value, depth=0):
            if depth > 8:
                return False
            if isinstance(value, dict):
                home_value = FootballProvider.pick(value, "home", "home_team", "team_home", "homeTeam")
                away_value = FootballProvider.pick(value, "away", "away_team", "team_away", "awayTeam")
                if home_value is not None and away_value is not None:
                    if same_team(home_value, target_home) and same_team(away_value, target_away):
                        return True
                return any(walk(child, depth + 1) for child in value.values()
                           if isinstance(child, (dict, list)))
            if isinstance(value, list):
                return any(walk(child, depth + 1) for child in value
                           if isinstance(child, (dict, list)))
            return False

        return walk(payload)

    def match_detail(self, slug):
        """Fetch one match's details and official event timeline."""
        if not slug: return {}
        try:
            response=self.session.get(MATCH_DETAIL_URL,params={"sport":"football","slug":str(slug)},timeout=10)
        except requests.RequestException as exc:
            raise ProviderError(f"SportScore maç detay bağlantı hatası: {exc}") from exc
        if response.status_code==429: raise ProviderError("SportScore maç detay istek sınırı doldu.")
        if response.status_code in (401,403): raise ProviderError("SportScore maç detay erişimi reddedildi.")
        if not response.ok: raise ProviderError(f"SportScore maç detay HTTP {response.status_code}.")
        try: payload=response.json()
        except ValueError as exc: raise ProviderError("SportScore maç detay yanıtı geçerli JSON değil.") from exc
        return payload if isinstance(payload,dict) else {}

    def search_team_logo(self, team_name):
        """Return a verified exact-name club crest URL from SportScore search, if supplied."""
        if self.is_placeholder_team_name(team_name):
            return None
        try:
            response = self.session.get(
                SEARCH_URL,
                params={"sport": "football", "q": team_name, "limit": 20},
                timeout=8,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"SportScore takım arama bağlantı hatası: {exc}") from exc
        if response.status_code == 429:
            raise ProviderError("SportScore istek sınırı doldu.")
        if not response.ok:
            raise ProviderError(f"SportScore takım arama HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("SportScore takım araması geçerli JSON döndürmedi.") from exc
        target = self._normalize_search_name(team_name)
        matches = []
        def walk(value):
            if isinstance(value, dict):
                name = self.pick(value, "name", "team_name", "short_name", "display_name")
                logo = self.pick(value, "logo", "logo_url", "image", "image_url", "crest", "badge", "team_logo")
                if name and logo and self._normalize_search_name(name) == target:
                    candidate = str(logo).strip()
                    if candidate.startswith("https://"):
                        matches.append(candidate)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(payload)
        unique = list(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else None

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
