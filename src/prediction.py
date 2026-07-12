import argparse
from itertools import permutations
import json
import os
from datetime import datetime, timedelta, timezone

from .db import connect, init_db
from .lineup_validation import normalize_lineup
from .pair_stats import pair_context_for_entries, refresh_racer_pair_stats


JST = timezone(timedelta(hours=9))
TRIFECTA = "3連単"
STAKE_AMOUNT = 100
MODEL_VERSION = "explainable-v6"
BET_TYPES = ["2車複", "2車単", "ワイド", "3連複", "3連単"]
RECOMMENDATION_MODEL_VERSION = "bet-fit-v3"

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
TYPE_FEATURE_3RENTAN = "feature_3rentan"
TYPE_FEATURE_BOX_3RENTAN = "feature_box_3rentan"
TYPE_FEATURE_LINE_MIX = "feature_line_mix"
FEATURE_PREDICTION_TYPES = [
    TYPE_FEATURE_3RENTAN,
    TYPE_FEATURE_BOX_3RENTAN,
    TYPE_FEATURE_LINE_MIX,
]
ALL_PREDICTION_TYPES = [*PREDICTION_TYPES, *FEATURE_PREDICTION_TYPES]


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


def racer_history(conn, entry: dict, venue: str | None, target_date: str) -> dict:
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
            "recent_starts": 0,
            "recent_win_rate": None,
            "recent_top2_rate": None,
            "recent_top3_rate": None,
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
        JOIN race_master m ON m.race_id = r.race_id
        WHERE {where} AND m.race_date < ?
        """,
        [*params, target_date],
    ).fetchone()
    venue_stats = conn.execute(
        f"""
        SELECT COUNT(*) AS starts,
               AVG(CASE WHEN r.rank <= 3 THEN 1.0 ELSE 0 END) * 100 AS top3_rate
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        WHERE {where} AND m.venue = ? AND m.race_date < ?
        """,
        [*params, venue, target_date],
    ).fetchone()
    recent_stats = conn.execute(
        f"""
        SELECT COUNT(*) AS starts,
               AVG(CASE WHEN rank = 1 THEN 1.0 ELSE 0 END) * 100 AS win_rate,
               AVG(CASE WHEN rank <= 2 THEN 1.0 ELSE 0 END) * 100 AS top2_rate,
               AVG(CASE WHEN rank <= 3 THEN 1.0 ELSE 0 END) * 100 AS top3_rate
        FROM (
            SELECT r.rank
            FROM race_result r
            JOIN race_master m ON m.race_id = r.race_id
            WHERE {where} AND m.race_date < ?
            ORDER BY m.race_date DESC, m.race_no DESC
            LIMIT 10
        )
        """,
        [*params, target_date],
    ).fetchone()
    upset_score = scalar(
        conn,
        f"""
        SELECT COALESCE(AVG(p.popularity - r.rank), 0)
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        JOIN payout p ON p.race_id = r.race_id AND p.bet_type = ?
        WHERE {where} AND p.popularity IS NOT NULL AND r.rank <= 3
          AND COALESCE(m.dead_heat, 0) = 0
          AND m.race_date < ?
        """,
        [TRIFECTA, *params, target_date],
    ) or 0
    fade_score = scalar(
        conn,
        f"""
        SELECT COALESCE(AVG(r.rank - p.popularity), 0)
        FROM race_result r
        JOIN race_master m ON m.race_id = r.race_id
        JOIN payout p ON p.race_id = r.race_id AND p.bet_type = ?
        WHERE {where} AND p.popularity IS NOT NULL
          AND COALESCE(m.dead_heat, 0) = 0
          AND m.race_date < ?
        """,
        [TRIFECTA, *params, target_date],
    ) or 0
    return {
        "starts": all_stats["starts"] or 0,
        "avg_rank": all_stats["avg_rank"],
        "win_rate": all_stats["win_rate"],
        "top2_rate": all_stats["top2_rate"],
        "top3_rate": all_stats["top3_rate"],
        "venue_starts": venue_stats["starts"] or 0,
        "venue_top3_rate": venue_stats["top3_rate"],
        "recent_starts": recent_stats["starts"] or 0,
        "recent_win_rate": recent_stats["win_rate"],
        "recent_top2_rate": recent_stats["top2_rate"],
        "recent_top3_rate": recent_stats["top3_rate"],
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
        WHERE m.venue = ? AND r.car_no = ? AND m.race_date < ?
        """,
        (venue, car_no, target_date),
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


def lineup_positions(conn, race_id: str) -> dict[int, int]:
    lineup = rows(
        conn,
        """
        SELECT car_no, line_no, line_position
        FROM race_lineup_forecast
        WHERE race_id = ?
        ORDER BY line_no, line_position
        """,
        (race_id,),
    )
    entry_car_nos = {
        int(row["car_no"])
        for row in rows(
            conn,
            "SELECT car_no FROM race_entry WHERE race_id = ?",
            (race_id,),
        )
    }
    lineup = normalize_lineup(lineup, entry_car_nos)
    if not lineup:
        return {}
    return {
        int(row["car_no"]): int(row["line_position"])
        for row in lineup
    }


def lineup_position(conn, race_id: str, car_no: int) -> int | None:
    return lineup_positions(conn, race_id).get(int(car_no))


def entry_feature_rows(conn, race_id: str) -> dict[int, dict]:
    return {
        int(row["car_no"]): row
        for row in rows(
            conn,
            """
            SELECT *
            FROM race_entry_features
            WHERE race_id = ?
            """,
            (race_id,),
        )
    }


