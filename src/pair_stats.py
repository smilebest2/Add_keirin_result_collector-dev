from datetime import datetime
from itertools import combinations


def racer_key(row: dict) -> str | None:
    name = row.get("racer_name") or ""
    prefecture = row.get("prefecture") or ""
    term = row.get("term")
    if not name:
        return None
    return f"{name}|{prefecture}|{term or ''}"


def normalized_pair(left: dict, right: dict) -> tuple[dict, dict] | None:
    left_key = racer_key(left)
    right_key = racer_key(right)
    if not left_key or not right_key or left_key == right_key:
        return None
    return (left, right) if left_key < right_key else (right, left)


def refresh_racer_pair_stats(conn, before_date: str | None = None) -> int:
    conn.execute("DELETE FROM racer_pair_stats")
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO racer_pair_stats
            (
                racer_key_a, racer_key_b, racer_name_a, racer_name_b,
                prefecture_a, prefecture_b, term_a, term_b,
                races_together, a_ahead_count, b_ahead_count,
                both_top2_count, both_top3_count,
                a_first_b_second_count, b_first_a_second_count,
                wide_count, quinella_count, avg_rank_sum,
                min_race_date, max_race_date, updated_at
            )
        WITH base AS (
            SELECT m.race_id,
                   m.race_date,
                   r.car_no,
                   r.racer_name,
                   COALESCE(r.prefecture, '') AS prefecture,
                   r.term,
                   COALESCE(r.racer_name, '') || '|' || COALESCE(r.prefecture, '') || '|' || COALESCE(CAST(r.term AS TEXT), '') AS racer_key,
                   r.rank
            FROM race_result r
            JOIN race_master m ON m.race_id = r.race_id
            WHERE r.rank IS NOT NULL
              AND r.racer_name IS NOT NULL
              AND r.racer_name != ''
              AND (? IS NULL OR m.race_date < ?)
        ), pairs AS (
            SELECT
                CASE WHEN a.racer_key < b.racer_key THEN a.racer_key ELSE b.racer_key END AS racer_key_a,
                CASE WHEN a.racer_key < b.racer_key THEN b.racer_key ELSE a.racer_key END AS racer_key_b,
                CASE WHEN a.racer_key < b.racer_key THEN a.racer_name ELSE b.racer_name END AS racer_name_a,
                CASE WHEN a.racer_key < b.racer_key THEN b.racer_name ELSE a.racer_name END AS racer_name_b,
                CASE WHEN a.racer_key < b.racer_key THEN a.prefecture ELSE b.prefecture END AS prefecture_a,
                CASE WHEN a.racer_key < b.racer_key THEN b.prefecture ELSE a.prefecture END AS prefecture_b,
                CASE WHEN a.racer_key < b.racer_key THEN a.term ELSE b.term END AS term_a,
                CASE WHEN a.racer_key < b.racer_key THEN b.term ELSE a.term END AS term_b,
                CASE WHEN a.racer_key < b.racer_key THEN a.rank ELSE b.rank END AS rank_a,
                CASE WHEN a.racer_key < b.racer_key THEN b.rank ELSE a.rank END AS rank_b,
                a.race_date AS race_date
            FROM base a
            JOIN base b ON b.race_id = a.race_id AND b.car_no > a.car_no
            WHERE a.racer_key != b.racer_key
        )
        SELECT racer_key_a,
               racer_key_b,
               racer_name_a,
               racer_name_b,
               prefecture_a,
               prefecture_b,
               term_a,
               term_b,
               COUNT(*) AS races_together,
               SUM(CASE WHEN rank_a < rank_b THEN 1 ELSE 0 END) AS a_ahead_count,
               SUM(CASE WHEN rank_b < rank_a THEN 1 ELSE 0 END) AS b_ahead_count,
               SUM(CASE WHEN rank_a <= 2 AND rank_b <= 2 THEN 1 ELSE 0 END) AS both_top2_count,
               SUM(CASE WHEN rank_a <= 3 AND rank_b <= 3 THEN 1 ELSE 0 END) AS both_top3_count,
               SUM(CASE WHEN rank_a = 1 AND rank_b = 2 THEN 1 ELSE 0 END) AS a_first_b_second_count,
               SUM(CASE WHEN rank_b = 1 AND rank_a = 2 THEN 1 ELSE 0 END) AS b_first_a_second_count,
               SUM(CASE WHEN rank_a <= 3 AND rank_b <= 3 THEN 1 ELSE 0 END) AS wide_count,
               SUM(CASE WHEN rank_a <= 2 AND rank_b <= 2 THEN 1 ELSE 0 END) AS quinella_count,
               AVG(rank_a + rank_b) AS avg_rank_sum,
               MIN(race_date) AS min_race_date,
               MAX(race_date) AS max_race_date,
               ? AS updated_at
        FROM pairs
        GROUP BY racer_key_a, racer_key_b
        """,
        (before_date, before_date, now),
    )
    count = conn.execute("SELECT COUNT(*) FROM racer_pair_stats").fetchone()[0]
    conn.commit()
    return int(count or 0)


def pair_context_for_entries(conn, entries: list[dict]) -> dict[int, dict]:
    context = {
        int(entry["car_no"]): {
            "pair_races": 0,
            "pair_samples": 0,
            "pair_ahead_rate": None,
            "pair_top2_rate": None,
            "pair_top3_rate": None,
            "pair_rank_sum": None,
        }
        for entry in entries
        if entry.get("car_no") is not None
    }
    totals = {
        car_no: {
            "races": 0,
            "ahead_weighted": 0.0,
            "top2_weighted": 0.0,
            "top3_weighted": 0.0,
            "rank_sum_weighted": 0.0,
            "samples": 0,
        }
        for car_no in context
    }
    for left, right in combinations(entries, 2):
        pair = normalized_pair(left, right)
        if pair is None:
            continue
        a, b = pair
        key_a = racer_key(a)
        key_b = racer_key(b)
        row = conn.execute(
            """
            SELECT *
            FROM racer_pair_stats
            WHERE racer_key_a = ? AND racer_key_b = ?
            """,
            (key_a, key_b),
        ).fetchone()
        if row is None:
            continue
        stat = dict(row)
        races = int(stat.get("races_together") or 0)
        if races <= 0:
            continue
        a_car = int(a["car_no"])
        b_car = int(b["car_no"])
        top2_rate = float(stat.get("both_top2_count") or 0) * 100 / races
        top3_rate = float(stat.get("both_top3_count") or 0) * 100 / races
        rank_sum = float(stat.get("avg_rank_sum") or 0)
        a_ahead_rate = float(stat.get("a_ahead_count") or 0) * 100 / races
        b_ahead_rate = float(stat.get("b_ahead_count") or 0) * 100 / races
        for car_no, ahead_rate in ((a_car, a_ahead_rate), (b_car, b_ahead_rate)):
            totals[car_no]["races"] += races
            totals[car_no]["ahead_weighted"] += ahead_rate * races
            totals[car_no]["top2_weighted"] += top2_rate * races
            totals[car_no]["top3_weighted"] += top3_rate * races
            totals[car_no]["rank_sum_weighted"] += rank_sum * races
            totals[car_no]["samples"] += 1

    for car_no, total in totals.items():
        races = total["races"]
        if races <= 0:
            continue
        context[car_no] = {
            "pair_races": races,
            "pair_samples": total["samples"],
            "pair_ahead_rate": total["ahead_weighted"] / races,
            "pair_top2_rate": total["top2_weighted"] / races,
            "pair_top3_rate": total["top3_weighted"] / races,
            "pair_rank_sum": total["rank_sum_weighted"] / races,
        }
    return context
