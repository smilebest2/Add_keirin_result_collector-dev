import argparse
from datetime import datetime

from .db import connect, init_db
from .prediction import (
    FEATURE_PREDICTION_TYPES,
    JST,
    MODEL_VERSION,
    STAKE_AMOUNT,
    TYPE_ANA_LINE_MIX,
    TYPE_ANA_PICKUP,
    TYPE_LINE_BREAK,
    TYPE_LINE_BREAK_PICKUP,
    clear_analysis_details_if_needed,
    confidence,
    default_target_date,
    entry_feature_rows,
    ensure_prediction_bets,
    evaluate_prediction_bets,
    evaluate_predictions,
    feature_prediction_score,
    is_dev_environment,
    pick_combo,
    rows,
    safe_float,
)


DEFAULT_TYPES = [TYPE_ANA_LINE_MIX, TYPE_LINE_BREAK, TYPE_ANA_PICKUP, TYPE_LINE_BREAK_PICKUP]


def placeholders(items: list) -> str:
    return ",".join("?" for _ in items)


def target_dates(conn, start_date: str | None, end_date: str | None) -> list[str]:
    filters = [
        """
        EXISTS (
            SELECT 1
            FROM race_entry_features f
            WHERE f.race_id = s.race_id
        )
        """
    ]
    params = []
    if start_date:
        filters.append("s.race_date >= ?")
        params.append(start_date)
    if end_date:
        filters.append("s.race_date <= ?")
        params.append(end_date)
    return [
        row["race_date"]
        for row in rows(
            conn,
            f"""
            SELECT DISTINCT s.race_date
            FROM race_schedule s
            WHERE {" AND ".join(filters)}
            ORDER BY s.race_date
            """,
            params,
        )
    ]


def clear_prediction_types(conn, race_date: str, prediction_types: list[str]) -> None:
    if not prediction_types:
        return
    type_clause = placeholders(prediction_types)
    prediction_ids = [
        row["id"]
        for row in rows(
            conn,
            f"""
            SELECT id
            FROM race_prediction
            WHERE race_date = ?
              AND prediction_type IN ({type_clause})
            """,
            [race_date, *prediction_types],
        )
    ]
    if not prediction_ids:
        return
    prediction_clause = placeholders(prediction_ids)
    bet_ids = [
        row["id"]
        for row in rows(
            conn,
            f"""
            SELECT id
            FROM race_prediction_bet
            WHERE prediction_id IN ({prediction_clause})
            """,
            prediction_ids,
        )
    ]
    if bet_ids:
        bet_clause = placeholders(bet_ids)
        conn.execute(
            f"DELETE FROM race_prediction_bet_result WHERE prediction_bet_id IN ({bet_clause})",
            bet_ids,
        )
    conn.execute(
        f"DELETE FROM race_prediction_bet WHERE prediction_id IN ({prediction_clause})",
        prediction_ids,
    )
    conn.execute(
        f"DELETE FROM race_prediction_result WHERE prediction_id IN ({prediction_clause})",
        prediction_ids,
    )
    conn.execute(
        f"DELETE FROM race_prediction WHERE id IN ({prediction_clause})",
        prediction_ids,
    )


def feature_only_entry_scores(conn, race: dict) -> list[dict]:
    entries = rows(
        conn,
        """
        SELECT *
        FROM race_entry
        WHERE race_id = ?
        ORDER BY car_no
        """,
        (race["race_id"],),
    )
    features = entry_feature_rows(conn, race["race_id"])
    scored = []
    for entry in entries:
        feature = features.get(int(entry["car_no"]), {})
        if not feature:
            continue
        line_position = int(safe_float(feature.get("line_position")))
        if line_position == 1:
            line_bonus = 1.2
        elif line_position == 2:
            line_bonus = 2.2
        elif line_position >= 3:
            line_bonus = 0.8
        else:
            line_bonus = 0.0
        base_score = (
            safe_float(entry.get("score")) * 0.60
            + safe_float(entry.get("win_rate")) * 0.10
            + safe_float(entry.get("quinella_rate")) * 0.20
            + safe_float(entry.get("trifecta_rate")) * 0.15
            + line_bonus
        )
        top3_score = (
            base_score
            + safe_float(entry.get("trifecta_rate")) * 0.75
            + safe_float(feature.get("top3_minus_race_avg")) * 0.20
            + line_bonus * 0.50
        )
        scored.append({
            **entry,
            "line_position": line_position,
            "entry_feature": feature,
            "feature_available": 1,
            "feature_score": round(feature_prediction_score(feature), 3),
            "base_score": round(base_score, 3),
            "top3_score": round(top3_score, 3),
        })
    return scored