def feature_prediction_score(feature: dict | None) -> float:
    if not feature:
        return 0.0
    return (
        safe_float(feature.get("score_minus_race_avg")) * 1.5
        + safe_float(feature.get("top3_minus_race_avg")) * 1.2
        + safe_float(feature.get("win_rate_minus_race_avg")) * 1.0
        - safe_float(feature.get("race_score_rank")) * 0.8
        - safe_float(feature.get("race_top3_rank")) * 0.6
        - safe_float(feature.get("line_strength_rank")) * 0.7
        - safe_float(feature.get("score_gap_top")) * 0.5
        - safe_float(feature.get("score_gap_second")) * 0.3
    )


def safe_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


def normalized_component(value, center: float, scale: float, weight: float) -> float:
    if value is None:
        return 0.0
    normalized = (float(value) - center) / scale
    normalized = max(-5.0, min(5.0, normalized))
    return normalized * weight


def entry_tactic_features(entry: dict) -> dict:
    result_counts = [
        entry.get("first_count"),
        entry.get("second_count"),
        entry.get("third_count"),
        entry.get("outside_count"),
    ]
    if all(value is None for value in result_counts):
        return {
            "front_rate": None,
            "finish_index": None,
            "place_rate": None,
            "activity": None,
        }

    first = safe_float(entry.get("first_count"))
    second = safe_float(entry.get("second_count"))
    third = safe_float(entry.get("third_count"))
    outside = safe_float(entry.get("outside_count"))
    starts = max(1.0, first + second + third + outside)

    return {
        "front_rate": (safe_float(entry.get("escape_count")) + safe_float(entry.get("makuri_count"))) / starts * 100.0,
        "finish_index": (first * 3.0 + second * 1.5 + third * 0.6 - outside * 0.25) / starts,
        "place_rate": (first + second + third) / starts * 100.0,
        "activity": (
            safe_float(entry.get("start_count"))
            + safe_float(entry.get("home_count"))
            + safe_float(entry.get("back_count"))
        ),
    }


def entry_role_fit(entry: dict, line_pos: int | None) -> dict[str, float]:
    tactic = entry_tactic_features(entry)
    axis = (
        normalized_component(entry.get("score"), 49.5, 2.2, 5.0)
        + normalized_component(entry.get("win_rate"), 18.0, 10.0, 2.2)
        + normalized_component(entry.get("quinella_rate"), 36.0, 14.0, 1.2)
        + normalized_component(tactic["finish_index"], 1.0, 0.9, 1.7)
    )
    place = (
        normalized_component(entry.get("score"), 49.5, 2.2, 3.2)
        + normalized_component(entry.get("trifecta_rate"), 55.0, 16.0, 2.6)
        + normalized_component(tactic["place_rate"], 55.0, 16.0, 1.4)
        + normalized_component(tactic["finish_index"], 1.0, 0.9, 1.3)
    )
    front_fit = 0.0
    if line_pos == 1:
        front_fit += normalized_component(tactic["front_rate"], 30.0, 18.0, 1.3)
        front_fit += normalized_component(tactic["activity"], 5.0, 4.0, 0.8)
    elif line_pos == 2:
        front_fit += normalized_component(entry.get("quinella_rate"), 36.0, 14.0, 1.1)
        front_fit += normalized_component(entry.get("mark_count"), 3.0, 3.0, 0.7)

    return {
        "axis": axis,
        "place": place,
        "front_fit": front_fit,
        "base": axis * 1.4 + place * 0.6 + front_fit,
    }


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
    positions = lineup_positions(conn, race["race_id"])
    pair_context = pair_context_for_entries(conn, entries)
    feature_rows = entry_feature_rows(conn, race["race_id"])
    for entry in entries:
        feature = feature_rows.get(int(entry["car_no"]), {})
        feature_score = feature_prediction_score(feature)
        history = racer_history(conn, entry, race.get("venue"), target_date)
        context = car_context(conn, race, entry["car_no"], target_date)
        line_pos = positions.get(int(entry["car_no"]))
        if line_pos == 1:
            line_bonus = 1.2
        elif line_pos == 2:
            line_bonus = 2.2
        elif line_pos and line_pos >= 3:
            line_bonus = 0.8
        else:
            line_bonus = -1.0 if positions else 0
        recent_component = (entry.get("score") or 0) * 0.60
        entry_win_component = (entry.get("win_rate") or 0) * 0.10
        entry_top2_component = (entry.get("quinella_rate") or 0) * 0.20
        entry_top3_component = (entry.get("trifecta_rate") or 0) * 0.15
        history_win_component = (history.get("win_rate") or 0) * 0.35
        history_top2_component = (history.get("top2_rate") or 0) * 0.20
        history_top3_component = (history.get("top3_rate") or 0) * 0.20
        venue_component = (history.get("venue_top3_rate") or 0) * 0.01
        car_component = context["venue_win_rate"] * 0.02
        yesterday_component = 0
        if context["same_venue_yesterday"]:
            yesterday_component = context["yesterday_top3"] * 0.05
        pair = pair_context.get(int(entry["car_no"]), {})
        pair_races = int(pair.get("pair_races") or 0)
        pair_ahead_rate = pair.get("pair_ahead_rate")
        pair_top2_rate = pair.get("pair_top2_rate")
        pair_top3_rate = pair.get("pair_top3_rate")
        pair_weight = min(pair_races / 20, 1.0)
        pair_ahead_component = ((pair_ahead_rate or 50) - 50) * 0.08 * pair_weight
        pair_top2_component = ((pair_top2_rate or 0) - 25) * 0.06 * pair_weight
        pair_top3_component = ((pair_top3_rate or 0) - 40) * 0.05 * pair_weight
        role_fit = entry_role_fit(entry, line_pos)
        score_components = {
            "直近": recent_component,
            "出走表勝率": entry_win_component,
            "出走表連対": entry_top2_component,
            "出走表3着内": entry_top3_component,
            "過去勝率": history_win_component,
            "過去連対": history_top2_component,
            "過去3着内": history_top3_component,
            "会場": venue_component,
            "車番": car_component,
            "ライン": line_bonus,
            "前日同会場": yesterday_component,
            "直接対戦先着": pair_ahead_component,
            "pair連対": pair_top2_component,
            "pair3着内": pair_top3_component,
        }
        score_components["new_data_role_fit"] = role_fit["base"]
        score_value = sum(score_components.values())
        win_score = (
            score_value
            + recent_component * 0.20
            + entry_win_component * 1.20
            + history_win_component * 0.90
            + pair_ahead_component * 1.40
            + role_fit["axis"] * 2.00
            + role_fit["front_fit"]
        )
        top2_score = (
            score_value
            + entry_top2_component * 0.85
            + history_top2_component * 0.70
            + pair_top2_component * 1.70
            + pair_ahead_component * 0.70
            + role_fit["place"] * 1.30
            + role_fit["front_fit"] * 0.50
        )
        top3_score = (
            score_value
            + entry_top3_component * 0.75
            + history_top3_component * 0.70
            + pair_top3_component * 1.80
            + line_bonus * 0.50
            + role_fit["place"] * 1.50
        )
        scored.append({
            **entry,
            **history,
            **context,
            "entry_win_rate": entry.get("win_rate"),
            "entry_top2_rate": entry.get("quinella_rate"),
            "entry_top3_rate": entry.get("trifecta_rate"),
            "pair_races": pair_races,
            "pair_samples": int(pair.get("pair_samples") or 0),
            "pair_ahead_rate": pair_ahead_rate,
            "pair_top2_rate": pair_top2_rate,
            "pair_top3_rate": pair_top3_rate,
            "pair_rank_sum": pair.get("pair_rank_sum"),
            "line_position": line_pos,
            "entry_feature": feature,
            "feature_available": 1 if feature else 0,
            "feature_score": round(feature_score, 3),
            "base_score": round(score_value, 3),
            "win_score": round(win_score, 3),
            "top2_score": round(top2_score, 3),
            "top3_score": round(top3_score, 3),
            "new_data_axis_score": round(role_fit["axis"], 3),
            "new_data_place_score": round(role_fit["place"], 3),
            "new_data_role_fit": round(role_fit["base"], 3),
            "score_components": {key: round(value, 3) for key, value in score_components.items()},
        })
    return scored


