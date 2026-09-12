import json
import re
from datetime import datetime, timedelta, timezone
from time import sleep
from urllib.parse import urljoin

import requests

from .config import BASE_URL, HEADERS, REQUEST_RETRY, REQUEST_TIMEOUT


JST = timezone(timedelta(hours=9))
PRELOADED_STATE_MARKER = "window.__PRELOADED_STATE__ = "
ODDS_QUERY_KEY = "FETCH_KEIRIN_RACE_ODDS"
ORDERED_BET_TYPES = {"3連単", "2車単", "枠単"}
ODDS_TABLES = {
    "trifecta": "3連単",
    "trio": "3連複",
    "exacta": "2車単",
    "quinella": "2車複",
    "quinellaPlace": "ワイド",
    "bracketExacta": "枠単",
    "bracketQuinella": "枠複",
}


class OddsFetchError(RuntimeError):
    pass


def odds_url_from_detail_url(url: str | None) -> str:
    if not url:
        raise OddsFetchError("race detail URL is empty")
    absolute_url = urljoin(BASE_URL, url)
    odds_url = re.sub(r"/(?:racecard|racedata|raceresult|odds)/", "/odds/", absolute_url, count=1)
    if odds_url == absolute_url and "/odds/" not in absolute_url:
        raise OddsFetchError(f"Unsupported WINTICKET race URL: {url}")
    return odds_url


def fetch_html(url: str, retry: int = REQUEST_RETRY) -> str:
    last_error = None
    for attempt in range(1, retry + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            sleep(attempt)
    raise OddsFetchError(f"Failed to fetch odds page: {url}") from last_error


def extract_preloaded_state(html_text: str) -> dict:
    start = html_text.find(PRELOADED_STATE_MARKER)
    if start < 0:
        raise OddsFetchError("WINTICKET preloaded state was not found")
    fragment = html_text[start + len(PRELOADED_STATE_MARKER):].lstrip()
    try:
        state, _ = json.JSONDecoder().raw_decode(fragment)
    except json.JSONDecodeError as exc:
        raise OddsFetchError("WINTICKET preloaded state could not be parsed") from exc
    if not isinstance(state, dict):
        raise OddsFetchError("WINTICKET preloaded state is not an object")
    return state


def find_odds_data(state: dict) -> dict:
    queries = state.get("tanStackQuery", {}).get("queries", [])
    for query in queries:
        query_key = query.get("queryKey")
        if ODDS_QUERY_KEY not in json.dumps(query_key, ensure_ascii=False):
            continue
        data = query.get("state", {}).get("data") or query.get("data")
        if isinstance(data, dict):
            return data
    raise OddsFetchError("Race odds data was not found in WINTICKET state")


def to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def canonical_combination(bet_type: str, cars: list[int] | tuple[int, ...] | str) -> str:
    if isinstance(cars, str):
        numbers = [int(value) for value in re.findall(r"\d+", cars)]
    else:
        numbers = [int(value) for value in cars]
    if bet_type not in ORDERED_BET_TYPES:
        numbers = sorted(numbers)
    separator = "-" if bet_type in ORDERED_BET_TYPES else "="
    return separator.join(str(value) for value in numbers)


def odds_label(item: dict, value_odds: float | None) -> str:
    min_odds = to_float(item.get("minOdds"))
    max_odds = to_float(item.get("maxOdds"))
    if min_odds and max_odds:
        return f"{min_odds:.1f}-{max_odds:.1f}"
    text = item.get("oddsStr")
    if text and text != "0.0":
        return str(text)
    return "" if value_odds is None else f"{value_odds:.1f}"


def normalize_odds_item(item: dict, bet_type: str) -> dict | None:
    cars = item.get("key") or []
    if not cars:
        return None
    try:
        car_numbers = [int(value) for value in cars]
    except (TypeError, ValueError):
        return None
    odds = to_float(item.get("odds"))
    min_odds = to_float(item.get("minOdds"))
    max_odds = to_float(item.get("maxOdds"))
    value_odds = odds if odds and odds > 0 else min_odds if min_odds and min_odds > 0 else None
    combination = canonical_combination(bet_type, car_numbers)
    return {
        "combination": combination,
        "cars": car_numbers,
        "odds": odds,
        "min_odds": min_odds,
        "max_odds": max_odds,
        "value_odds": value_odds,
        "odds_label": odds_label(item, value_odds),
        "popularity": to_int(item.get("popularityOrder")),
        "unit_price": to_int(item.get("unitPrice")),
        "payoff_unit_price": to_int(item.get("payoffUnitPrice")),
        "absent": bool(item.get("absent")),
    }


def format_unix_timestamp(value) -> str | None:
    timestamp = to_int(value)
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, JST).isoformat(timespec="seconds")


def normalize_odds_data(odds_data: dict, source_url: str) -> dict:
    bets: dict[str, dict[str, dict]] = {}
    counts: dict[str, int] = {}
    for source_key, bet_type in ODDS_TABLES.items():
        table = {}
        for raw_item in odds_data.get(source_key) or []:
            item = normalize_odds_item(raw_item, bet_type)
            if item:
                table[item["combination"]] = item
        if table:
            bets[bet_type] = table
            counts[bet_type] = len(table)
    return {
        "source_url": source_url,
        "fetched_at": datetime.now(JST).isoformat(timespec="seconds"),
        "odds_updated_at": format_unix_timestamp(odds_data.get("oddsUpdatedAt")),
        "odds_delayed": bool(odds_data.get("oddsDelayed")),
        "final_odds": bool(odds_data.get("finalOdds")),
        "is_aggregated": bool(odds_data.get("isAggregated")),
        "available_bet_types": list(bets.keys()),
        "counts": counts,
        "bets": bets,
    }


def parse_race_odds_html(html_text: str, source_url: str = "") -> dict:
    state = extract_preloaded_state(html_text)
    odds_data = find_odds_data(state)
    return normalize_odds_data(odds_data, source_url)


def fetch_race_odds(url: str) -> dict:
    odds_url = odds_url_from_detail_url(url)
    return parse_race_odds_html(fetch_html(odds_url), odds_url)
