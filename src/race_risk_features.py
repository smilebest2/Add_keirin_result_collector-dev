import argparse
import logging
import math
import re
from datetime import datetime, timedelta, timezone

from .config import LOG_DIR
from .db import connect, init_db


JST = timezone(timedelta(hours=9))
MODEL_NAME = "heuristic-volatility-v1"
LIGHTGBM_MODEL_NAME = "lightgbm-volatility-v1"
NORMALIZATION_METHOD = "z_score"
Z_SCORE_CLIP = 4.0
TEMPERATURE_CANDIDATES = [8.0, 12.0, 16.0, 20.0, 24.0]

FEATURE_SCORE_WEIGHTS = [
    ("score_minus_race_avg", 1.5),
    ("top3_minus_race_avg", 1.2),
    ("win_rate_minus_race_avg", 1.0),
    ("race_score_rank", -0.8),
    ("race_top3_rank", -0.6),
    ("line_strength_rank", -0.7),
    ("score_gap_top", -0.5),
    ("score_gap_second", -0.3),
]

CONFIDENCE_COLUMNS = [
    "top1_probability",
    "top2_probability",
    "top3_probability",
    "probability_gap",
    "probability_variance",
    "probability_entropy",
    "top1_top2_gap",
    "top2_top3_gap",
    "top3_top4_gap",
    "line_count",
    "tanki_count",
    "max_line_members",
    "line_member_variance",
    "line_strength_gap",
    "line_strength_ratio",
    "confidence_score",
    "expected_value_score",
]

VOLATILITY_COLUMNS = [
    "line_member_variance",
    "line_strength_gap",
    "score_minus_race_avg_variance",
    "win_rate_variance",
    "class_variance",
    "age_variance",
    "leader_score_gap",
    "second_score_gap",
    "tanki_count",
    "line_count",
    "high_payout",
    "trifecta_payout",
    "volatility_probability",
]

VOLATILITY_MODEL_FEATURES = [
    "line_member_variance",
    "line_strength_gap",
    "score_minus_race_avg_variance",
    "win_rate_variance",
    "class_variance",
    "age_variance",
    "leader_score_gap",
    "second_score_gap",
    "tanki_count",
    "line_count",
]


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "race_risk_features.log", mode="w", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def rows(conn, sql: str, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def value(row: dict, key: str) -> float:
    item = row.get(key)
    return float(item or 0)


def avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def variance(values: list[float]) -> float:
    if not values:
        return 0.0
    center = avg(values)
    return sum((item - center) ** 2 for item in values) / len(values)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, value))))


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(max(int(len(ordered) * ratio), 0), len(ordered) - 1)
    return ordered[index]


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def stddev(values: list[float]) -> float:
    return math.sqrt(variance(values))


class FeatureScoreNormalizer:
    def __init__(self, stats: dict[str, dict[str, float]], method: str = NORMALIZATION_METHOD):
        self.stats = stats
        self.method = method

    @classmethod
    def from_entries(cls, entries: list[dict]) -> "FeatureScoreNormalizer":
        stats = {}
        for name, _weight in FEATURE_SCORE_WEIGHTS:
            values = [value(entry, name) for entry in entries]
            stats[name] = {
                "mean": avg(values),
                "stddev": stddev(values),
                "min": min(values) if values else 0.0,
                "max": max(values) if values else 0.0,
            }
        return cls(stats)

    def normalized(self, feature: dict, name: str) -> float:
        item = self.stats.get(name) or {}
        scale = item.get("stddev") or 0.0
        if scale <= 0:
            return 0.0
        z_score = (value(feature, name) - float(item.get("mean") or 0.0)) / scale
        return max(-Z_SCORE_CLIP, min(Z_SCORE_CLIP, z_score))

    def contributions(self, feature: dict) -> dict[str, float]:
        return {name: self.normalized(feature, name) * weight for name, weight in FEATURE_SCORE_WEIGHTS}


def feature_prediction_score(feature: dict, normalizer: FeatureScoreNormalizer | None = None) -> float:
    if normalizer is None:
        return sum(value(feature, name) * weight for name, weight in FEATURE_SCORE_WEIGHTS)
    return sum(normalizer.normalized(feature, name) * weight for name, weight in FEATURE_SCORE_WEIGHTS)


