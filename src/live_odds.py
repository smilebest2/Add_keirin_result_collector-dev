import json
import re
from datetime import datetime, timedelta, timezone

from .config import DB_PATH
from .db import connect, init_db
from .odds import canonical_combination, fetch_race_odds, odds_url_from_detail_url


JST = timezone(timedelta(hours=9))
BUY_INDEX_BY_CHAOS = {
    "low": 1.15,
    "medium": 1.25,
    "high": 1.40,
}
BET_BASE_PROBABILITY = {
    "ワイド": 0.42,
    "2車複": 0.22,
    "2車単": 0.16,
    "3連複": 0.16,
    "3連単": 0.06,
}
BET_PROBABILITY_LIMITS = {
    "ワイド": (0.12, 0.72),
    "2車複": (0.04, 0.48),
    "2車単": (0.03, 0.36),
    "3連複": (0.03, 0.38),
    "3連単": (0.01, 0.22),
}
BET_MIN_PROBABILITY = {
    "ワイド": 0.30,
    "2車複": 0.15,
    "2車単": 0.10,
    "3連複": 0.10,
    "3連単": 0.03,
}
PREDICTION_TYPE_PRIORITY = [
    "feature_line_mix",
    "本命予想",
    "line_break",
    "ana_line_mix",
    "feature_3rentan",
    "feature_box_3rentan",
    "穴予想",
]


