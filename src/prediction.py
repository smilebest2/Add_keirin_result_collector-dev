import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from .db import connect, init_db


JST = timezone(timedelta(hours=9))
TRIFECTA = "3連単"
STAKE_AMOUNT = 100
MODEL_VERSION = "explainable-v1"

PREDICTION_TYPES = [
    "本命予想",
    "穴予想",
    "ヘテオジマーベリック予想",
    "行動ヒヒーン予想",
    "感情ブヒー予想",
]

TYPE_HONMEI = PREDICTION_TYPES[0]
TYPE_ANA = PREDICTION_TYPES[1]
TYPE_HETEOJI = PREDICTION_TYPES[2]
TYPE_KODO = PREDICTION_TYPES[3]
TYPE_KANJO = PREDICTION_TYPES[4]


def is_dev_environment() -> bool:
    env = os.environ.get("SITE_ENV") or os.environ.get("APP_ENV") or ""
    return env.lower() in {"dev", "development", "local"}


def default_target_date() -> str:
    return datetime.now(JST).date().isoformat()


def yesterday(value: str) -> str:
    return (datetime.fromisoformat(value).date() - timedelta(days=1)).isoformat()


def rows(conn, sql: str, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def scalar(conn, sql: str, params=()):
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return row[0]


def racer_history(conn, entry: dict, venue: str | None) -> dict:
    if entry.get("prefecture") and entry.get("term"):
        where = "r.racer_name = ? AND r.prefecture = ? AND r.term = ?"
        params = [entry["racer_name"], entry["prefecture"], entry["term"]]
    elif entry.get("prefecture"):
        where = "r.racer_name = ? AND r.prefecture = ?"
        params = [entry["racer_name"], entry["prefecture"]]
    else:
        return {
            "starts": 0,
            "avg_rank": None,
            "win_rate": None,
            "top2_rate": None,
            "top3_rate": None,
            "venue_starts": 0,
            "venue_top3_rate": None,
            "upset_score": 0,
            "fade_score": 0,
            "activity_score": 0,
        }

    all_stats = conn.execute(
        f"""
        SELECT COUNT(*) AS starts,
               AVG(r.rank) AS avg_rank,
               AVG(CASE WHEN r.rank = 1 THEN 1.0 ELSE 0 END) * 100 AS win_rate,
               AVG(CASE WHEN r.rank <= 2 THEN 1.0 ELSE 0 END) * 100 AS top2_rate,
               AVG(CASE WHEN r.rank <= 3 THEN 1.0 ELSE 0 END) * 100 AS top3_rate
        FROM race_result r
        WHERE {where}
        """,
        params,
    ).fetchone()
    venue_stats = conn.execute(
        f"""
        SELECT COUNT(*) AS starts,
               AVG(CASE WHEN r.rank <= 3 THEN 1.0 ELSE 0 END) * 100 AS top3_rate
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        WHERE {where} AND m.venue = ?
        """,
        [*params, venue],
    ).fetchone()
    upset_score = scalar(
        conn,
        f"""
        SELECT COALESCE(AVG(p.popularity - r.rank), 0)
        FROM race_result r
        JOIN payout p ON p.race_id = r.race_id AND p.bet_type = ?
        WHERE {where} AND p.popularity IS NOT NULL AND r.rank <= 3
        """,
        [TRIFECTA, *params],
    ) or 0
    fade_score = scalar(
        conn,
        f"""
        SELECT COALESCE(AVG(r.rank - p.popularity), 0)
        FROM race_result r
        JOIN payout p ON p.race_id = r.race_id AND p.bet_type = ?
        WHERE {where} AND p.popularity IS NOT NULL
        """,
        [TRIFECTA, *params],
    ) or 0
    return {
        "starts": all_stats["starts"] or 0,
        "avg_rank": all_stats["avg_rank"],
        "win_rate": all_stats["win_rate"],
        "top2_rate": all_stats["top2_rate"],
        "top3_rate": all_stats["top3_rate"],
        "venue_starts": venue_stats["starts"] or 0,
        "venue_top3_rate": venue_stats["top3_rate"],
        "upset_score": upset_score,
        "fade_score": fade_score,
        "activity_score": all_stats["starts"] or 0,
    }


def car_context(conn, race: dict, car_no: int, target_date: str) -> dict:
    venue = race.get("venue")
    prior_date = yesterday(target_date)
    venue_win_rate = scalar(
        conn,
        """
        SELECT AVG(CASE WHEN r.rank = 1 THEN 1.0 ELSE 0 END) * 100
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        WHERE m.venue = ? AND r.car_no = ?
        """,
        (venue, car_no),
    )
    same_venue_yesterday = scalar(
        conn,
        "SELECT COUNT(*) FROM race_master WHERE venue = ? AND race_date = ?",
        (venue, prior_date),
    ) or 0
    yesterday_top3 = scalar(
        conn,
        """
        SELECT AVG(CASE WHEN r.rank <= 3 THEN 1.0 ELSE 0 END) * 100
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        WHERE m.venue = ? AND m.race_date = ? AND r.car_no = ?
        """,
        (venue, prior_date, car_no),
    )
    return {
        "venue_win_rate": venue_win_rate or 0,
        "same_venue_yesterday": bool(same_venue_yesterday),
        "yesterday_top3": yesterday_top3 or 0,
    }


def lineup_position(conn, race_id: str, car_no: int) -> int | None:
    return scalar(
        conn,
        """
        SELECT line_position
        FROM race_lineup_forecast
        WHERE race_id = ? AND car_no = ?
        LIMIT 1
        """,
        (race_id, car_no),
    )


def entry_scores(conn, race: dict, target_date: str) -> list[dict]:
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
    scored = []
    for entry in entries:
        history = racer_history(conn, entry, race.get("venue"))
        context = car_context(conn, race, entry["car_no"], target_date)
        line_pos = lineup_position(conn, race["race_id"], entry["car_no"])
        line_bonus = 4 if line_pos == 1 else 2 if line_pos == 2 else 0
        recent_component = (entry.get("score") or 0)
        win_component = (entry.get("win_rate") or history.get("win_rate") or 0) * 0.32
        top2_component = (entry.get("quinella_rate") or history.get("top2_rate") or 0) * 0.18
        top3_component = (entry.get("trifecta_rate") or history.get("top3_rate") or 0) * 0.12
        venue_component = (history.get("venue_top3_rate") or 0) * 0.08
        car_component = context["venue_win_rate"] * 0.06
        yesterday_component = 0
        if context["same_venue_yesterday"]:
            yesterday_component = context["yesterday_top3"] * 0.08
        score_components = {
            "直近": recent_component,
            "勝率": win_component,
            "連対": top2_component,
            "3着内": top3_component,
            "会場": venue_component,
            "車番": car_component,
            "ライン": line_bonus,
            "前日同会場": yesterday_component,
        }
        score_value = sum(score_components.values())
        scored.append({
            **entry,
            **history,
            **context,
            "line_position": line_pos,
            "base_score": round(score_value, 3),
            "score_components": {key: round(value, 3) for key, value in score_components.items()},
        })
    return scored


def metric(row: dict, key: str) -> float:
    return float(row.get(key) or 0)


def avg_rank(row: dict) -> float:
    return float(row.get("avg_rank") or 99)


def strategy_adjustments(prediction_type: str, row: dict) -> dict[str, float]:
    win = metric(row, "win_rate")
    top2 = metric(row, "top2_rate")
    top3 = metric(row, "top3_rate")
    venue_top3 = metric(row, "venue_top3_rate")
    upset = metric(row, "upset_score")
    fade = max(metric(row, "fade_score"), 0)
    activity = metric(row, "activity_score")
    line_pos = row.get("line_position")
    line_bonus = 6 if line_pos == 1 else 3 if line_pos == 2 else 0

    if prediction_type == TYPE_HONMEI:
        return {
            "本命勝率": win * 0.25,
            "本命連対": top2 * 0.12,
            "ライン軸": line_bonus,
        }
    if prediction_type == TYPE_ANA:
        return {
            "中位上昇": venue_top3 * 0.35 + top3 * 0.25,
            "反発実績": upset * 8,
            "過剰本命抑制": -win * 0.25,
        }
    if prediction_type == TYPE_HETEOJI:
        return {
            "反人気実績": upset * 12,
            "3着内余地": top3 * 0.2 + venue_top3 * 0.15,
            "人気寄り抑制": -win * 0.3,
        }
    if prediction_type == TYPE_KODO:
        return {
            "継続出走": activity * 1.2,
            "平均着順安定": -avg_rank(row) * 4,
            "連対安定": top2 * 0.25,
        }
    return {
        "安定連対": top2 * 0.35,
        "3着内安定": top3 * 0.25,
        "ライン保険": line_bonus,
        "人気倒れ抑制": -fade * 6,
    }


def strategy_value(prediction_type: str, row: dict) -> float:
    return metric(row, "base_score") + sum(strategy_adjustments(prediction_type, row).values())


def score_detail(prediction_type: str, row: dict) -> dict:
    adjustments = strategy_adjustments(prediction_type, row)
    final_score = metric(row, "base_score") + sum(adjustments.values())
    return {
        "car_no": int(row["car_no"]),
        "racer_name": row.get("racer_name") or "",
        "base_score": round(metric(row, "base_score"), 1),
        "type_adjustment": round(sum(adjustments.values()), 1),
        "final_score": round(final_score, 1),
        "base_components": {
            key: round(value, 1)
            for key, value in (row.get("score_components") or {}).items()
            if abs(value) >= 0.1
        },
        "type_components": {
            key: round(value, 1)
            for key, value in adjustments.items()
            if abs(value) >= 0.1
        },
    }


def score_detail_text(prediction_type: str, picked: list[dict]) -> str:
    details = [score_detail(prediction_type, row) for row in picked]
    parts = []
    for item in details:
        type_text = ", ".join(f"{key}{value:+.1f}" for key, value in item["type_components"].items())
        if not type_text:
            type_text = "タイプ補正なし"
        parts.append(
            f'{item["car_no"]}号車 基礎{item["base_score"]:.1f} '
            f'補正{item["type_adjustment"]:+.1f} 最終{item["final_score"]:.1f} ({type_text})'
        )
    return " / ".join(parts)


def score_detail_json(prediction_type: str, picked: list[dict]) -> str:
    return json.dumps([score_detail(prediction_type, row) for row in picked], ensure_ascii=False)


def take_unique(*groups: list[dict]) -> list[dict]:
    picked = []
    seen = set()
    for group in groups:
        for row in group:
            car_no = int(row["car_no"])
            if car_no in seen:
                continue
            picked.append(row)
            seen.add(car_no)
            if len(picked) == 3:
                return picked
    return picked


def pick_combo(prediction_type: str, scored: list[dict]) -> tuple[list[int], float, str, str, str]:
    if len(scored) < 3:
        return [], 0, "出走表データが不足しています。", "", ""

    base_ranked = sorted(scored, key=lambda row: row["base_score"], reverse=True)
    strategy_ranked = sorted(scored, key=lambda row: strategy_value(prediction_type, row), reverse=True)

    if prediction_type == TYPE_HONMEI:
        ranked = base_ranked
        reason = "選手成績、会場傾向、車番傾向を総合して上位評価。"
    elif prediction_type == TYPE_ANA:
        ranked = take_unique(strategy_ranked[1:4], strategy_ranked, base_ranked)
        reason = "本命寄りになりすぎないよう、会場相性と3着内の余地がある中位上昇候補を重視。"
    elif prediction_type == TYPE_HETEOJI:
        ranked = take_unique(strategy_ranked, base_ranked[2:], base_ranked)
        reason = "過去に人気を覆して上位に来た傾向と、3着内へ飛び込む余地を重視。"
    elif prediction_type == TYPE_KODO:
        ranked = take_unique(strategy_ranked, base_ranked)
        reason = "出走数と継続性、安定した平均着順を重視。"
    else:
        ranked = take_unique(strategy_ranked, base_ranked)
        reason = "人気倒れ傾向を避け、統計上の安定候補を残す。"

    combo = [int(row["car_no"]) for row in ranked[:3]]
    score_value = sum(strategy_value(prediction_type, row) for row in ranked[:3])
    if not any(row.get("same_venue_yesterday") for row in scored):
        reason += " 前日同会場データなしのため、累積会場傾向と選手成績を優先。"
    else:
        reason += " 前日同会場データがあるため、直近の会場傾向を補正。"
    picked = ranked[:3]
    return (
        combo,
        round(score_value, 3),
        reason,
        score_detail_text(prediction_type, picked),
        score_detail_json(prediction_type, picked),
    )


def confidence(score_value: float, has_same_venue_yesterday: bool) -> str:
    if score_value >= 300 and has_same_venue_yesterday:
        return "A"
    if score_value >= 220:
        return "B"
    return "C"


def clear_date_predictions(conn, target_date: str) -> None:
    prediction_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM race_prediction WHERE race_date = ?",
            (target_date,),
        ).fetchall()
    ]
    if prediction_ids:
        conn.executemany("DELETE FROM race_prediction_result WHERE prediction_id = ?", [(item,) for item in prediction_ids])
    conn.execute("DELETE FROM race_prediction WHERE race_date = ?", (target_date,))