def slot_ranked(scored: list[dict]) -> list[dict]:
    remaining = list(scored)
    picked = []
    for key in ("win_score", "top2_score", "top3_score"):
        if not remaining:
            break
        row = max(remaining, key=lambda item: metric(item, key))
        picked.append(row)
        remaining = [item for item in remaining if int(item["car_no"]) != int(row["car_no"])]
    if len(picked) < 3:
        picked = take_unique(picked, sorted(scored, key=lambda row: row["base_score"], reverse=True))
    return picked


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
    if line_pos == 1:
        line_bonus = 2.0
    elif line_pos == 2:
        line_bonus = 4.0
    elif line_pos and line_pos >= 3:
        line_bonus = 1.5
    else:
        line_bonus = 0

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
        "slot_scores": {
            "win_score": round(metric(row, "win_score"), 1),
            "top2_score": round(metric(row, "top2_score"), 1),
            "top3_score": round(metric(row, "top3_score"), 1),
            "new_data_axis_score": round(metric(row, "new_data_axis_score"), 1),
            "new_data_place_score": round(metric(row, "new_data_place_score"), 1),
            "new_data_role_fit": round(metric(row, "new_data_role_fit"), 1),
            "pair_races": int(row.get("pair_races") or 0),
            "pair_ahead_rate": round(metric(row, "pair_ahead_rate"), 1),
            "pair_top3_rate": round(metric(row, "pair_top3_rate"), 1),
        },
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


def feature_ranked(scored: list[dict]) -> list[dict]:
    return sorted(
        [row for row in scored if row.get("feature_available")],
        key=lambda row: (
            metric(row, "feature_score"),
            metric(row, "top3_score"),
            metric(row, "base_score"),
            -int(row["car_no"]),
        ),
        reverse=True,
    )


def feature_reason(prediction_type: str) -> str:
    if prediction_type == TYPE_FEATURE_BOX_3RENTAN:
        return "race_entry_features の feature_score 上位3名を3連単BOX候補として評価。既存予想とは別モード。"
    if prediction_type == TYPE_FEATURE_LINE_MIX:
        return "feature_score 1位を軸に、番手補正とライン強度補正を加えて2着・3着候補を選定。既存予想とは別モード。"
    return "race_entry_features の feature_score 上位3名を1着・2着・3着順に選定。既存予想とは別モード。"


def feature_detail_json(prediction_type: str, picked: list[dict]) -> str:
    details = []
    for row in picked:
        feature = row.get("entry_feature") or {}
        details.append({
            "car_no": int(row["car_no"]),
            "racer_name": row.get("racer_name") or "",
            "feature_score": round(metric(row, "feature_score"), 3),
            "score_minus_race_avg": round(safe_float(feature.get("score_minus_race_avg")), 3),
            "top3_minus_race_avg": round(safe_float(feature.get("top3_minus_race_avg")), 3),
            "win_rate_minus_race_avg": round(safe_float(feature.get("win_rate_minus_race_avg")), 3),
            "race_score_rank": int(safe_float(feature.get("race_score_rank"))),
            "race_top3_rank": int(safe_float(feature.get("race_top3_rank"))),
            "line_strength_rank": int(safe_float(feature.get("line_strength_rank"))),
            "score_gap_top": round(safe_float(feature.get("score_gap_top")), 3),
            "score_gap_second": round(safe_float(feature.get("score_gap_second")), 3),
            "line_position": int(safe_float(feature.get("line_position"))),
            "line_strength": round(safe_float(feature.get("line_strength")), 3),
        })
    return json.dumps({"prediction_type": prediction_type, "details": details}, ensure_ascii=False)