def rows(conn, sql: str, params=()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def row(conn, sql: str, params=()) -> dict | None:
    result = conn.execute(sql, params).fetchone()
    return dict(result) if result else None


def parse_json(value, fallback):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed if parsed is not None else fallback


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


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def prediction_combo(prediction: dict) -> str:
    values = [prediction.get("predicted_1st"), prediction.get("predicted_2nd"), prediction.get("predicted_3rd")]
    if any(value is None for value in values):
        return ""
    return "-".join(str(int(value)) for value in values)


def prediction_bet_combinations(prediction: dict) -> dict[str, list[str]]:
    values = [prediction.get("predicted_1st"), prediction.get("predicted_2nd"), prediction.get("predicted_3rd")]
    if any(value is None for value in values):
        return {}
    first, second, third = [int(value) for value in values]
    return {
        "2車複": [canonical_combination("2車複", [first, second])],
        "2車単": [canonical_combination("2車単", [first, second])],
        "ワイド": [
            canonical_combination("ワイド", pair)
            for pair in ((first, second), (first, third), (second, third))
        ],
        "3連複": [canonical_combination("3連複", [first, second, third])],
        "3連単": [canonical_combination("3連単", [first, second, third])],
    }


def race_start_status(race: dict) -> dict:
    race_date = race.get("race_date")
    start_time = race.get("start_time")
    if not race_date or not start_time:
        return {"minutes": None, "label": ""}
    try:
        start_at = datetime.strptime(f"{race_date} {start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except ValueError:
        return {"minutes": None, "label": ""}
    minutes = int((start_at - datetime.now(JST)).total_seconds() // 60)
    if minutes >= 0:
        label = f"発走まで約{minutes}分"
    else:
        label = f"発走後約{abs(minutes)}分"
    return {"minutes": minutes, "label": label, "start_at": start_at.isoformat(timespec="seconds")}


def estimate_hit_probability(recommendation: dict, features: dict) -> float:
    bet_type = recommendation.get("recommended_bet_type") or ""
    sample_count = int(recommendation.get("similar_sample_count") or 0)
    similar_hit_rate = to_float(recommendation.get("similar_hit_rate"))
    if sample_count >= 10 and similar_hit_rate is not None:
        probability = similar_hit_rate / 100
    else:
        probability = BET_BASE_PROBABILITY.get(bet_type, 0.10)

    confidence = recommendation.get("confidence") or "C"
    if confidence == "A":
        probability += 0.03
    elif confidence == "B":
        probability += 0.01
    else:
        probability -= 0.04

    chaos_level = features.get("chaos_level") or ""
    if chaos_level == "high":
        probability -= 0.08
    elif chaos_level == "medium":
        probability -= 0.03

    if sample_count and sample_count < 30:
        probability -= 0.02
    if features.get("recommendation_source") == "feature_line_mix":
        probability += 0.02

    lower, upper = BET_PROBABILITY_LIMITS.get(bet_type, (0.01, 0.60))
    return clamp(probability, lower, upper)


def normalize_combination(bet_type: str, combination: str) -> str:
    return canonical_combination(bet_type, [int(value) for value in re.findall(r"\d+", combination)])


def ticket_items(odds_snapshot: dict, bet_type: str, combinations: list[str]) -> tuple[list[dict], list[str]]:
    odds_table = odds_snapshot.get("bets", {}).get(bet_type, {})
    tickets = []
    missing = []
    for raw_combination in combinations:
        combination = normalize_combination(bet_type, str(raw_combination))
        odds_item = odds_table.get(combination)
        if not odds_item:
            missing.append(combination)
            tickets.append({
                "combination": combination,
                "odds_label": "",
                "value_odds": None,
                "popularity": None,
                "absent": True,
            })
            continue
        tickets.append({
            "combination": combination,
            "odds_label": odds_item.get("odds_label") or "",
            "value_odds": odds_item.get("value_odds"),
            "popularity": odds_item.get("popularity"),
            "absent": bool(odds_item.get("absent")),
        })
    return tickets, missing


def evaluate_recommendation_with_odds(recommendation: dict | None, odds_snapshot: dict) -> dict:
    if not recommendation:
        return {
            "status": "skip",
            "label": "見送り",
            "summary": "朝時点の推奨買い目がありません。",
            "reasons": ["通常予想の買い候補がないため、直前オッズだけでは買い候補化しません。"],
            "candidate": None,
        }

    bet_type = recommendation.get("recommended_bet_type") or "見送り"
    combinations = parse_json(recommendation.get("combinations_json"), [])
    features = parse_json(recommendation.get("feature_json"), {})
    if bet_type == "見送り" or not combinations:
        return {
            "status": "skip",
            "label": "見送り",
            "summary": recommendation.get("skip_reason") or "朝時点で見送りです。",
            "reasons": ["通常予想の運用条件を満たしていないため、オッズだけでは買い候補化しません。"],
            "candidate": {
                "bet_type": bet_type,
                "combinations": [],
                "tickets": [],
                "confidence": recommendation.get("confidence") or "C",
                "chaos_level": features.get("chaos_level") or "",
            },
        }

    tickets, missing = ticket_items(odds_snapshot, bet_type, combinations)
    available_odds = [
        float(ticket["value_odds"])
        for ticket in tickets
        if ticket.get("value_odds") is not None and not ticket.get("absent")
    ]
    probability = estimate_hit_probability(recommendation, features)
    ticket_count = max(len(combinations), 1)
    confidence = recommendation.get("confidence") or "C"
    chaos_level = features.get("chaos_level") or "unknown"
    sample_count = int(recommendation.get("similar_sample_count") or 0)
    similar_roi = to_float(recommendation.get("similar_roi"))

    candidate = {
        "bet_type": bet_type,
        "combinations": [normalize_combination(bet_type, str(item)) for item in combinations],
        "tickets": tickets,
        "ticket_count": ticket_count,
        "confidence": confidence,
        "chaos_level": chaos_level,
        "probability": probability,
        "probability_pct": probability * 100,
        "similar_sample_count": sample_count,
        "similar_hit_rate": to_float(recommendation.get("similar_hit_rate")),
        "similar_roi": similar_roi,
        "source": features.get("recommendation_source") or "",
    }

    if missing or not available_odds:
        return {
            "status": "unknown",
            "label": "判定不可",
            "summary": "対象買い目のオッズが取得できませんでした。",
            "reasons": [f"未取得の買い目: {', '.join(missing)}"] if missing else ["オッズ表に有効な倍率がありません。"],
            "candidate": candidate,
        }

    average_odds = sum(available_odds) / len(available_odds)
    minimum_odds = min(available_odds)
    break_even_odds = ticket_count / probability if probability > 0 else None
    expected_index = probability * average_odds / ticket_count
    safety_index = BUY_INDEX_BY_CHAOS.get(chaos_level, 1.25)
    value_odds_line = break_even_odds * safety_index if break_even_odds else None
    candidate.update({
        "average_odds": average_odds,
        "minimum_odds": minimum_odds,
        "break_even_odds": break_even_odds,
        "value_odds_line": value_odds_line,
        "expected_index": expected_index,
        "expected_roi": expected_index * 100,
        "safety_index": safety_index,
    })

    reasons = [
        f"推定的中率 {probability * 100:.1f}%",
        f"平均オッズ {average_odds:.1f}倍",
        f"必要オッズ {break_even_odds:.1f}倍",
        f"妙味ライン {value_odds_line:.1f}倍",
    ]
    if sample_count:
        reasons.append(f"類似実績 {sample_count}件")
    if similar_roi is not None:
        reasons.append(f"類似回収率 {similar_roi:.1f}%")
    if chaos_level and chaos_level != "unknown":
        reasons.append(f"荒れ度 {chaos_level}")

    source_ok = candidate["source"] == "feature_line_mix"
    confidence_ok = confidence in {"A", "B"}
    probability_ok = probability >= BET_MIN_PROBABILITY.get(bet_type, 0.05)
    if expected_index >= safety_index and probability_ok and confidence_ok and source_ok:
        return {
            "status": "buy",
            "label": "買い候補",
            "summary": f"直前オッズ込みの期待値目安は{expected_index * 100:.1f}%です。",
            "reasons": reasons,
            "candidate": candidate,
        }
    if expected_index >= 1.0 and probability_ok and confidence_ok:
        return {
            "status": "caution",
            "label": "少額候補",
            "summary": f"元返し以上の目安ですが、安全幅は薄めです（{expected_index * 100:.1f}%）。",
            "reasons": reasons,
            "candidate": candidate,
        }
    if break_even_odds and average_odds < break_even_odds:
        return {
            "status": "no_value",
            "label": "妙味なし",
            "summary": f"推定的中率に対して平均オッズが足りません（{expected_index * 100:.1f}%）。",
            "reasons": reasons,
            "candidate": candidate,
        }
    return {
        "status": "skip",
        "label": "見送り",
        "summary": f"オッズ妙味または運用条件が不足しています（{expected_index * 100:.1f}%）。",
        "reasons": reasons,
        "candidate": candidate,
    }


def market_top(odds_snapshot: dict, limit: int = 5) -> dict[str, list[dict]]:
    result = {}
    for bet_type in ("ワイド", "2車複", "2車単", "3連複", "3連単"):
        items = [
            item for item in odds_snapshot.get("bets", {}).get(bet_type, {}).values()
            if not item.get("absent") and item.get("value_odds") is not None
        ]
        items.sort(key=lambda item: (item.get("popularity") is None, item.get("popularity") or 9999))
        result[bet_type] = [
            {
                "combination": item.get("combination"),
                "odds_label": item.get("odds_label"),
                "value_odds": item.get("value_odds"),
                "popularity": item.get("popularity"),
            }
            for item in items[:limit]
        ]
    return result


def prediction_summaries(predictions: list[dict]) -> list[dict]:
    def sort_key(item: dict) -> tuple[int, float]:
        prediction_type = item.get("prediction_type") or ""
        try:
            priority = PREDICTION_TYPE_PRIORITY.index(prediction_type)
        except ValueError:
            priority = len(PREDICTION_TYPE_PRIORITY)
        return priority, -(float(item.get("score") or 0))

    summaries = []
    for prediction in sorted(predictions, key=sort_key)[:6]:
        summaries.append({
            "prediction_type": prediction.get("prediction_type"),
            "combo": prediction_combo(prediction),
            "confidence": prediction.get("confidence") or "C",
            "score": to_float(prediction.get("score")),
            "bets": prediction_bet_combinations(prediction),
        })
    return summaries


def recommendation_summary(recommendation: dict | None) -> dict:
    if not recommendation:
        return {
            "bet_type": "見送り",
            "combinations": [],
            "confidence": "C",
            "similar_sample_count": 0,
            "similar_hit_rate": None,
            "similar_roi": None,
            "chaos_level": "",
            "skip_reason": "推奨買い目なし",
        }
    features = parse_json(recommendation.get("feature_json"), {})
    return {
        "bet_type": recommendation.get("recommended_bet_type") or "見送り",
        "combinations": parse_json(recommendation.get("combinations_json"), []),
        "confidence": recommendation.get("confidence") or "C",
        "similar_sample_count": to_int(recommendation.get("similar_sample_count")) or 0,
        "similar_hit_rate": to_float(recommendation.get("similar_hit_rate")),
        "similar_roi": to_float(recommendation.get("similar_roi")),
        "suitability_score": to_float(recommendation.get("suitability_score")),
        "reason_text": recommendation.get("reason_text") or "",
        "skip_reason": recommendation.get("skip_reason") or "",
        "chaos_level": features.get("chaos_level") or "",
        "chaos_score": to_float(features.get("chaos_score")),
        "source": features.get("recommendation_source") or "",
    }


def live_odds_recheck(conn, race_id: str) -> dict:
    race = row(
        conn,
        """
        SELECT race_id, race_date, venue, race_no, event_name, race_title,
               race_class, start_time, deadline_time, status, detail_url
        FROM race_schedule
        WHERE race_id = ?
        """,
        (race_id,),
    )
    if not race:
        raise ValueError(f"race_id was not found: {race_id}")
    if not race.get("detail_url"):
        raise ValueError(f"race detail URL was not found: {race_id}")

    recommendation = row(conn, "SELECT * FROM race_bet_recommendation WHERE race_id = ?", (race_id,))
    predictions = rows(
        conn,
        """
        SELECT *
        FROM race_prediction
        WHERE race_id = ?
        ORDER BY prediction_type
        """,
        (race_id,),
    )
    odds_snapshot = fetch_race_odds(race["detail_url"])
    decision = evaluate_recommendation_with_odds(recommendation, odds_snapshot)
    return {
        "ok": True,
        "race": {
            **race,
            "start_status": race_start_status(race),
            "odds_url": odds_url_from_detail_url(race.get("detail_url")),
        },
        "normal_recommendation": recommendation_summary(recommendation),
        "decision": decision,
        "odds": {
            key: value
            for key, value in odds_snapshot.items()
            if key != "bets"
        },
        "market_top": market_top(odds_snapshot),
        "predictions": prediction_summaries(predictions),
    }


def live_odds_recheck_by_id(race_id: str, db_path=DB_PATH) -> dict:
    with connect(db_path) as conn:
        init_db(conn)
        return live_odds_recheck(conn, race_id)