def clear_analysis_details_if_needed(conn) -> None:
    if is_dev_environment():
        return
    conn.execute(
        """
        UPDATE race_prediction
        SET score_detail_text = NULL,
            score_detail_json = NULL
        WHERE score_detail_text IS NOT NULL
           OR score_detail_json IS NOT NULL
        """
    )


def generate_predictions(conn, target_date: str) -> int:
    include_analysis_detail = is_dev_environment()
    races = rows(
        conn,
        """
        SELECT *
        FROM race_schedule
        WHERE race_date = ?
        ORDER BY venue, race_no
        """,
        (target_date,),
    )
    clear_date_predictions(conn, target_date)
    saved = 0
    for prediction_type in PREDICTION_TYPES:
        candidates = []
        for race in races:
            scored = entry_scores(conn, race, target_date)
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
                        stake_amount, created_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    race["race_id"],
                    target_date,
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
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            saved += 1
    conn.commit()
    return saved


def evaluate_predictions(conn) -> int:
    predictions = rows(
        conn,
        """
        SELECT p.*
        FROM race_prediction p
        JOIN race_master m ON m.race_id = p.race_id
        LEFT JOIN race_prediction_result pr ON pr.prediction_id = p.id
        WHERE pr.id IS NULL
           OR (
                pr.payout IS NULL
                AND EXISTS (
                    SELECT 1
                    FROM payout pay
                    WHERE pay.race_id = p.race_id
                      AND pay.bet_type = ?
                )
           )
        """,
        (TRIFECTA,),
    )
    checked = 0
    for prediction in predictions:
        actual_rows = rows(
            conn,
            """
            SELECT rank, car_no
            FROM race_result
            WHERE race_id = ? AND rank IN (1, 2, 3)
            ORDER BY rank
            """,
            (prediction["race_id"],),
        )
        if len(actual_rows) < 3:
            continue
        actual = [int(row["car_no"]) for row in actual_rows]
        predicted = [int(prediction["predicted_1st"]), int(prediction["predicted_2nd"]), int(prediction["predicted_3rd"])]
        exact = predicted == actual
        payout = scalar(
            conn,
            """
            SELECT payout
            FROM payout
            WHERE race_id = ? AND bet_type = ? AND combination = ?
            LIMIT 1
            """,
            (prediction["race_id"], TRIFECTA, "-".join(str(item) for item in actual)),
        )
        if payout is None:
            payout = scalar(
                conn,
                "SELECT payout FROM payout WHERE race_id = ? AND bet_type = ? LIMIT 1",
                (prediction["race_id"], TRIFECTA),
            )
        return_amount = int(payout or 0) if exact else 0
        stake = int(prediction["stake_amount"] or STAKE_AMOUNT)
        roi = (return_amount / stake * 100) if stake else 0
        conn.execute(
            """
            INSERT OR REPLACE INTO race_prediction_result
                (
                    prediction_id, race_id, actual_1st, actual_2nd, actual_3rd,
                    hit_exact, hit_1st, hit_top2, hit_top3_count, payout,
                    stake_amount, return_amount, roi, checked_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prediction["id"],
                prediction["race_id"],
                actual[0],
                actual[1],
                actual[2],
                1 if exact else 0,
                1 if predicted[0] == actual[0] else 0,
                1 if set(predicted[:2]) == set(actual[:2]) else 0,
                len(set(predicted) & set(actual)),
                payout,
                stake,
                return_amount,
                roi,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        checked += 1
    conn.commit()
    return checked


def run(target_date: str | None = None) -> dict:
    target_date = target_date or default_target_date()
    with connect() as conn:
        init_db(conn)
        checked_before = evaluate_predictions(conn)
        saved = generate_predictions(conn, target_date)
        checked_after = evaluate_predictions(conn)
        clear_analysis_details_if_needed(conn)
        conn.commit()
    return {"date": target_date, "predictions": saved, "checked": checked_before + checked_after}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and evaluate keirin predictions")
    parser.add_argument("--date", help="Target date in YYYY-MM-DD. Default: today")
    args = parser.parse_args()
    result = run(args.date)
    print(result)


if __name__ == "__main__":
    main()