def softmax(scores: list[float], temperature: float = 8.0) -> list[float]:
    if not scores:
        return []
    scaled = [score / temperature for score in scores]
    base = max(scaled)
    exps = [math.exp(score - base) for score in scaled]
    total = sum(exps)
    return [item / total for item in exps] if total else [1.0 / len(scores) for _score in scores]


def entropy(probabilities: list[float]) -> float:
    if not probabilities:
        return 0.0
    raw = -sum(prob * math.log(prob) for prob in probabilities if prob > 0)
    return raw / math.log(len(probabilities)) if len(probabilities) > 1 else 0.0


def stars(score: float) -> str:
    filled = int(round(clamp(score) * 5))
    return "★" * filled + "☆" * (5 - filled)


def confidence_bucket(score: float) -> str:
    score = max(0.0, min(0.999999, score))
    lower = int(score * 10) / 10
    upper = lower + 0.1
    return f"{lower:.1f}-{upper:.1f}"


def histogram(values: list[float]) -> dict[str, int]:
    result = {f"{index / 10:.1f}-{(index + 1) / 10:.1f}": 0 for index in range(10)}
    for item in values:
        result[confidence_bucket(item)] += 1
    return result


def scaled_line_gap(line_strength_gap: float) -> float:
    return math.log1p(max(0.0, line_strength_gap)) / math.log(36.0)


def confidence_from_signals(
    top1_probability: float,
    top1_top2_gap: float,
    probability_variance: float,
    probability_entropy: float,
    line_strength_gap: float,
    max_line_members: int,
    line_count: int,
    tanki_count: int,
    line_member_variance: float,
) -> float:
    raw_score = (
        -0.35
        + top1_probability * 1.15
        + top1_top2_gap * 1.05
        + probability_variance * 1.25
        - probability_entropy * 1.10
        + scaled_line_gap(line_strength_gap) * 0.28
        + ((max_line_members - 1) / 4.0) * 0.16
        - max(0.0, (line_count - 2) / 5.0) * 0.20
        - (tanki_count / 4.0) * 0.16
        - min(line_member_variance, 3.0) * 0.04
    )
    return sigmoid(raw_score)


def payout_threshold(conn, start_date: str | None, end_date: str | None) -> float:
    filters = ["p.bet_type LIKE '3%'"]
    params = []
    if start_date:
        filters.append("m.race_date >= ?")
        params.append(start_date)
    if end_date:
        filters.append("m.race_date <= ?")
        params.append(end_date)
    values = [
        int(row["payout"] or 0)
        for row in rows(
            conn,
            f"""
            SELECT p.payout
            FROM payout p
            JOIN race_master m ON m.race_id = p.race_id
            WHERE {" AND ".join(filters)}
              AND p.payout IS NOT NULL
              AND p.payout > 0
            ORDER BY p.payout
            """,
            params,
        )
    ]
    if not values:
        return 10000.0
    index = int(len(values) * 0.8)
    index = min(max(index, 0), len(values) - 1)
    return max(10000.0, float(values[index]))


def payout_by_race(conn) -> dict[str, int]:
    return {
        row["race_id"]: int(row["payout"] or 0)
        for row in rows(
            conn,
            """
            SELECT race_id, MAX(payout) AS payout
            FROM payout
            WHERE bet_type LIKE '3%'
            GROUP BY race_id
            """,
        )
    }


def class_value(text: str | None) -> float:
    if not text:
        return 0.0
    match = re.search(r"([ASL])(\d)?", text)
    if not match:
        return 0.0
    base = {"S": 30, "A": 20, "L": 10}.get(match.group(1), 0)
    rank = int(match.group(2) or 3)
    return float(base + (4 - rank))


def race_classes(conn) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    for row in rows(conn, "SELECT race_id, class FROM race_entry"):
        result.setdefault(row["race_id"], []).append(class_value(row.get("class")))
    return result