def pick_feature_combo(prediction_type: str, scored: list[dict]) -> tuple[list[int], float, str, str, str]:
    ranked = feature_ranked(scored)
    if len(ranked) < 3:
        return [], 0, "race_entry_features が不足しているため新特徴量予想を作成しません。", "", ""

    if prediction_type in {TYPE_FEATURE_3RENTAN, TYPE_FEATURE_BOX_3RENTAN}:
        picked = ranked[:3]
    else:
        first = ranked[0]
        rest = [row for row in ranked if int(row["car_no"]) != int(first["car_no"])]
        second = max(
            rest,
            key=lambda row: (
                metric(row, "feature_score")
                + safe_float((row.get("entry_feature") or {}).get("is_second")) * 8.0
                + safe_float((row.get("entry_feature") or {}).get("leader_second_win_gap")) * 0.15,
                metric(row, "top3_score"),
                -int(row["car_no"]),
            ),
        )
        rest = [row for row in rest if int(row["car_no"]) != int(second["car_no"])]
        third = max(
            rest,
            key=lambda row: (
                metric(row, "feature_score")
                - safe_float((row.get("entry_feature") or {}).get("line_strength_rank")) * 3.0
                + safe_float((row.get("entry_feature") or {}).get("line_strength")) * 0.03,
                metric(row, "top3_score"),
                -int(row["car_no"]),
            ),
        )
        picked = [first, second, third]

    combo = [int(row["car_no"]) for row in picked]
    score_value = sum(metric(row, "feature_score") for row in picked)
    return (
        combo,
        round(score_value, 3),
        feature_reason(prediction_type),
        "",
        feature_detail_json(prediction_type, picked),
    )


def pick_combo(prediction_type: str, scored: list[dict]) -> tuple[list[int], float, str, str, str]:
    if prediction_type in FEATURE_PREDICTION_TYPES:
        return pick_feature_combo(prediction_type, scored)

    if len(scored) < 3:
        return [], 0, "出走表データが不足しています。", "", ""

    base_ranked = sorted(scored, key=lambda row: row["base_score"], reverse=True)
    slot_rank = slot_ranked(scored)
    strategy_ranked = sorted(scored, key=lambda row: strategy_value(prediction_type, row), reverse=True)

    if prediction_type == TYPE_HONMEI:
        ranked = slot_rank
        reason = "1着軸、2着以内、3着残りの適性を分け、直接対戦とpair相性も加味して上位評価。"
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
    if prediction_type == TYPE_HONMEI:
        score_value = sum(
            metric(row, key)
            for row, key in zip(ranked[:3], ("win_score", "top2_score", "top3_score"))
        )
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
    if 180 <= score_value < 220:
        return "A"
    if 220 <= score_value < 300:
        return "B"
    return "C"


def bet_combinations(predicted: list[int], prediction_type: str | None = None) -> dict[str, list[str]]:
    if prediction_type == TYPE_FEATURE_BOX_3RENTAN:
        return {
            TRIFECTA: [
                "-".join(str(item) for item in combination)
                for combination in permutations(predicted, 3)
            ]
        }
    first, second, third = predicted
    top2 = "=".join(str(item) for item in sorted([first, second]))
    top3 = "=".join(str(item) for item in sorted([first, second, third]))
    wide = [
        "=".join(str(item) for item in sorted(pair))
        for pair in [(first, second), (first, third), (second, third)]
    ]
    return {
        "2車複": [top2],
        "2車単": [f"{first}-{second}"],
        "ワイド": wide,
        "3連複": [top3],
        "3連単": [f"{first}-{second}-{third}"],
    }


def lineup_context(conn, race_id: str, axis_car_no: int) -> dict:
    lineup = rows(
        conn,
        """
        SELECT car_no, line_no, line_position
        FROM race_lineup_forecast
        WHERE race_id = ?
        ORDER BY line_no, line_position
        """,
        (race_id,),
    )
    entry_car_nos = {
        int(row["car_no"])
        for row in rows(
            conn,
            "SELECT car_no FROM race_entry WHERE race_id = ?",
            (race_id,),
        )
    }
    lineup = normalize_lineup(lineup, entry_car_nos)
    if not lineup:
        return {
            "available": False,
            "line_count": None,
            "bunsen_count": None,
            "axis_followers": None,
        }
    line_sizes = {}
    for row in lineup:
        line_sizes[row["line_no"]] = line_sizes.get(row["line_no"], 0) + 1
    axis = next(
        (row for row in lineup if int(row["car_no"]) == int(axis_car_no)),
        None,
    )
    return {
        "available": True,
        "line_count": len(line_sizes),
        "bunsen_count": sum(size >= 2 for size in line_sizes.values()),
        "axis_followers": (
            line_sizes[axis["line_no"]] - axis["line_position"]
            if axis
            else None
        ),
    }


