"""Pure helpers for comparing stored predictions with verified full-time scores."""

def _score(value):
    try:
        if value is None or str(value).strip() == "":
            return None
        parsed = int(value)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError):
        return None


def actual_market_values(match):
    """Return only markets supported by the provider's verified match data."""
    home = _score(match.get("home_goals"))
    away = _score(match.get("away_goals"))
    if home is None or away is None:
        return {}

    result = "MS 1" if home > away else "MS 2" if home < away else "MS X"
    total = home + away
    values = {
        "match_result": result,
        "over25": "Üst 2,5" if total >= 3 else "Alt 2,5",
        "btts": "Var" if home > 0 and away > 0 else "Yok",
    }

    ht_home = _score(match.get("ht_home"))
    ht_away = _score(match.get("ht_away"))
    if ht_home is not None and ht_away is not None and ht_home <= home and ht_away <= away:
        ht_result = "1" if ht_home > ht_away else "2" if ht_home < ht_away else "X"
        ft_result = "1" if home > away else "2" if home < away else "X"
        values["htft"] = f"{ht_result}/{ft_result}"
        values["first_half_btts"] = "Var" if ht_home > 0 and ht_away > 0 else "Yok"
        second_home, second_away = home - ht_home, away - ht_away
        values["second_half_btts"] = "Var" if second_home > 0 and second_away > 0 else "Yok"
    return values


def evaluate_prediction(prediction, actual):
    """Return won/lost for a comparable, source-backed market selection."""
    return "won" if str(prediction or "").strip().casefold() == str(actual or "").strip().casefold() else "lost"