def grouped_line_stats(entries: list[dict]) -> list[dict]:
    grouped: dict[int, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(int(entry.get("line_no") or 0), []).append(entry)
    stats = []
    for line_no, members in grouped.items():
        strengths = [value(row, "line_strength") for row in members]
        line_strength = avg(strengths)
        stats.append({
            "line_no": line_no,
            "members": len(members),
            "strength": line_strength,
            "leader_score": max((value(row, "leader_score") for row in members), default=0.0),
        })
    return stats


def bucket(value_: float) -> str:
    if value_ >= 0.7:
        return "high"
    if value_ >= 0.4:
        return "middle"
    return "low"


def apply_lightgbm_volatility(records: list[dict]) -> bool:
    train_rows = [row for row in records if int(row.get("trifecta_payout") or 0) > 0]
    labels = [int(row["high_payout"]) for row in train_rows]
    if len(train_rows) < 80 or len(set(labels)) < 2:
        return False
    try:
        from lightgbm import LGBMClassifier
    except Exception:
        return False
    model = LGBMClassifier(
        n_estimators=80,
        max_depth=3,
        learning_rate=0.06,
        num_leaves=7,
        min_child_samples=20,
        random_state=42,
        verbose=-1,
    )
    x_train = [[float(row.get(feature) or 0) for feature in VOLATILITY_MODEL_FEATURES] for row in train_rows]
    model.fit(x_train, labels)
    x_all = [[float(row.get(feature) or 0) for feature in VOLATILITY_MODEL_FEATURES] for row in records]
    probabilities = model.predict_proba(x_all)
    for row, probability in zip(records, probabilities):
        score = float(probability[1])
        row["volatility_probability"] = score
        row["volatility_bucket"] = bucket(score)
        row["model_name"] = LIGHTGBM_MODEL_NAME
    return True


def build_for_race(
    race: dict,
    entries: list[dict],
    payout: int,
    threshold: float,
    class_values: list[float],
    normalizer: FeatureScoreNormalizer | None = None,
    temperature: float = 8.0,
) -> tuple[dict, dict]:
    scored = sorted(
        [
            {
                **entry,
                "feature_score": feature_prediction_score(entry, normalizer),
            }
            for entry in entries
        ],
        key=lambda row: (row["feature_score"], value(row, "top3_minus_race_avg"), -int(row["car_no"])),
        reverse=True,
    )
    scores = [row["feature_score"] for row in scored]
    probabilities = softmax(scores, temperature=temperature)
    ranked_probs = sorted(zip(scored, probabilities), key=lambda item: item[1], reverse=True)
    probs = [prob for _row, prob in ranked_probs]
    while len(probs) < 4:
        probs.append(0.0)
    line_stats = grouped_line_stats(entries)
    line_members = [float(row["members"]) for row in line_stats]
    strengths = sorted([row["strength"] for row in line_stats], reverse=True)
    line_strength_gap = strengths[0] - strengths[1] if len(strengths) > 1 else strengths[0] if strengths else 0.0
    line_strength_ratio = strengths[0] / strengths[1] if len(strengths) > 1 and strengths[1] else 1.0 if strengths else 0.0
    leader_scores = sorted([row["leader_score"] for row in line_stats], reverse=True)
    leader_score_gap = leader_scores[0] - leader_scores[1] if len(leader_scores) > 1 else leader_scores[0] if leader_scores else 0.0
    race_entropy = entropy(probabilities)
    prob_variance = variance(probabilities)
    top_gap = probs[0] - probs[1]
    top2_gap = probs[1] - probs[2]
    top3_gap = probs[2] - probs[3]
    line_count = len([row for row in line_stats if row["line_no"] > 0])
    tanki_count = sum(1 for row in line_stats if row["members"] == 1)
    max_line_members = int(max(line_members, default=0))
    line_member_variance = variance(line_members)
    confidence_score = confidence_from_signals(
        probs[0],
        top_gap,
        prob_variance,
        race_entropy,
        line_strength_gap,
        max_line_members,
        line_count,
        tanki_count,
        line_member_variance,
    )
    score_variance = variance([value(row, "score_minus_race_avg") for row in entries])
    win_variance = variance([value(row, "win_rate_minus_race_avg") for row in entries])
    age_variance = variance([value(row, "age_minus_race_avg") for row in entries])
    second_score_gap = value(scored[0], "leader_second_score_gap") if scored else 0.0
    volatility_probability = sigmoid(
        -1.0
        + race_entropy * 2.1
        - top_gap * 3.2
        - clamp(line_strength_gap / 35.0) * 1.1
        + clamp(score_variance / 70.0) * 0.7
        + clamp(win_variance / 120.0) * 0.8
        + clamp(age_variance / 90.0) * 0.4
        + clamp(tanki_count / 3.0) * 0.7
        + clamp((line_count - 2) / 5.0) * 0.8
        + clamp(line_member_variance, 0.0, 3.0) * 0.18
    )
    expected_value_score = clamp(confidence_score * 0.55 + volatility_probability * 0.45)
    top_rows = [item[0] for item in ranked_probs[:3]]
    while len(top_rows) < 3:
        top_rows.append({})
    confidence = {
        "race_id": race["race_id"],
        "race_date": race.get("race_date") or "",
        "venue": race.get("venue") or "",
        "race_no": int(race.get("race_no") or 0),
        "top1_car_no": int(top_rows[0].get("car_no") or 0),
        "top2_car_no": int(top_rows[1].get("car_no") or 0),
        "top3_car_no": int(top_rows[2].get("car_no") or 0),
        "top1_probability": probs[0],
        "top2_probability": probs[1],
        "top3_probability": probs[2],
        "probability_gap": top_gap,
        "probability_variance": prob_variance,
        "probability_entropy": race_entropy,
        "top1_top2_gap": top_gap,
        "top2_top3_gap": top2_gap,
        "top3_top4_gap": top3_gap,
        "line_count": line_count,
        "tanki_count": tanki_count,
        "max_line_members": max_line_members,
        "line_member_variance": line_member_variance,
        "line_strength_gap": line_strength_gap,
        "line_strength_ratio": line_strength_ratio,
        "confidence_score": confidence_score,
        "confidence_stars": stars(confidence_score),
        "expected_value_score": expected_value_score,
    }
    volatility = {
        "race_id": race["race_id"],
        "race_date": race.get("race_date") or "",
        "venue": race.get("venue") or "",
        "race_no": int(race.get("race_no") or 0),
        "line_member_variance": line_member_variance,
        "line_strength_gap": line_strength_gap,
        "score_minus_race_avg_variance": score_variance,
        "win_rate_variance": win_variance,
        "class_variance": variance(class_values),
        "age_variance": age_variance,
        "leader_score_gap": leader_score_gap,
        "second_score_gap": second_score_gap,
        "tanki_count": tanki_count,
        "line_count": line_count,
        "high_payout": 1 if payout >= threshold and payout > 0 else 0,
        "trifecta_payout": payout,
        "volatility_probability": volatility_probability,
        "volatility_bucket": bucket(volatility_probability),
        "model_name": MODEL_NAME,
    }
    return confidence, volatility


def quality_rows(conn, table_name: str, columns: list[str]) -> list[dict]:
    now = datetime.now(JST).isoformat(timespec="seconds")
    result = []
    for column in columns:
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
        result.append({
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
        })
    return result


def save_quality(conn, table_name: str, columns: list[str]) -> int:
    summaries = quality_rows(conn, table_name, columns)
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
    for row in summaries:
        logging.info(
            "feature_quality %s.%s null=%s min=%.4f max=%.4f avg=%.4f std=%.4f categories=%s",
            table_name,
            row["feature_name"],
            row["null_count"],
            row["min_value"],
            row["max_value"],
            row["avg_value"],
            row["stddev_value"],
            row["category_count"],
        )
    return len(summaries)


def save_confidence(conn, records: list[dict], created_at: str) -> None:
    if not records:
        return
    columns = [
        "race_id", "race_date", "venue", "race_no",
        "top1_car_no", "top2_car_no", "top3_car_no",
        *CONFIDENCE_COLUMNS,
        "confidence_stars", "created_at",
    ]
    placeholders = ", ".join("?" for _column in columns)
    update_sql = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "race_id")
    conn.executemany(
        f"""
        INSERT INTO race_confidence ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(race_id) DO UPDATE SET {update_sql}
        """,
        [tuple({**record, "created_at": created_at}[column] for column in columns) for record in records],
    )


