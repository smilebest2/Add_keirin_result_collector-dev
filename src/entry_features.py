import argparse
import logging
import math
from datetime import datetime, timezone, timedelta

from .config import LOG_DIR
from .db import connect, init_db
from .lineup_validation import normalize_lineup


JST = timezone(timedelta(hours=9))

LINE_STRENGTH_SCORE_WEIGHT = 1.0
LINE_STRENGTH_TOP3_WEIGHT = 0.35
LINE_STRENGTH_MEMBER_WEIGHT = 2.5

FEATURE_COLUMNS = [
    "line_member_count",
    "line_average_score",
    "line_score_avg",
    "line_max_score",
    "line_min_score",
    "line_score_std",
    "line_top3_std",
    "line_winrate_std",
    "line_average_win_rate",
    "line_average_top3_rate",
    "line_average_age",
    "line_average_bs",
    "line_average_escape",
    "line_average_dash",
    "line_average_mark",
    "line_average_chase",
    "line_total_escape",
    "line_total_dash",
    "line_total_mark",
    "line_total_chase",
    "line_total_bs",
    "line_total_h",
    "line_total_s",
    "line_score_rank",
    "score_rank_in_line",
    "top3_rank_in_line",
    "win_rate_rank_in_line",
    "line_win_rate_rank",
    "line_bs_rank",
    "line_age_rank",
    "line_escape_rank",
    "line_dash_rank",
    "line_mark_rank",
    "line_chase_rank",
    "race_score_rank",
    "race_win_rate_rank",
    "race_top2_rank",
    "race_top3_rank",
    "race_age_rank",
    "race_escape_rank",
    "race_dash_rank",
    "race_mark_rank",
    "race_chase_rank",
    "score_minus_race_avg",
    "score_minus_line_avg",
    "win_rate_minus_race_avg",
    "top3_minus_race_avg",
    "bs_minus_race_avg",
    "age_minus_race_avg",
    "line_position",
    "line_is_head",
    "line_is_second",
    "line_is_last",
    "leader_score",
    "leader_escape",
    "leader_dash",
    "leader_bs",
    "leader_win_rate",
    "leader_top3_rate",
    "leader_age",
    "leader_score_rank",
    "leader_is_escape_type",
    "leader_is_dash_type",
    "is_second",
    "leader_second_score_gap",
    "leader_second_age_gap",
    "leader_second_bs_gap",
    "leader_second_win_gap",
    "style_escape",
    "style_dash",
    "style_mark",
    "style_allround",
    "age_20s",
    "age_30s",
    "age_40s",
    "age_50plus",
    "score_under_95",
    "score_95_100",
    "score_100_105",
    "score_105plus",
    "line_strength",
    "line_strength_rank",
    "line_strength_gap",
    "line_strength_ratio",
    "line_gap_top",
    "line_gap_second",
    "line_members",
    "is_single_line",
    "is_two_man_line",
    "is_three_man_line",
    "is_four_man_line",
    "score_gap_top",
    "score_gap_second",
    "score_gap_line_top",
    "age_gap_line_top",
    "win_gap_line_top",
]

