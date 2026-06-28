import argparse
import json
import math
from datetime import datetime, timezone, timedelta

from .db import connect, init_db
from .entry_features import FEATURE_COLUMNS


JST = timezone(timedelta(hours=9))


def rows(conn, sql: str, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def gini(labels: list[int]) -> float:
    if not labels:
        return 0.0
    p = sum(labels) / len(labels)
    return 1.0 - p * p - (1.0 - p) * (1.0 - p)


def best_split_gain(values: list[float], labels: list[int]) -> tuple[float, int]:
    pairs = sorted(zip(values, labels), key=lambda item: item[0])
    unique_values = sorted({value for value, _label in pairs})
    if len(unique_values) < 2:
        return 0.0, 0
    if len(unique_values) > 80:
        step = max(1, len(unique_values) // 80)
        thresholds = unique_values[step::step]
    else:
        thresholds = [(unique_values[index - 1] + unique_values[index]) / 2 for index in range(1, len(unique_values))]
    base = gini(labels)
    best = 0.0
    split_count = 0
    for threshold in thresholds:
        left = [label for value, label in pairs if value <= threshold]
        right = [label for value, label in pairs if value > threshold]
        if not left or not right:
            continue
        split_count += 1
        weighted = (len(left) / len(labels)) * gini(left) + (len(right) / len(labels)) * gini(right)
        best = max(best, base - weighted)
    return best, split_count


def auc(values: list[float], labels: list[int]) -> float:
    positives = [(value, label) for value, label in zip(values, labels) if label == 1]
    negatives = [(value, label) for value, label in zip(values, labels) if label == 0]
    if not positives or not negatives:
        return 0.5
    wins = 0.0
    for pos_value, _ in positives:
        for neg_value, _ in negatives:
            if pos_value > neg_value:
                wins += 1.0
            elif pos_value == neg_value:
                wins += 0.5
    return wins / (len(positives) * len(negatives))


def pearson_abs(values: list[float], labels: list[int]) -> float:
    if not values:
        return 0.0
    value_avg = sum(values) / len(values)
    label_avg = sum(labels) / len(labels)
    value_var = sum((value - value_avg) ** 2 for value in values)
    label_var = sum((label - label_avg) ** 2 for label in labels)
    if value_var <= 0 or label_var <= 0:
        return 0.0
    cov = sum((value - value_avg) * (label - label_avg) for value, label in zip(values, labels))
    return abs(cov / math.sqrt(value_var * label_var))


def shap_proxy(values: list[float], labels: list[int]) -> float:
    if not values:
        return 0.0
    corr = pearson_abs(values, labels)
    avg = sum(values) / len(values)
    return sum(abs(value - avg) * corr for value in values) / len(values)


def permutation_proxy(values: list[float], labels: list[int]) -> float:
    return abs(auc(values, labels) - 0.5)


def load_dataset(conn, target_name: str, start_date: str | None, end_date: str | None) -> list[dict]:
    filters = [f"{target_name} IN (0, 1)"]
    params = []
    if start_date:
        filters.append("race_date >= ?")
        params.append(start_date)
    if end_date:
        filters.append("race_date <= ?")
        params.append(end_date)
    return rows(
        conn,
        f"""
        SELECT {target_name}, {", ".join(FEATURE_COLUMNS)}
        FROM race_entry_features
        WHERE {" AND ".join(filters)}
        """,
        params,
    )


def compute_importance(conn, target_name: str = "is_top3", start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    init_db(conn)
    dataset = load_dataset(conn, target_name, start_date, end_date)
    labels = [int(row[target_name]) for row in dataset]
    now = datetime.now(JST).isoformat(timespec="seconds")
    results = []
    for feature in FEATURE_COLUMNS:
        values = [float(row[feature] or 0) for row in dataset]
        gain, split = best_split_gain(values, labels)
        results.append({
            "target_name": target_name,
            "feature_name": feature,
            "gain": round(gain, 8),
            "split": split,
            "permutation_importance": round(permutation_proxy(values, labels), 8),
            "shap_importance": round(shap_proxy(values, labels), 8),
            "sample_count": len(dataset),
            "created_at": now,
        })
    conn.executemany(
        """
        INSERT INTO feature_importance
            (
                target_name, feature_name, gain, split,
                permutation_importance, shap_importance, sample_count, created_at
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_name, feature_name) DO UPDATE SET
            gain = excluded.gain,
            split = excluded.split,
            permutation_importance = excluded.permutation_importance,
            shap_importance = excluded.shap_importance,
            sample_count = excluded.sample_count,
            created_at = excluded.created_at
        """,
        [
            (
                row["target_name"],
                row["feature_name"],
                row["gain"],
                row["split"],
                row["permutation_importance"],
                row["shap_importance"],
                row["sample_count"],
                row["created_at"],
            )
            for row in results
        ],
    )
    conn.commit()
    return sorted(results, key=lambda row: (row["gain"], row["permutation_importance"]), reverse=True)


def run(target_name: str = "is_top3", start_date: str | None = None, end_date: str | None = None, limit: int = 30) -> list[dict]:
    with connect() as conn:
        return compute_importance(conn, target_name=target_name, start_date=start_date, end_date=end_date)[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute feature importance for race_entry_features")
    parser.add_argument("--target", default="is_top3")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    print(json.dumps(run(args.target, args.start_date, args.end_date, args.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