def save_volatility(conn, records: list[dict], created_at: str) -> None:
    if not records:
        return
    columns = [
        "race_id", "race_date", "venue", "race_no",
        *VOLATILITY_COLUMNS,
        "volatility_bucket", "model_name", "created_at",
    ]
    placeholders = ", ".join("?" for _column in columns)
    update_sql = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "race_id")
    conn.executemany(
        f"""
        INSERT INTO race_volatility_features ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(race_id) DO UPDATE SET {update_sql}
        """,
        [tuple({**record, "created_at": created_at}[column] for column in columns) for record in records],
    )


def race_probability_signals(entries: list[dict], normalizer: FeatureScoreNormalizer, temperature: float) -> dict:
    scored = sorted(
        [{**entry, "feature_score": feature_prediction_score(entry, normalizer)} for entry in entries],
        key=lambda row: (row["feature_score"], value(row, "top3_minus_race_avg"), -int(row["car_no"])),
        reverse=True,
    )
    probabilities = softmax([row["feature_score"] for row in scored], temperature=temperature)
    ranked_probs = sorted(zip(scored, probabilities), key=lambda item: item[1], reverse=True)
    probs = [prob for _row, prob in ranked_probs]
    while len(probs) < 4:
        probs.append(0.0)
    line_stats = grouped_line_stats(entries)
    line_members = [float(row["members"]) for row in line_stats]
    strengths = sorted([row["strength"] for row in line_stats], reverse=True)
    line_strength_gap = strengths[0] - strengths[1] if len(strengths) > 1 else strengths[0] if strengths else 0.0
    line_member_variance = variance(line_members)
    line_count = len([row for row in line_stats if row["line_no"] > 0])
    tanki_count = sum(1 for row in line_stats if row["members"] == 1)
    max_line_members = int(max(line_members, default=0))
    race_entropy = entropy(probabilities)
    prob_variance = variance(probabilities)
    top_gap = probs[0] - probs[1]
    confidence_score = confidence_from_signals(
        probs[0],
        top_gap,
        prob_variance,
        race_entropy,
        line_strength_gap,
        max_line_members,
        line_count,
        tanki_count,
        line_member_variance,
    )
    return {
        "top1_probability": probs[0],
        "top1_top2_gap": top_gap,
        "top2_top3_gap": probs[1] - probs[2],
        "top3_top4_gap": probs[2] - probs[3],
        "probability_entropy": race_entropy,
        "probability_variance": prob_variance,
        "confidence_score": confidence_score,
    }