TABLE_COLUMNS = [
    "race_id",
    "race_date",
    "venue",
    "race_no",
    "car_no",
    "racer_name",
    "rank",
    "is_top3",
    "line_no",
    *FEATURE_COLUMNS,
    "created_at",
]


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "entry_features.log", mode="w", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def rows(conn, sql: str, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def value(row: dict, key: str) -> float:
    item = row.get(key)
    if item is None:
        return 0.0
    return float(item)


def average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stddev(values: list[float]) -> float:
    if not values:
        return 0.0
    avg = average(values)
    return math.sqrt(sum((item - avg) ** 2 for item in values) / len(values))


def rank_map(items: list[dict], key: str, descending: bool = True) -> dict[int, int]:
    sorted_items = sorted(
        items,
        key=lambda row: (value(row, key), -int(row["car_no"])),
        reverse=descending,
    )
    return {int(row["car_no"]): index for index, row in enumerate(sorted_items, start=1)}


def build_lineup(conn, race_id: str, entries: list[dict]) -> list[list[int]]:
    entry_car_nos = {int(row["car_no"]) for row in entries}
    feature_rows = rows(
        conn,
        """
        SELECT car_no, line_no, line_position
        FROM race_line_features
        WHERE race_id = ?
        ORDER BY line_no, line_position
        """,
        (race_id,),
    )
    if feature_rows:
        groups: dict[int, list[tuple[int, int]]] = {}
        for row in feature_rows:
            groups.setdefault(int(row["line_no"]), []).append((int(row["line_position"]), int(row["car_no"])))
        parsed = [[car_no for _pos, car_no in sorted(group)] for _line_no, group in sorted(groups.items())]
        if parsed:
            return parsed

    lineup_rows = rows(
        conn,
        """
        SELECT car_no, line_no, line_position
        FROM race_lineup_forecast
        WHERE race_id = ?
        ORDER BY line_no, line_position
        """,
        (race_id,),
    )
    lineup = normalize_lineup(lineup_rows, entry_car_nos)
    if not lineup:
        return [[car_no] for car_no in sorted(entry_car_nos)]
    groups: dict[int, list[tuple[int, int]]] = {}
    for row in lineup:
        groups.setdefault(int(row["line_no"]), []).append((int(row["line_position"]), int(row["car_no"])))
    return [[car_no for _pos, car_no in sorted(group)] for _line_no, group in sorted(groups.items())]


def dominant_style(entry: dict) -> str:
    counts = {
        "escape": value(entry, "escape_count"),
        "dash": value(entry, "makuri_count"),
        "mark": value(entry, "mark_count"),
        "chase": value(entry, "sashi_count"),
    }
    total = sum(counts.values())
    if total <= 0:
        return "allround"
    best = max(counts, key=counts.get)
    if counts[best] / total < 0.4:
        return "allround"
    if best == "chase":
        return "allround"
    return best


def line_strength(line_stats: dict) -> float:
    return (
        line_stats["line_average_score"] * LINE_STRENGTH_SCORE_WEIGHT
        + line_stats["line_average_top3_rate"] * LINE_STRENGTH_TOP3_WEIGHT
        + line_stats["line_member_count"] * LINE_STRENGTH_MEMBER_WEIGHT
    )


def stats_for_line(members: list[dict]) -> dict:
    scores = [value(row, "score") for row in members]
    win_rates = [value(row, "win_rate") for row in members]
    top3_rates = [value(row, "trifecta_rate") for row in members]
    ages = [value(row, "age") for row in members]
    bs = [value(row, "back_count") for row in members]
    escape = [value(row, "escape_count") for row in members]
    dash = [value(row, "makuri_count") for row in members]
    mark = [value(row, "mark_count") for row in members]
    chase = [value(row, "sashi_count") for row in members]
    line_stats = {
        "line_member_count": len(members),
        "line_average_score": average(scores),
        "line_score_avg": average(scores),
        "line_max_score": max(scores) if scores else 0.0,
        "line_min_score": min(scores) if scores else 0.0,
        "line_score_std": stddev(scores),
        "line_top3_std": stddev(top3_rates),
        "line_winrate_std": stddev(win_rates),
        "line_average_win_rate": average(win_rates),
        "line_average_top3_rate": average(top3_rates),
        "line_average_age": average(ages),
        "line_average_bs": average(bs),
        "line_average_escape": average(escape),
        "line_average_dash": average(dash),
        "line_average_mark": average(mark),
        "line_average_chase": average(chase),
        "line_total_escape": sum(escape),
        "line_total_dash": sum(dash),
        "line_total_mark": sum(mark),
        "line_total_chase": sum(chase),
        "line_total_bs": sum(bs),
        "line_total_h": sum(value(row, "home_count") for row in members),
        "line_total_s": sum(value(row, "start_count") for row in members),
    }
    line_stats["line_strength"] = line_strength(line_stats)
    return line_stats


def age_flags(age: float) -> dict[str, int]:
    return {
        "age_20s": 1 if 20 <= age < 30 else 0,
        "age_30s": 1 if 30 <= age < 40 else 0,
        "age_40s": 1 if 40 <= age < 50 else 0,
        "age_50plus": 1 if age >= 50 else 0,
    }


def score_flags(score: float) -> dict[str, int]:
    return {
        "score_under_95": 1 if score < 95 else 0,
        "score_95_100": 1 if 95 <= score < 100 else 0,
        "score_100_105": 1 if 100 <= score < 105 else 0,
        "score_105plus": 1 if score >= 105 else 0,
    }


def build_features_for_race(conn, race: dict, entries: list[dict], result_by_car: dict[int, dict]) -> list[dict]:
    now = datetime.now(JST).isoformat(timespec="seconds")
    groups = build_lineup(conn, race["race_id"], entries)
    by_car = {int(row["car_no"]): row for row in entries}
    line_no_by_car = {}
    pos_by_car = {}
    lines = {}
    for line_no, group in enumerate(groups, start=1):
        members = [by_car[car_no] for car_no in group if car_no in by_car]
        if not members:
            continue
        lines[line_no] = members
        for pos, car_no in enumerate(group, start=1):
            line_no_by_car[car_no] = line_no
            pos_by_car[car_no] = 0 if len(group) == 1 else min(pos, 4)

    race_avg = {
        "score": average([value(row, "score") for row in entries]),
        "win_rate": average([value(row, "win_rate") for row in entries]),
        "top3": average([value(row, "trifecta_rate") for row in entries]),
        "bs": average([value(row, "back_count") for row in entries]),
        "age": average([value(row, "age") for row in entries]),
    }
    race_ranks = {
        "score": rank_map(entries, "score"),
        "win_rate": rank_map(entries, "win_rate"),
        "top2": rank_map(entries, "quinella_rate"),
        "top3": rank_map(entries, "trifecta_rate"),
        "age": rank_map(entries, "age", descending=False),
        "escape": rank_map(entries, "escape_count"),
        "dash": rank_map(entries, "makuri_count"),
        "mark": rank_map(entries, "mark_count"),
        "chase": rank_map(entries, "sashi_count"),
    }
    race_scores = sorted([value(row, "score") for row in entries], reverse=True)
    top_score = race_scores[0] if race_scores else 0.0
    second_score = race_scores[1] if len(race_scores) > 1 else top_score
    line_stats_by_no = {line_no: stats_for_line(members) for line_no, members in lines.items()}
    line_strength_ranks = {
        line_no: rank
        for rank, (line_no, _strength) in enumerate(
            sorted(
                ((line_no, stats["line_strength"]) for line_no, stats in line_stats_by_no.items()),
                key=lambda item: (item[1], -item[0]),
                reverse=True,
            ),
            start=1,
        )
    }
    line_strength_values = sorted((stats["line_strength"] for stats in line_stats_by_no.values()), reverse=True)
    top_line_strength = line_strength_values[0] if line_strength_values else 0.0

    features = []
    for entry in entries:
        car_no = int(entry["car_no"])
        line_no = int(line_no_by_car.get(car_no, 0))
        members = lines.get(line_no, [entry])
        line_stats = line_stats_by_no.get(line_no, stats_for_line([entry]))
        line_ranks = {
            "score": rank_map(members, "score"),
            "win_rate": rank_map(members, "win_rate"),
            "top3": rank_map(members, "trifecta_rate"),
            "bs": rank_map(members, "back_count"),
            "age": rank_map(members, "age", descending=False),
            "escape": rank_map(members, "escape_count"),
            "dash": rank_map(members, "makuri_count"),
            "mark": rank_map(members, "mark_count"),
            "chase": rank_map(members, "sashi_count"),
        }
        leader = members[0] if members else entry
        leader_style = dominant_style(leader)
        line_position = int(pos_by_car.get(car_no, 0))
        is_second = 1 if line_position == 2 else 0
        line_member_count = int(line_stats["line_member_count"])
        line_scores = sorted([value(row, "score") for row in members], reverse=True)
        line_top_score = line_scores[0] if line_scores else value(entry, "score")
        line_second_score = line_scores[1] if len(line_scores) > 1 else line_top_score
        style = dominant_style(entry)
        rank = int((result_by_car.get(car_no) or {}).get("rank") or 0)
        row = {
            "race_id": race["race_id"],
            "race_date": race.get("race_date") or "",
            "venue": race.get("venue") or "",
            "race_no": int(race.get("race_no") or 0),
            "car_no": car_no,
            "racer_name": entry.get("racer_name") or "",
            "rank": rank,
            "is_top3": 1 if 1 <= rank <= 3 else 0,
            "line_no": line_no,
            **line_stats,
            "line_score_rank": line_ranks["score"].get(car_no, 0),
            "score_rank_in_line": line_ranks["score"].get(car_no, 0),
            "top3_rank_in_line": line_ranks["top3"].get(car_no, 0),
            "win_rate_rank_in_line": line_ranks["win_rate"].get(car_no, 0),
            "line_win_rate_rank": line_ranks["win_rate"].get(car_no, 0),
            "line_bs_rank": line_ranks["bs"].get(car_no, 0),
            "line_age_rank": line_ranks["age"].get(car_no, 0),
            "line_escape_rank": line_ranks["escape"].get(car_no, 0),
            "line_dash_rank": line_ranks["dash"].get(car_no, 0),
            "line_mark_rank": line_ranks["mark"].get(car_no, 0),
            "line_chase_rank": line_ranks["chase"].get(car_no, 0),
            "race_score_rank": race_ranks["score"].get(car_no, 0),
            "race_win_rate_rank": race_ranks["win_rate"].get(car_no, 0),
            "race_top2_rank": race_ranks["top2"].get(car_no, 0),
            "race_top3_rank": race_ranks["top3"].get(car_no, 0),
            "race_age_rank": race_ranks["age"].get(car_no, 0),
            "race_escape_rank": race_ranks["escape"].get(car_no, 0),
            "race_dash_rank": race_ranks["dash"].get(car_no, 0),
            "race_mark_rank": race_ranks["mark"].get(car_no, 0),
            "race_chase_rank": race_ranks["chase"].get(car_no, 0),
            "score_minus_race_avg": value(entry, "score") - race_avg["score"],
            "score_minus_line_avg": value(entry, "score") - line_stats["line_average_score"],
            "win_rate_minus_race_avg": value(entry, "win_rate") - race_avg["win_rate"],
            "top3_minus_race_avg": value(entry, "trifecta_rate") - race_avg["top3"],
            "bs_minus_race_avg": value(entry, "back_count") - race_avg["bs"],
            "age_minus_race_avg": value(entry, "age") - race_avg["age"],
            "line_position": line_position,
            "line_is_head": 1 if line_position == 1 else 0,
            "line_is_second": is_second,
            "line_is_last": 1 if line_member_count == 1 or car_no == int(members[-1]["car_no"]) else 0,
            "leader_score": value(leader, "score"),
            "leader_escape": value(leader, "escape_count"),
            "leader_dash": value(leader, "makuri_count"),
            "leader_bs": value(leader, "back_count"),
            "leader_win_rate": value(leader, "win_rate"),
            "leader_top3_rate": value(leader, "trifecta_rate"),
            "leader_age": value(leader, "age"),
            "leader_score_rank": race_ranks["score"].get(int(leader["car_no"]), 0),
            "leader_is_escape_type": 1 if leader_style == "escape" else 0,
            "leader_is_dash_type": 1 if leader_style == "dash" else 0,
            "is_second": is_second,
            "leader_second_score_gap": value(leader, "score") - value(entry, "score") if is_second else 0,
            "leader_second_age_gap": value(leader, "age") - value(entry, "age") if is_second else 0,
            "leader_second_bs_gap": value(leader, "back_count") - value(entry, "back_count") if is_second else 0,
            "leader_second_win_gap": value(leader, "win_rate") - value(entry, "win_rate") if is_second else 0,
            "style_escape": 1 if style == "escape" else 0,
            "style_dash": 1 if style == "dash" else 0,
            "style_mark": 1 if style == "mark" else 0,
            "style_allround": 1 if style == "allround" else 0,
            **age_flags(value(entry, "age")),
            **score_flags(value(entry, "score")),
            "line_strength_rank": line_strength_ranks.get(line_no, 0),
            "line_strength_gap": top_line_strength - line_stats["line_strength"],
            "line_strength_ratio": line_stats["line_strength"] / top_line_strength if top_line_strength else 0,
            "line_gap_top": line_top_score - value(entry, "score"),
            "line_gap_second": line_second_score - value(entry, "score"),
            "line_members": line_member_count,
            "is_single_line": 1 if line_member_count == 1 else 0,
            "is_two_man_line": 1 if line_member_count == 2 else 0,
            "is_three_man_line": 1 if line_member_count == 3 else 0,
            "is_four_man_line": 1 if line_member_count >= 4 else 0,
            "score_gap_top": top_score - value(entry, "score"),
            "score_gap_second": second_score - value(entry, "score"),
            "score_gap_line_top": value(leader, "score") - value(entry, "score"),
            "age_gap_line_top": value(leader, "age") - value(entry, "age"),
            "win_gap_line_top": value(leader, "win_rate") - value(entry, "win_rate"),
            "created_at": now,
        }
        for column in FEATURE_COLUMNS:
            row[column] = row.get(column, 0) or 0
        features.append(row)
    return features


def save_features(conn, features: list[dict]) -> None:
    if not features:
        return
    placeholders = ", ".join("?" for _ in TABLE_COLUMNS)
    column_sql = ", ".join(TABLE_COLUMNS)
    update_sql = ", ".join(f"{column}=excluded.{column}" for column in TABLE_COLUMNS if column not in {"race_id", "car_no"})
    conn.executemany(
        f"""
        INSERT INTO race_entry_features ({column_sql})
        VALUES ({placeholders})
        ON CONFLICT(race_id, car_no) DO UPDATE SET {update_sql}
        """,
        [tuple(feature[column] for column in TABLE_COLUMNS) for feature in features],
    )


def write_quality_log(conn, table_name: str = "race_entry_features") -> list[dict]:
    now = datetime.now(JST).isoformat(timespec="seconds")
    summaries = []
    for column in FEATURE_COLUMNS:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS row_count,
                SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END) AS null_count,
                MIN({column}) AS min_value,
                MAX({column}) AS max_value,
                AVG({column}) AS avg_value,
                AVG({column} * {column}) AS avg_square,
                COUNT(DISTINCT {column}) AS category_count
            FROM {table_name}
            """
        ).fetchone()
        row_count = int(row["row_count"] or 0)
        null_count = int(row["null_count"] or 0)
        avg_value = float(row["avg_value"] or 0)
        avg_square = float(row["avg_square"] or 0)
        summary = {
            "table_name": table_name,
            "feature_name": column,
            "row_count": row_count,
            "null_count": null_count,
            "min_value": float(row["min_value"] or 0),
            "max_value": float(row["max_value"] or 0),
            "avg_value": avg_value,
            "stddev_value": math.sqrt(max(0.0, avg_square - avg_value * avg_value)),
            "category_count": int(row["category_count"] or 0),
            "importance_value": 0.0,
            "missing_rate": round(null_count * 100 / row_count, 6) if row_count else 0.0,
            "created_at": now,
        }
        summaries.append(summary)
        logging.info(
            "feature_quality %s null=%s min=%.4f max=%.4f avg=%.4f missing=%.4f%%",
            column,
            summary["null_count"],
            summary["min_value"],
            summary["max_value"],
            summary["avg_value"],
            summary["missing_rate"],
        )
    conn.execute("DELETE FROM feature_quality_log WHERE table_name = ?", (table_name,))
    conn.executemany(
        """
        INSERT INTO feature_quality_log
            (
                table_name, feature_name, row_count, null_count,
                min_value, max_value, avg_value, stddev_value,
                category_count, importance_value, missing_rate, created_at
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["table_name"],
                row["feature_name"],
                row["row_count"],
                row["null_count"],
                row["min_value"],
                row["max_value"],
                row["avg_value"],
                row["stddev_value"],
                row["category_count"],
                row["importance_value"],
                row["missing_rate"],
                row["created_at"],
            )
            for row in summaries
        ],
    )
    return summaries