def save_prediction_types(conn, race_date: str, prediction_types: list[str], replace: bool = True) -> int:
    invalid_types = [item for item in prediction_types if item not in FEATURE_PREDICTION_TYPES]
    if invalid_types:
        raise ValueError(f"feature prediction type only: {invalid_types}")
    if replace:
        clear_prediction_types(conn, race_date, prediction_types)
    include_analysis_detail = is_dev_environment()
    races = rows(
        conn,
        """
        SELECT *
        FROM race_schedule
        WHERE race_date = ?
        ORDER BY venue, race_no
        """,
        (race_date,),
    )
    scored_by_race = {
        race["race_id"]: feature_only_entry_scores(conn, race)
        for race in races
    }
    sample_kind = "backtest" if race_date < default_target_date() else "live"
    saved = 0
    for prediction_type in prediction_types:
        candidates = []
        for race in races:
            scored = scored_by_race[race["race_id"]]
            combo, score_value, reason, detail, detail_json = pick_combo(prediction_type, scored)
            if len(combo) != 3:
                continue
            has_same_venue_yesterday = any(row.get("same_venue_yesterday") for row in scored)
            candidates.append({
                "race": race,
                "combo": combo,
                "score": score_value,
                "confidence": confidence(score_value, has_same_venue_yesterday),
                "reason": reason,
                "detail": detail if include_analysis_detail else "",
                "detail_json": detail_json if include_analysis_detail else "",
            })
        candidates.sort(key=lambda row: row["score"], reverse=True)
        for candidate in candidates:
            race = candidate["race"]
            combo = candidate["combo"]
            conn.execute(
                """
                INSERT INTO race_prediction
                    (
                        race_id, race_date, prediction_type, predicted_1st,
                        predicted_2nd, predicted_3rd, confidence, score,
                        reason_text, score_detail_text, score_detail_json, model_version,
                        stake_amount, sample_kind, created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    race["race_id"],
                    race_date,
                    prediction_type,
                    combo[0],
                    combo[1],
                    combo[2],
                    candidate["confidence"],
                    candidate["score"],
                    candidate["reason"],
                    candidate["detail"],
                    candidate["detail_json"],
                    MODEL_VERSION,
                    STAKE_AMOUNT,
                    sample_kind,
                    datetime.now(JST).isoformat(timespec="seconds"),
                ),
            )
            saved += 1
    conn.commit()
    return saved


def backfill(start_date: str | None = None, end_date: str | None = None, prediction_types: list[str] | None = None) -> dict:
    prediction_types = prediction_types or DEFAULT_TYPES
    with connect() as conn:
        init_db(conn)
        dates = target_dates(conn, start_date, end_date)
        saved_by_date = {}
        for race_date in dates:
            print(f"backfill {race_date}", flush=True)
            saved_by_date[race_date] = save_prediction_types(conn, race_date, prediction_types, replace=True)
            conn.commit()
        bet_saved = ensure_prediction_bets(conn)
        checked = evaluate_predictions(conn)
        bet_checked = evaluate_prediction_bets(conn)
        clear_analysis_details_if_needed(conn)
        conn.commit()
    return {
        "start_date": start_date,
        "end_date": end_date,
        "prediction_types": prediction_types,
        "dates": len(saved_by_date),
        "predictions": sum(saved_by_date.values()),
        "prediction_bets": bet_saved,
        "checked": checked,
        "bet_checked": bet_checked,
        "saved_by_date": saved_by_date,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill selected feature prediction types without rebuilding old predictions")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--type", dest="prediction_types", action="append")
    args = parser.parse_args()
    result = backfill(args.start_date, args.end_date, args.prediction_types)
    print(result)


if __name__ == "__main__":
    main()