def evaluate_temperature_candidates(features_by_race: dict[str, list[dict]], normalizer: FeatureScoreNormalizer) -> tuple[float, list[dict]]:
    summaries = []
    race_entries = [entries for entries in features_by_race.values() if len(entries) >= 3]
    for temperature in TEMPERATURE_CANDIDATES:
        signals = [race_probability_signals(entries, normalizer, temperature) for entries in race_entries]
        top1_values = [row["top1_probability"] for row in signals]
        confidence_values = [row["confidence_score"] for row in signals]
        entropy_values = [row["probability_entropy"] for row in signals]
        top_gap_values = [row["top1_top2_gap"] for row in signals]
        high_08_ratio = avg([1.0 if value_ >= 0.8 else 0.0 for value_ in confidence_values])
        high_09_ratio = avg([1.0 if value_ >= 0.9 else 0.0 for value_ in confidence_values])
        natural_score = (
            abs(avg(top1_values) - 0.30)
            + abs(median(top1_values) - 0.28)
            + abs(avg(entropy_values) - 0.82) * 0.7
            + max(0.0, high_08_ratio - 0.35) * 1.4
            + high_09_ratio * 0.8
        )
        summaries.append({
            "temperature": temperature,
            "avg_top1_probability": avg(top1_values),
            "median_top1_probability": median(top1_values),
            "avg_top1_top2_gap": avg(top_gap_values),
            "avg_entropy": avg(entropy_values),
            "confidence_median": median(confidence_values),
            "confidence_high_08_ratio": high_08_ratio,
            "confidence_high_09_ratio": high_09_ratio,
            "natural_score": natural_score,
            "histogram": histogram(confidence_values),
        })
    selected = min(summaries, key=lambda row: row["natural_score"])["temperature"] if summaries else 8.0
    return selected, summaries