def build_entry_features(conn, start_date: str | None = None, end_date: str | None = None) -> dict:
    init_db(conn)
    filters = []
    params = []
    if start_date:
        filters.append("s.race_date >= ?")
        params.append(start_date)
    if end_date:
        filters.append("s.race_date <= ?")
        params.append(end_date)
    where = "WHERE " + " AND ".join(filters) if filters else ""
    races = rows(
        conn,
        f"""
        SELECT s.race_id, s.race_date, s.venue, s.race_no
        FROM race_schedule s
        {where}
        ORDER BY s.race_date, s.venue, s.race_no
        """,
        params,
    )
    entries_by_race: dict[str, list[dict]] = {}
    for entry in rows(conn, "SELECT * FROM race_entry ORDER BY race_id, car_no"):
        entries_by_race.setdefault(entry["race_id"], []).append(entry)
    results_by_race: dict[str, dict[int, dict]] = {}
    for result in rows(conn, "SELECT race_id, car_no, rank FROM race_result WHERE car_no IS NOT NULL"):
        results_by_race.setdefault(result["race_id"], {})[int(result["car_no"])] = result

    all_features = []
    skipped = 0
    for race in races:
        entries = entries_by_race.get(race["race_id"], [])
        if len(entries) < 3:
            skipped += 1
            continue
        all_features.extend(build_features_for_race(conn, race, entries, results_by_race.get(race["race_id"], {})))

    if start_date or end_date:
        delete_filters = []
        delete_params = []
        if start_date:
            delete_filters.append("race_date >= ?")
            delete_params.append(start_date)
        if end_date:
            delete_filters.append("race_date <= ?")
            delete_params.append(end_date)
        conn.execute(f"DELETE FROM race_entry_features WHERE {' AND '.join(delete_filters)}", delete_params)
    else:
        conn.execute("DELETE FROM race_entry_features")
    save_features(conn, all_features)
    quality = write_quality_log(conn)
    conn.commit()
    return {
        "races": len(races),
        "skipped_races": skipped,
        "features": len(all_features),
        "quality_features": len(quality),
    }


def run(start_date: str | None = None, end_date: str | None = None) -> dict:
    setup_logging()
    with connect() as conn:
        return build_entry_features(conn, start_date=start_date, end_date=end_date)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build race entry learning features")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    args = parser.parse_args()
    print(run(start_date=args.start_date, end_date=args.end_date))


if __name__ == "__main__":
    main()