def similar_bet_stats(
    conn,
    race: dict,
    target_date: str,
    field_size: int,
    prediction_score: float,
) -> dict[str, dict]:
    historical = rows(
        conn,
        """
        SELECT p.race_id, p.predicted_1st, p.predicted_2nd,
               p.predicted_3rd, p.score, s.race_class,
               b.bet_type, b.combination,
               r.hit, r.return_amount, r.stake_amount
        FROM race_prediction p
        JOIN race_schedule s ON s.race_id = p.race_id
        JOIN race_prediction_bet b ON b.prediction_id = p.id
        JOIN race_prediction_bet_result r ON r.prediction_bet_id = b.id
        WHERE p.prediction_type = ?
          AND p.race_date < ?
          AND (
                SELECT COUNT(*)
                FROM race_entry e
                WHERE e.race_id = p.race_id
              ) = ?
          AND p.score BETWEEN ? AND ?
        """,
        (TYPE_HONMEI, target_date, field_size, prediction_score - 60, prediction_score + 60),
    )
    race_class = race.get("race_class") or ""
    same_class = [
        row for row in historical
        if race_class and row.get("race_class") == race_class
    ]
    if len({row["race_id"] for row in same_class}) >= 10:
        historical = same_class

    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in historical:
        grouped.setdefault((row["race_id"], row["bet_type"]), []).append(row)

    result = {}
    for bet_type in ("3連単", "3連複", "2車複", "ワイド"):
        samples = []
        for (race_id, historical_type), items in grouped.items():
            if historical_type != bet_type:
                continue
            if bet_type == "ワイド":
                axis = str(items[0]["predicted_1st"])
                items = [
                    item
                    for item in items
                    if axis in str(item["combination"]).split("=")
                ]
            if not items:
                continue
            samples.append({
                "race_id": race_id,
                "hit": any(item.get("hit") for item in items),
                "return_amount": sum(int(item.get("return_amount") or 0) for item in items),
                "stake_amount": sum(int(item.get("stake_amount") or 0) for item in items),
            })
        sample_count = len(samples)
        stake = sum(item["stake_amount"] for item in samples)
        result[bet_type] = {
            "sample_count": sample_count,
            "hit_rate": (
                sum(item["hit"] for item in samples) * 100 / sample_count
                if sample_count
                else None
            ),
            "roi": (
                sum(item["return_amount"] for item in samples) * 100 / stake
                if stake
                else None
            ),
        }
    return result


def chaos_level(score: float) -> str:
    if score >= 75:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def chaos_score_from_features(
    gap12: float,
    gap23: float,
    gap34: float,
    avg_top2: float,
    avg_top3: float,
    min_starts: int,
    min_recent_starts: int,
    line_info: dict,
    prediction_score: float,
) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    if gap12 < 4:
        score += 14
        reasons.append("1-2評価差が小さい")
    if gap23 < 4:
        score += 16
        reasons.append("2-3評価差が小さい")
    if gap34 < 3:
        score += 14
        reasons.append("3着候補が混戦")
    if avg_top2 < 35:
        score += 10
        reasons.append("上位の連対率が低い")
    if avg_top3 < 45:
        score += 10
        reasons.append("上位の3着内率が低い")
    if min_starts < 5:
        score += 12
        reasons.append("過去出走数が少ない")
    if min_recent_starts < 3:
        score += 10
        reasons.append("直近材料が少ない")
    if not line_info.get("available"):
        score += 18
        reasons.append("ライン情報なし")
    else:
        line_count = int(line_info.get("line_count") or 0)
        bunsen_count = int(line_info.get("bunsen_count") or 0)
        axis_followers = int(line_info.get("axis_followers") or 0)
        if line_count >= 4:
            score += 8
            reasons.append("分線が多い")
        if bunsen_count <= 1:
            score += 10
            reasons.append("主導ラインが少ない")
        if axis_followers == 0:
            score += 8
            reasons.append("軸に後続ラインなし")
    if prediction_score >= 300:
        score += 18
        reasons.append("高スコア帯の過信注意")
    return min(score, 100.0), reasons