def summarize_values(values: list[float]) -> dict:
    return {
        "count": len(values),
        "min": min(values) if values else 0.0,
        "max": max(values) if values else 0.0,
        "avg": avg(values),
        "median": median(values),
        "stddev": stddev(values),
    }


def summarize_confidence_values(values: list[float]) -> dict:
    summary = summarize_values(values)
    summary.update({
        "histogram": histogram(values),
        "count_ge_09": sum(1 for item in values if item >= 0.9),
        "count_ge_08": sum(1 for item in values if item >= 0.8),
        "count_ge_07": sum(1 for item in values if item >= 0.7),
    })
    return summary


def confidence_summary_from_db(conn, start_date: str | None, end_date: str | None) -> dict:
    filters = []
    params = []
    if start_date:
        filters.append("race_date >= ?")
        params.append(start_date)
    if end_date:
        filters.append("race_date <= ?")
        params.append(end_date)
    where = "WHERE " + " AND ".join(filters) if filters else ""
    values = [float(row["confidence_score"] or 0) for row in rows(conn, f"SELECT confidence_score FROM race_confidence {where}", params)]
    return summarize_confidence_values(values)


def feature_score_analysis(features_by_race: dict[str, list[dict]], normalizer: FeatureScoreNormalizer) -> dict:
    scores = []
    top1_top2_gaps = []
    top2_top3_gaps = []
    top3_top4_gaps = []
    contribution_totals = {name: 0.0 for name, _weight in FEATURE_SCORE_WEIGHTS}
    for entries in features_by_race.values():
        race_scores = []
        for entry in entries:
            scores.append(feature_prediction_score(entry, normalizer))
            race_scores.append(feature_prediction_score(entry, normalizer))
            for name, contribution in normalizer.contributions(entry).items():
                contribution_totals[name] += abs(contribution)
        race_scores = sorted(race_scores, reverse=True)
        if len(race_scores) >= 2:
            top1_top2_gaps.append(race_scores[0] - race_scores[1])
        if len(race_scores) >= 3:
            top2_top3_gaps.append(race_scores[1] - race_scores[2])
        if len(race_scores) >= 4:
            top3_top4_gaps.append(race_scores[2] - race_scores[3])
    total_contribution = sum(contribution_totals.values()) or 1.0
    return {
        "feature_score": summarize_values(scores),
        "top1_top2_gap": summarize_values(top1_top2_gaps),
        "top2_top3_gap": summarize_values(top2_top3_gaps),
        "top3_top4_gap": summarize_values(top3_top4_gaps),
        "contribution_rate": {
            name: contribution_totals[name] / total_contribution for name, _weight in FEATURE_SCORE_WEIGHTS
        },
    }


