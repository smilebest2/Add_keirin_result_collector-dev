import argparse
import json

from .db import connect, init_db
from .prediction import (
    STAKE_AMOUNT,
    TRIFECTA,
    TYPE_FEATURE_3RENTAN,
    TYPE_FEATURE_BOX_3RENTAN,
    TYPE_FEATURE_LINE_MIX,
    TYPE_ANA_LINE_MIX,
    TYPE_ANA_PICKUP,
    TYPE_LINE_BREAK,
    TYPE_LINE_BREAK_PICKUP,
    TYPE_HONMEI,
    evaluate_prediction_bets,
    evaluate_predictions,
)


COMPARE_TYPES = [
    ("old_model", TYPE_HONMEI),
    (TYPE_FEATURE_3RENTAN, TYPE_FEATURE_3RENTAN),
    (TYPE_FEATURE_BOX_3RENTAN, TYPE_FEATURE_BOX_3RENTAN),
    (TYPE_FEATURE_LINE_MIX, TYPE_FEATURE_LINE_MIX),
    (TYPE_ANA_LINE_MIX, TYPE_ANA_LINE_MIX),
    (TYPE_LINE_BREAK, TYPE_LINE_BREAK),
    (TYPE_ANA_PICKUP, TYPE_ANA_PICKUP),
    (TYPE_LINE_BREAK_PICKUP, TYPE_LINE_BREAK_PICKUP),
]


def rows(conn, sql: str, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def race_filter_sql(start_date: str | None, end_date: str | None) -> tuple[str, list[str]]:
    filters = [
        """
        p.race_id IN (
            SELECT DISTINCT race_id
            FROM race_entry_features
        )
        """
    ]
    params = []
    if start_date:
        filters.append("p.race_date >= ?")
        params.append(start_date)
    if end_date:
        filters.append("p.race_date <= ?")
        params.append(end_date)
    return " AND ".join(filters), params


def prediction_summary(conn, label: str, prediction_type: str, start_date: str | None, end_date: str | None) -> dict:
    filter_sql, params = race_filter_sql(start_date, end_date)
    result_rows = rows(
        conn,
        f"""
        SELECT
            p.race_id,
            p.predicted_1st,
            p.predicted_2nd,
            p.predicted_3rd,
            p.stake_amount
        FROM race_prediction p
        WHERE p.prediction_type = ?
          AND {filter_sql}
        """,
        [prediction_type, *params],
    )
    actual_by_race: dict[str, list[int]] = {}
    for row in rows(
        conn,
        """
        SELECT race_id, rank, car_no
        FROM race_result
        WHERE rank IN (1, 2, 3)
        ORDER BY race_id, rank, car_no
        """,
    ):
        actual_by_race.setdefault(row["race_id"], []).append(int(row["car_no"]))
    evaluated = [
        row for row in result_rows
        if len(set(actual_by_race.get(row["race_id"], []))) >= 3
    ]
    count = len(evaluated)
    if not count:
        return {
            "prediction_type": label,
            "races": 0,
            "first_rate": 0,
            "exact_rate": 0,
            "avg_top3_count": 0,
            "top3_set_rate": 0,
            "roi": 0,
        }
    stake = sum(int(row.get("stake_amount") or STAKE_AMOUNT) for row in evaluated)
    returns = 0
    first_hits = 0
    exact_hits = 0
    top3_count = 0
    top3_set_hits = 0
    for row in evaluated:
        predicted = {int(row["predicted_1st"]), int(row["predicted_2nd"]), int(row["predicted_3rd"])}
        actual_ordered = actual_by_race.get(row["race_id"], [])[:3]
        actual = set(actual_ordered)
        predicted_ordered = [int(row["predicted_1st"]), int(row["predicted_2nd"]), int(row["predicted_3rd"])]
        first_hits += 1 if predicted_ordered[0] == actual_ordered[0] else 0
        top3_count += len(predicted & actual)
        top3_set_hits += 1 if predicted == actual else 0
        combination = "-".join(str(item) for item in predicted_ordered)
        payout = conn.execute(
            """
            SELECT payout
            FROM payout
            WHERE race_id = ? AND bet_type = ? AND combination = ?
            LIMIT 1
            """,
            (row["race_id"], TRIFECTA, combination),
        ).fetchone()
        if payout:
            exact_hits += 1
            returns += int(payout["payout"] or 0)
    return {
        "prediction_type": label,
        "races": count,
        "first_rate": round(first_hits * 100 / count, 3),
        "exact_rate": round(exact_hits * 100 / count, 3),
        "avg_top3_count": round(top3_count / count, 3),
        "top3_set_rate": round(top3_set_hits * 100 / count, 3),
        "roi": round(returns * 100 / stake, 3) if stake else 0,
    }


def box_summary(conn, start_date: str | None, end_date: str | None) -> dict:
    filters = ["b.prediction_type = ?", "b.bet_type = ?"]
    params = [TYPE_FEATURE_BOX_3RENTAN, TRIFECTA]
    if start_date:
        filters.append("b.race_date >= ?")
        params.append(start_date)
    if end_date:
        filters.append("b.race_date <= ?")
        params.append(end_date)
    bet_rows = rows(
        conn,
        f"""
        SELECT
            b.race_id,
            b.stake_amount,
            r.hit,
            r.return_amount
        FROM race_prediction_bet b
        JOIN race_prediction_bet_result r ON r.prediction_bet_id = b.id
        WHERE {" AND ".join(filters)}
          AND b.race_id IN (
              SELECT DISTINCT race_id
              FROM race_entry_features
          )
        """,
        params,
    )
    if not bet_rows:
        return {"prediction_type": TYPE_FEATURE_BOX_3RENTAN, "races": 0, "hit_rate": 0, "roi": 0}
    by_race: dict[str, list[dict]] = {}
    for row in bet_rows:
        by_race.setdefault(row["race_id"], []).append(row)
    hit_races = sum(1 for items in by_race.values() if any(row["hit"] for row in items))
    stake = sum(int(row.get("stake_amount") or STAKE_AMOUNT) for row in bet_rows)
    returns = sum(int(row.get("return_amount") or 0) for row in bet_rows)
    return {
        "prediction_type": TYPE_FEATURE_BOX_3RENTAN,
        "races": len(by_race),
        "hit_rate": round(hit_races * 100 / len(by_race), 3),
        "roi": round(returns * 100 / stake, 3) if stake else 0,
        "tickets": len(bet_rows),
    }


def compare(start_date: str | None = None, end_date: str | None = None) -> dict:
    with connect() as conn:
        init_db(conn)
        evaluate_predictions(conn)
        evaluate_prediction_bets(conn)
        prediction_rows = [
            prediction_summary(conn, label, prediction_type, start_date, end_date)
            for label, prediction_type in COMPARE_TYPES
        ]
        return {
            "start_date": start_date,
            "end_date": end_date,
            "prediction_summary": prediction_rows,
            "box_summary": box_summary(conn, start_date, end_date),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare old prediction and race_entry_features modes")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    args = parser.parse_args()
    print(json.dumps(compare(args.start_date, args.end_date), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