def classify_bet_fit(
    scored: list[dict],
    line_info: dict,
    similar_stats: dict[str, dict],
) -> dict:
    ranked = sorted(scored, key=lambda row: row["base_score"], reverse=True)
    if len(ranked) < 4:
        return {
            "bet_type": "見送り",
            "combinations": [],
            "confidence": "C",
            "score": 0,
            "reason": "",
            "skip_reason": "出走選手または評価対象が4車未満です。",
            "similar": {"sample_count": 0, "hit_rate": None, "roi": None},
            "features": {"field_size": len(ranked)},
        }

    scores = [float(row["base_score"]) for row in ranked]
    cars = [int(row["car_no"]) for row in ranked]
    gap12 = scores[0] - scores[1]
    gap23 = scores[1] - scores[2]
    gap34 = scores[2] - scores[3]
    gap13 = scores[0] - scores[2]
    starts = [int(row.get("starts") or 0) for row in ranked[:3]]
    recent_starts = [int(row.get("recent_starts") or 0) for row in ranked[:3]]
    top2_rates = [float(row.get("top2_rate") or 0) for row in ranked[:3]]
    top3_rates = [float(row.get("top3_rate") or 0) for row in ranked[:3]]
    recent_top3_rates = [
        float(row.get("recent_top3_rate") or 0)
        for row in ranked[:3]
    ]
    competition_scores = [
        float(row.get("score") or 0)
        for row in ranked[:3]
        if row.get("score") is not None
    ]
    avg_top2 = sum(top2_rates) / 3
    avg_top3 = sum(top3_rates) / 3
    avg_recent_top3 = sum(recent_top3_rates) / 3
    avg_competition_score = (
        sum(competition_scores) / len(competition_scores)
        if competition_scores
        else None
    )
    min_starts = min(starts)
    min_recent_starts = min(recent_starts)
    model_top3_stability = (
        avg_top3 * 0.65
        + avg_recent_top3 * 0.35
        + min(gap34, 15) * 1.5
    )
    prediction_score = sum(scores[:3])

    line_adjustment = 0
    line_reasons = []
    if line_info.get("available"):
        line_count = int(line_info.get("line_count") or 0)
        bunsen_count = int(line_info.get("bunsen_count") or 0)
        axis_followers = int(line_info.get("axis_followers") or 0)
        if 2 <= bunsen_count <= 3:
            line_adjustment += 3
        if axis_followers >= 1:
            line_adjustment += 2
        line_reasons.append(
            f"ライン{line_count}本、分線数{bunsen_count}、軸候補の後続{axis_followers}人"
        )
    else:
        line_reasons.append("正常なライン情報なし")

    suitability = {
        "3連単": gap12 * 1.4 + gap23 * 1.0 + gap34 * 0.8 + model_top3_stability * 0.15 + line_adjustment,
        "3連複": gap34 * 2.0 + model_top3_stability * 0.35 - (gap12 + gap23) * 0.15,
        "2車複": gap23 * 1.8 + avg_top2 * 0.25 + avg_recent_top3 * 0.10 - gap12 * 0.20,
        "ワイド": gap13 * 0.8 + float(ranked[0].get("top3_rate") or 0) * 0.35 + float(ranked[0].get("recent_top3_rate") or 0) * 0.20,
    }
    chaos_score, chaos_reasons = chaos_score_from_features(
        gap12,
        gap23,
        gap34,
        avg_top2,
        avg_top3,
        min_starts,
        min_recent_starts,
        line_info,
        prediction_score,
    )
    chaos = chaos_level(chaos_score)
    suitability["3連単"] -= chaos_score * 0.35
    suitability["3連複"] -= chaos_score * 0.15
    suitability["2車複"] -= chaos_score * 0.08
    suitability["ワイド"] += max(0, chaos_score - 45) * 0.05

    minimums = {
        "3連単": 38,
        "3連複": 26,
        "2車複": 25,
        "ワイド": 24,
    }
    for bet_type, stats in similar_stats.items():
        if stats.get("sample_count", 0) >= 30:
            suitability[bet_type] += min(float(stats.get("hit_rate") or 0), 40) * 0.15
            if stats.get("roi") is not None:
                suitability[bet_type] += max(min((stats["roi"] - 70) / 10, 3), -3)

    structural_fit = {
        "3連単": (
            chaos != "high"
            and prediction_score < 300
            and gap12 >= 13.5
            and gap23 >= 4.5
            and gap34 >= 2.5
            and model_top3_stability >= 45
        ),
        "3連複": gap34 >= 5.5 and model_top3_stability >= 45,
        "2車複": gap23 >= 8 and avg_top2 >= 35,
        "ワイド": (
            float(ranked[0].get("top3_rate") or 0) * 0.65
            + float(ranked[0].get("recent_top3_rate") or 0) * 0.35
        ) >= 40,
    }
    eligible = [
        bet_type
        for bet_type in ("3連単", "3連複", "2車複", "ワイド")
        if structural_fit[bet_type] and suitability[bet_type] >= minimums[bet_type]
    ]
    data_reasons = []
    if min_starts < 5:
        data_reasons.append("上位候補の過去出走数が少ない")
    if min_recent_starts < 3:
        data_reasons.append("上位候補の直近成績が不足")
    if not line_info.get("available"):
        data_reasons.append("ライン情報を評価できない")
    if model_top3_stability < 35:
        data_reasons.append("評価上位3車の安定度が低い")
    if chaos == "high":
        data_reasons.append("荒れ度が高い")

    if not eligible or len(data_reasons) >= 2:
        return {
            "bet_type": "見送り",
            "combinations": [],
            "confidence": "C",
            "score": max(suitability.values()),
            "reason": "",
            "skip_reason": "、".join(data_reasons or ["全券種の適性が最低基準未満"]),
            "similar": {"sample_count": 0, "hit_rate": None, "roi": None},
            "features": {
                "field_size": len(ranked),
                "gap12": gap12,
                "gap23": gap23,
                "gap34": gap34,
                "gap13": gap13,
                "avg_top2": avg_top2,
                "avg_top3": avg_top3,
                "avg_recent_top3": avg_recent_top3,
                "avg_competition_score": avg_competition_score,
                "min_starts": min_starts,
                "min_recent_starts": min_recent_starts,
                "prediction_score": prediction_score,
                "chaos_score": chaos_score,
                "chaos_level": chaos,
                "chaos_reasons": chaos_reasons,
                **line_info,
            },
        }

    bet_type = eligible[0]
    similar = similar_stats.get(
        bet_type,
        {"sample_count": 0, "hit_rate": None, "roi": None},
    )
    if int(similar.get("sample_count") or 0) < 10:
        return {
            "bet_type": "見送り",
            "combinations": [],
            "confidence": "C",
            "score": suitability[bet_type],
            "reason": "",
            "skip_reason": "類似レース実績が不足",
            "similar": similar,
            "features": {
                "field_size": len(ranked),
                "gap12": gap12,
                "gap23": gap23,
                "gap34": gap34,
                "gap13": gap13,
                "avg_top2": avg_top2,
                "avg_top3": avg_top3,
                "avg_recent_top3": avg_recent_top3,
                "avg_competition_score": avg_competition_score,
                "min_starts": min_starts,
                "min_recent_starts": min_recent_starts,
                "model_top3_stability": model_top3_stability,
                "prediction_score": prediction_score,
                "chaos_score": chaos_score,
                "chaos_level": chaos,
                "chaos_reasons": chaos_reasons,
                "suitability": suitability,
                "structural_fit": structural_fit,
                **line_info,
            },
        }
    combinations = {
        "3連単": [f"{cars[0]}-{cars[1]}-{cars[2]}"],
        "3連複": ["=".join(map(str, sorted(cars[:3])))],
        "2車複": ["=".join(map(str, sorted(cars[:2])))],
        "ワイド": [
            "=".join(map(str, sorted((cars[0], cars[1])))),
            "=".join(map(str, sorted((cars[0], cars[2])))),
        ],
    }[bet_type]
    margin = suitability[bet_type] - max(
        [value for key, value in suitability.items() if key != bet_type],
        default=0,
    )
    confidence_value = suitability[bet_type] - chaos_score * 0.2
    if not line_info.get("available"):
        confidence_value -= 5
    if similar.get("sample_count", 0) < 30:
        confidence_value -= 4
    confidence_label = "A" if chaos == "low" and confidence_value >= 45 and margin >= 6 else "B" if chaos != "high" and confidence_value >= 30 else "C"
    similar_text = (
        f"類似{similar['sample_count']}レースの的中率{similar['hit_rate']:.1f}%、回収率{similar['roi']:.1f}%"
        if similar.get("sample_count", 0) >= 10
        and similar.get("hit_rate") is not None
        and similar.get("roi") is not None
        else f"類似レースは{similar.get('sample_count', 0)}件で参考不足"
    )
    reason = (
        f"評価差 1-2位{gap12:.1f}、2-3位{gap23:.1f}、3-4位{gap34:.1f}。"
        f"上位3車の過去3着内率平均{avg_top3:.1f}%、直近10走{avg_recent_top3:.1f}%。"
        f"{f'競走得点平均{avg_competition_score:.1f}。' if avg_competition_score is not None else '競走得点未取得。'}"
        f"{'、'.join(line_reasons)}。{similar_text}。"
    )
    return {
        "bet_type": bet_type,
        "combinations": combinations,
        "confidence": confidence_label,
        "score": suitability[bet_type],
        "reason": reason,
        "skip_reason": "",
        "similar": similar,
        "features": {
            "field_size": len(ranked),
            "gap12": gap12,
            "gap23": gap23,
            "gap34": gap34,
            "gap13": gap13,
            "avg_top2": avg_top2,
            "avg_top3": avg_top3,
            "avg_recent_top3": avg_recent_top3,
            "avg_competition_score": avg_competition_score,
            "min_starts": min_starts,
            "min_recent_starts": min_recent_starts,
            "model_top3_stability": model_top3_stability,
            "prediction_score": prediction_score,
            "chaos_score": chaos_score,
            "chaos_level": chaos,
            "chaos_reasons": chaos_reasons,
            "suitability": suitability,
            "structural_fit": structural_fit,
            **line_info,
        },
    }