def prediction_overall_summary(conn) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS predictions,
               ROUND(AVG(r.hit_1st) * 100, 3) AS first_rate,
               ROUND(AVG(r.hit_exact) * 100, 3) AS exact_rate,
               ROUND(AVG(r.hit_top3_count), 3) AS avg_top3_count,
               ROUND(SUM(r.return_amount) * 100.0 / NULLIF(SUM(r.stake_amount), 0), 3) AS roi
        FROM race_prediction p
        JOIN race_prediction_result r ON r.prediction_id = p.id
        WHERE p.prediction_type = 'feature_line_mix'
          AND COALESCE(p.sample_kind, 'live') = 'live'
        """
    ).fetchone()
    return {
        "predictions": int(row["predictions"] or 0),
        "first_rate": float(row["first_rate"] or 0),
        "exact_rate": float(row["exact_rate"] or 0),
        "avg_top3_count": float(row["avg_top3_count"] or 0),
        "roi": float(row["roi"] or 0),
    }


def confidence_calibration_rows(conn) -> list[dict]:
    query = """
        SELECT CASE
                 WHEN c.confidence_score >= 0.9 THEN '0.9-1.0'
                 WHEN c.confidence_score >= 0.8 THEN '0.8-0.9'
                 WHEN c.confidence_score >= 0.7 THEN '0.7-0.8'
                 WHEN c.confidence_score >= 0.6 THEN '0.6-0.7'
                 WHEN c.confidence_score >= 0.5 THEN '0.5-0.6'
                 WHEN c.confidence_score >= 0.4 THEN '0.4-0.5'
                 WHEN c.confidence_score >= 0.3 THEN '0.3-0.4'
                 WHEN c.confidence_score >= 0.2 THEN '0.2-0.3'
                 WHEN c.confidence_score >= 0.1 THEN '0.1-0.2'
                 ELSE '0.0-0.1'
               END AS bucket,
               COUNT(*) AS predictions,
               ROUND(AVG(r.hit_1st) * 100, 3) AS first_rate,
               ROUND(AVG(r.hit_top3_count), 3) AS avg_top3_count,
               ROUND(AVG(r.hit_exact) * 100, 3) AS exact_rate,
               ROUND(SUM(r.return_amount) * 100.0 / NULLIF(SUM(r.stake_amount), 0), 3) AS roi
        FROM race_prediction p
        JOIN race_prediction_result r ON r.prediction_id = p.id
        JOIN race_confidence c ON c.race_id = p.race_id
        WHERE p.prediction_type = 'feature_line_mix'
          AND COALESCE(p.sample_kind, 'live') = 'live'
        GROUP BY bucket
        ORDER BY bucket
    """
    return rows(conn, query)


def log_risk_report(report: dict) -> None:
    logging.info("feature_score_normalization method=%s clip=%.1f", NORMALIZATION_METHOD, Z_SCORE_CLIP)
    for name, stats in report.get("normalization_stats", {}).items():
        logging.info(
            "normalization_feature %s mean=%.6f std=%.6f min=%.6f max=%.6f",
            name,
            stats["mean"],
            stats["stddev"],
            stats["min"],
            stats["max"],
        )
    for row in report.get("temperature_candidates", []):
        logging.info(
            "temperature_candidate temp=%.0f avg_top1=%.4f median_top1=%.4f avg_gap=%.4f entropy=%.4f conf_median=%.4f ge08=%.4f ge09=%.4f score=%.4f hist=%s",
            row["temperature"],
            row["avg_top1_probability"],
            row["median_top1_probability"],
            row["avg_top1_top2_gap"],
            row["avg_entropy"],
            row["confidence_median"],
            row["confidence_high_08_ratio"],
            row["confidence_high_09_ratio"],
            row["natural_score"],
            row["histogram"],
        )
    logging.info("selected_temperature %.0f", report.get("selected_temperature", 0))
    logging.info("feature_score_analysis %s", report.get("feature_score_analysis"))
    logging.info("confidence_before %s", report.get("confidence_before"))
    logging.info("confidence_after %s", report.get("confidence_after"))
    logging.info("confidence_calibration %s", report.get("confidence_calibration"))
    logging.info("comparison %s", report.get("comparison"))


def build_race_risk_features(conn, start_date: str | None = None, end_date: str | None = None) -> dict:
    init_db(conn)
    filters = []
    params = []
    if start_date:
        filters.append("race_date >= ?")
        params.append(start_date)
    if end_date:
        filters.append("race_date <= ?")
        params.append(end_date)
    where = "WHERE " + " AND ".join(filters) if filters else ""
    races = rows(
        conn,
        f"""
        SELECT race_id, race_date, venue, race_no
        FROM race_schedule
        {where}
        ORDER BY race_date, venue, race_no
        """,
        params,
    )
    features_by_race: dict[str, list[dict]] = {}
    for row in rows(conn, "SELECT * FROM race_entry_features ORDER BY race_id, car_no"):
        features_by_race.setdefault(row["race_id"], []).append(row)
    target_race_ids = {race["race_id"] for race in races}
    target_features_by_race = {
        race_id: entries for race_id, entries in features_by_race.items() if race_id in target_race_ids and len(entries) >= 3
    }
    target_entries = [entry for entries in target_features_by_race.values() for entry in entries]
    normalizer = FeatureScoreNormalizer.from_entries(target_entries)
    selected_temperature, temperature_summaries = evaluate_temperature_candidates(target_features_by_race, normalizer)
    before_confidence = confidence_summary_from_db(conn, start_date, end_date)
    score_analysis = feature_score_analysis(target_features_by_race, normalizer)
    payouts = payout_by_race(conn)
    classes = race_classes(conn)
    threshold = payout_threshold(conn, start_date, end_date)
    confidence_records = []
    volatility_records = []
    skipped = 0
    for race in races:
        entries = features_by_race.get(race["race_id"], [])
        if len(entries) < 3:
            skipped += 1
            continue
        confidence, volatility = build_for_race(
            race,
            entries,
            payouts.get(race["race_id"], 0),
            threshold,
            classes.get(race["race_id"], []),
            normalizer,
            selected_temperature,
        )
        confidence_records.append(confidence)
        volatility_records.append(volatility)
    if start_date or end_date:
        delete_filters = []
        delete_params = []
        if start_date:
            delete_filters.append("race_date >= ?")
            delete_params.append(start_date)
        if end_date:
            delete_filters.append("race_date <= ?")
            delete_params.append(end_date)
        condition = " AND ".join(delete_filters)
        conn.execute(f"DELETE FROM race_confidence WHERE {condition}", delete_params)
        conn.execute(f"DELETE FROM race_volatility_features WHERE {condition}", delete_params)
    else:
        conn.execute("DELETE FROM race_confidence")
        conn.execute("DELETE FROM race_volatility_features")
    now = datetime.now(JST).isoformat(timespec="seconds")
    save_confidence(conn, confidence_records, now)
    used_lightgbm = apply_lightgbm_volatility(volatility_records)
    save_volatility(conn, volatility_records, now)
    confidence_quality = save_quality(conn, "race_confidence", CONFIDENCE_COLUMNS)
    volatility_quality = save_quality(conn, "race_volatility_features", VOLATILITY_COLUMNS)
    after_confidence = summarize_confidence_values([row["confidence_score"] for row in confidence_records])
    prediction_summary = prediction_overall_summary(conn)
    calibration_rows = confidence_calibration_rows(conn)
    before_count = before_confidence.get("count") or 0
    after_count = after_confidence.get("count") or 0
    comparison = {
        "confidence_median_before": before_confidence.get("median", 0),
        "confidence_median_after": after_confidence.get("median", 0),
        "confidence_median_delta": after_confidence.get("median", 0) - before_confidence.get("median", 0),
        "confidence_ge08_ratio_before": (before_confidence.get("count_ge_08", 0) / before_count) if before_count else 0.0,
        "confidence_ge08_ratio_after": (after_confidence.get("count_ge_08", 0) / after_count) if after_count else 0.0,
        "first_rate": prediction_summary["first_rate"],
        "exact_rate": prediction_summary["exact_rate"],
        "avg_top3_count": prediction_summary["avg_top3_count"],
        "roi": prediction_summary["roi"],
    }
    report = {
        "normalization_method": NORMALIZATION_METHOD,
        "normalization_stats": normalizer.stats,
        "selected_temperature": selected_temperature,
        "temperature_candidates": temperature_summaries,
        "feature_score_analysis": score_analysis,
        "confidence_before": before_confidence,
        "confidence_after": after_confidence,
        "confidence_calibration": calibration_rows,
        "prediction_summary": prediction_summary,
        "comparison": comparison,
    }
    log_risk_report(report)
    conn.commit()
    return {
        "races": len(races),
        "skipped_races": skipped,
        "confidence_rows": len(confidence_records),
        "volatility_rows": len(volatility_records),
        "payout_threshold": int(threshold),
        "quality_features": confidence_quality + volatility_quality,
        "model_name": LIGHTGBM_MODEL_NAME if used_lightgbm else MODEL_NAME,
        "normalization_method": NORMALIZATION_METHOD,
        "selected_temperature": selected_temperature,
        "confidence_before": before_confidence,
        "confidence_after": after_confidence,
        "feature_score_analysis": score_analysis,
        "confidence_calibration": calibration_rows,
        "comparison": comparison,
    }


def run(start_date: str | None = None, end_date: str | None = None) -> dict:
    setup_logging()
    with connect() as conn:
        return build_race_risk_features(conn, start_date=start_date, end_date=end_date)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build race confidence and volatility features")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    args = parser.parse_args()
    print(run(start_date=args.start_date, end_date=args.end_date))


if __name__ == "__main__":
    main()