def save_bet_recommendations(
    conn,
    races: list[dict],
    scored_by_race: dict[str, list[dict]],
    target_date: str,
) -> int:
    saved = 0
    for race in races:
        scored = scored_by_race.get(race["race_id"], [])
        if len(scored) < 3:
            continue
        base_ranked = sorted(scored, key=lambda row: row["base_score"], reverse=True)
        prediction_score = sum(float(row["base_score"]) for row in base_ranked[:3])
        line_info = lineup_context(conn, race["race_id"], int(base_ranked[0]["car_no"]))
        similar_stats = similar_bet_stats(
            conn,
            race,
            target_date,
            len(scored),
            prediction_score,
        )
        recommendation = classify_bet_fit(scored, line_info, similar_stats)
        similar = recommendation["similar"]
        conn.execute(
            """
            INSERT OR REPLACE INTO race_bet_recommendation
                (
                    race_id, race_date, recommended_bet_type,
                    combinations_json, confidence, suitability_score,
                    reason_text, skip_reason, similar_sample_count,
                    similar_hit_rate, similar_roi, feature_json,
                    model_version, created_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                race["race_id"],
                target_date,
                recommendation["bet_type"],
                json.dumps(recommendation["combinations"], ensure_ascii=False),
                recommendation["confidence"],
                round(float(recommendation["score"]), 3),
                recommendation["reason"],
                recommendation["skip_reason"],
                int(similar.get("sample_count") or 0),
                similar.get("hit_rate"),
                similar.get("roi"),
                json.dumps(recommendation["features"], ensure_ascii=False),
                RECOMMENDATION_MODEL_VERSION,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        saved += 1
    return saved


def ensure_prediction_bets(conn) -> int:
    saved = 0
    predictions = rows(
        conn,
        """
        SELECT id, race_id, race_date, prediction_type,
               predicted_1st, predicted_2nd, predicted_3rd, created_at
        FROM race_prediction
        """,
    )
    for prediction in predictions:
        predicted = [
            int(prediction["predicted_1st"]),
            int(prediction["predicted_2nd"]),
            int(prediction["predicted_3rd"]),
        ]
        for bet_type, combinations in bet_combinations(predicted, prediction["prediction_type"]).items():
            for combination in combinations:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO race_prediction_bet
                        (
                            prediction_id, race_id, race_date, prediction_type,
                            bet_type, combination, stake_amount, created_at
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        prediction["id"],
                        prediction["race_id"],
                        prediction["race_date"],
                        prediction["prediction_type"],
                        bet_type,
                        combination,
                        STAKE_AMOUNT,
                        prediction["created_at"],
                    ),
                )
                saved += max(cursor.rowcount, 0)
    conn.commit()
    return saved


def clear_date_predictions(conn, target_date: str) -> None:
    prediction_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM race_prediction WHERE race_date = ?",
            (target_date,),
        ).fetchall()
    ]
    if prediction_ids:
        bet_ids = [
            row["id"]
            for row in conn.execute(
                f"""
                SELECT id
                FROM race_prediction_bet
                WHERE prediction_id IN ({','.join('?' for _ in prediction_ids)})
                """,
                prediction_ids,
            ).fetchall()
        ]
        if bet_ids:
            conn.executemany(
                "DELETE FROM race_prediction_bet_result WHERE prediction_bet_id = ?",
                [(item,) for item in bet_ids],
            )
        conn.executemany("DELETE FROM race_prediction_bet WHERE prediction_id = ?", [(item,) for item in prediction_ids])
        conn.executemany("DELETE FROM race_prediction_result WHERE prediction_id = ?", [(item,) for item in prediction_ids])
    conn.execute("DELETE FROM race_prediction WHERE race_date = ?", (target_date,))
    conn.execute("DELETE FROM race_bet_recommendation WHERE race_date = ?", (target_date,))


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


def generate_predictions(conn, target_date: str, replace: bool = False) -> int:
    include_analysis_detail = is_dev_environment()
    existing = scalar(conn, "SELECT COUNT(*) FROM race_prediction WHERE race_date = ?", (target_date,)) or 0
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
    if replace:
        clear_date_predictions(conn, target_date)
    refresh_racer_pair_stats(conn, before_date=target_date)
    scored_by_race = {
        race["race_id"]: entry_scores(conn, race, target_date)
        for race in races
    }
    recommendation_count = scalar(
        conn,
        "SELECT COUNT(*) FROM race_bet_recommendation WHERE race_date = ?",
        (target_date,),
    ) or 0
    if replace or not recommendation_count:
        save_bet_recommendations(conn, races, scored_by_race, target_date)
    if existing and not replace:
        conn.commit()
        ensure_prediction_bets(conn)
        return 0
    sample_kind = "backtest" if target_date < default_target_date() else "live"
    saved = 0
    for prediction_type in ALL_PREDICTION_TYPES:
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
                    sample_kind,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            saved += 1
    conn.commit()
    ensure_prediction_bets(conn)
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
            ORDER BY rank, car_no
            """,
            (prediction["race_id"],),
        )
        rank_candidates = {
            rank: [
                int(row["car_no"])
                for row in actual_rows
                if int(row["rank"]) == rank
            ]
            for rank in (1, 2, 3)
        }
        official_top3 = {
            int(row["car_no"])
            for row in actual_rows
        }
        if len(official_top3) < 3 or not rank_candidates[1]:
            continue
        actual = [
            rank_candidates[rank][0] if rank_candidates[rank] else None
            for rank in (1, 2, 3)
        ]
        predicted = [int(prediction["predicted_1st"]), int(prediction["predicted_2nd"]), int(prediction["predicted_3rd"])]
        predicted_combination = "-".join(str(item) for item in predicted)
        payout = scalar(
            conn,
            """
            SELECT payout
            FROM payout
            WHERE race_id = ? AND bet_type = ? AND combination = ?
            LIMIT 1
            """,
            (prediction["race_id"], TRIFECTA, predicted_combination),
        )
        exact = payout is not None
        return_amount = int(payout or 0) if exact else 0
        stake = int(prediction["stake_amount"] or STAKE_AMOUNT)
        roi = (return_amount / stake * 100) if stake else 0
        top2_candidates = set(rank_candidates[1]) | set(rank_candidates[2])
        dead_heat = any(len(candidates) > 1 for candidates in rank_candidates.values())
        conn.execute(
            """
            INSERT OR REPLACE INTO race_prediction_result
                (
                    prediction_id, race_id, actual_1st, actual_2nd, actual_3rd,
                    actual_1st_candidates, actual_2nd_candidates,
                    actual_3rd_candidates, dead_heat,
                    hit_exact, hit_1st, hit_top2, hit_top3_count, payout,
                    stake_amount, return_amount, roi, checked_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prediction["id"],
                prediction["race_id"],
                actual[0],
                actual[1],
                actual[2],
                ",".join(str(item) for item in rank_candidates[1]),
                ",".join(str(item) for item in rank_candidates[2]),
                ",".join(str(item) for item in rank_candidates[3]),
                1 if dead_heat else 0,
                1 if exact else 0,
                1 if predicted[0] in rank_candidates[1] else 0,
                1 if set(predicted[:2]).issubset(top2_candidates) else 0,
                len(set(predicted) & official_top3),
                payout or 0,
                stake,
                return_amount,
                roi,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        checked += 1
    conn.commit()
    return checked


def evaluate_prediction_bets(conn) -> int:
    ensure_prediction_bets(conn)
    prediction_bets = rows(
        conn,
        """
        SELECT b.*
        FROM race_prediction_bet b
        JOIN race_master m ON m.race_id = b.race_id
        LEFT JOIN race_prediction_bet_result r ON r.prediction_bet_id = b.id
        WHERE (
                r.id IS NULL
                OR r.payout IS NULL
              )
          AND EXISTS (
                SELECT 1
                FROM race_result rr
                WHERE rr.race_id = b.race_id AND rr.rank IN (1, 2, 3)
              )
          AND EXISTS (
                SELECT 1
                FROM payout pay
                WHERE pay.race_id = b.race_id AND pay.bet_type = b.bet_type
              )
        """,
    )
    checked = 0
    for bet in prediction_bets:
        payout = scalar(
            conn,
            """
            SELECT payout
            FROM payout
            WHERE race_id = ? AND bet_type = ? AND combination = ?
            LIMIT 1
            """,
            (bet["race_id"], bet["bet_type"], bet["combination"]),
        )
        hit = payout is not None
        stake = int(bet["stake_amount"] or STAKE_AMOUNT)
        return_amount = int(payout or 0) if hit else 0
        roi = (return_amount / stake * 100) if stake else 0
        conn.execute(
            """
            INSERT OR REPLACE INTO race_prediction_bet_result
                (
                    prediction_bet_id, race_id, hit, payout, stake_amount,
                    return_amount, roi, checked_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bet["id"],
                bet["race_id"],
                1 if hit else 0,
                payout or 0,
                stake,
                return_amount,
                roi,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        checked += 1
    conn.commit()
    return checked


def run(target_date: str | None = None, replace: bool = False) -> dict:
    target_date = target_date or default_target_date()
    with connect() as conn:
        init_db(conn)
        checked_before = evaluate_predictions(conn)
        bet_checked_before = evaluate_prediction_bets(conn)
        saved = generate_predictions(conn, target_date, replace=replace)
        checked_after = evaluate_predictions(conn)
        bet_checked_after = evaluate_prediction_bets(conn)
        clear_analysis_details_if_needed(conn)
        conn.commit()
    return {
        "date": target_date,
        "predictions": saved,
        "checked": checked_before + checked_after,
        "bet_checked": bet_checked_before + bet_checked_after,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and evaluate keirin predictions")
    parser.add_argument("--date", help="Target date in YYYY-MM-DD. Default: today")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing predictions for the target date. Historical replacements are marked as backtests.",
    )
    args = parser.parse_args()
    result = run(args.date, replace=args.replace)
    print(result)


if __name__ == "__main__":
    main()
