import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR, DB_PATH
from .lineup_validation import lineup_groups, normalize_lineup


SCHEMA = """
CREATE TABLE IF NOT EXISTS race_master (
    race_id TEXT PRIMARY KEY,
    race_date TEXT,
    venue TEXT,
    race_no INTEGER,
    event_name TEXT,
    race_title TEXT,
    race_class TEXT,
    start_time TEXT,
    deadline_time TEXT,
    status TEXT,
    distance INTEGER,
    laps INTEGER,
    weather TEXT,
    temperature REAL,
    wind_direction TEXT,
    wind_speed REAL,
    lineup_text TEXT,
    race_comment TEXT,
    dead_heat INTEGER DEFAULT 0,
    detail_url TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS race_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT,
    rank INTEGER,
    car_no INTEGER,
    racer_name TEXT,
    class TEXT,
    prefecture TEXT,
    age INTEGER,
    term INTEGER,
    margin TEXT,
    time TEXT,
    kimarite TEXT,
    start_mark TEXT,
    back_mark TEXT,
    FOREIGN KEY (race_id) REFERENCES race_master(race_id)
);

CREATE TABLE IF NOT EXISTS payout (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT,
    bet_type TEXT,
    combination TEXT,
    payout INTEGER,
    popularity INTEGER,
    FOREIGN KEY (race_id) REFERENCES race_master(race_id)
);

CREATE TABLE IF NOT EXISTS race_lineup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT,
    car_no INTEGER,
    line_no INTEGER,
    line_position INTEGER,
    FOREIGN KEY (race_id) REFERENCES race_master(race_id)
);

CREATE TABLE IF NOT EXISTS race_schedule (
    race_id TEXT PRIMARY KEY,
    race_date TEXT,
    venue TEXT,
    race_no INTEGER,
    event_name TEXT,
    race_title TEXT,
    race_class TEXT,
    start_time TEXT,
    deadline_time TEXT,
    status TEXT,
    distance INTEGER,
    laps INTEGER,
    weather TEXT,
    temperature REAL,
    wind_direction TEXT,
    wind_speed REAL,
    lineup_text TEXT,
    detail_url TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS race_entry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT,
    car_no INTEGER,
    racer_name TEXT,
    class TEXT,
    prefecture TEXT,
    age INTEGER,
    term INTEGER,
    gear_ratio REAL,
    leg_type TEXT,
    score REAL,
    win_rate REAL,
    quinella_rate REAL,
    trifecta_rate REAL,
    comment TEXT,
    FOREIGN KEY (race_id) REFERENCES race_schedule(race_id)
);

CREATE TABLE IF NOT EXISTS race_lineup_forecast (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT,
    car_no INTEGER,
    line_no INTEGER,
    line_position INTEGER,
    FOREIGN KEY (race_id) REFERENCES race_schedule(race_id)
);

CREATE TABLE IF NOT EXISTS race_prediction (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT,
    race_date TEXT,
    prediction_type TEXT,
    predicted_1st INTEGER,
    predicted_2nd INTEGER,
    predicted_3rd INTEGER,
    confidence TEXT,
    score REAL,
    reason_text TEXT,
    score_detail_text TEXT,
    score_detail_json TEXT,
    model_version TEXT,
    stake_amount INTEGER DEFAULT 100,
    created_at TEXT,
    UNIQUE (race_id, prediction_type),
    FOREIGN KEY (race_id) REFERENCES race_schedule(race_id)
);

CREATE TABLE IF NOT EXISTS race_prediction_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER UNIQUE,
    race_id TEXT,
    actual_1st INTEGER,
    actual_2nd INTEGER,
    actual_3rd INTEGER,
    actual_1st_candidates TEXT,
    actual_2nd_candidates TEXT,
    actual_3rd_candidates TEXT,
    dead_heat INTEGER DEFAULT 0,
    hit_exact INTEGER,
    hit_1st INTEGER,
    hit_top2 INTEGER,
    hit_top3_count INTEGER,
    payout INTEGER,
    stake_amount INTEGER,
    return_amount INTEGER,
    roi REAL,
    checked_at TEXT,
    FOREIGN KEY (prediction_id) REFERENCES race_prediction(id)
);

CREATE TABLE IF NOT EXISTS race_prediction_bet (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER,
    race_id TEXT,
    race_date TEXT,
    prediction_type TEXT,
    bet_type TEXT,
    combination TEXT,
    stake_amount INTEGER DEFAULT 100,
    created_at TEXT,
    UNIQUE (prediction_id, bet_type, combination),
    FOREIGN KEY (prediction_id) REFERENCES race_prediction(id)
);

CREATE TABLE IF NOT EXISTS race_prediction_bet_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_bet_id INTEGER UNIQUE,
    race_id TEXT,
    hit INTEGER,
    payout INTEGER,
    stake_amount INTEGER,
    return_amount INTEGER,
    roi REAL,
    checked_at TEXT,
    FOREIGN KEY (prediction_bet_id) REFERENCES race_prediction_bet(id)
);

CREATE TABLE IF NOT EXISTS race_line_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT,
    race_date TEXT,
    venue TEXT,
    race_no INTEGER,
    car_no INTEGER,
    racer_name TEXT,
    prefecture TEXT,
    term INTEGER,
    rank INTEGER,
    line_no INTEGER,
    line_size INTEGER,
    line_position INTEGER,
    position_label TEXT,
    followers INTEGER,
    is_leader INTEGER,
    is_tanki INTEGER,
    is_max_line INTEGER,
    starter_count INTEGER,
    line_count INTEGER,
    bunsen_count INTEGER,
    tanki_count INTEGER,
    max_line_size INTEGER,
    parse_status TEXT,
    source_lineup_text TEXT,
    created_at TEXT,
    UNIQUE (race_id, car_no),
    FOREIGN KEY (race_id) REFERENCES race_master(race_id)
);

CREATE TABLE IF NOT EXISTS racer_line_condition_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    racer_name TEXT,
    prefecture TEXT,
    term INTEGER,
    condition_type TEXT,
    condition_key TEXT,
    line_position INTEGER,
    position_label TEXT,
    followers INTEGER,
    bunsen_count INTEGER,
    line_size INTEGER,
    is_tanki INTEGER,
    is_max_line INTEGER,
    races INTEGER,
    wins INTEGER,
    seconds INTEGER,
    thirds INTEGER,
    top2 INTEGER,
    top3 INTEGER,
    win_rate REAL,
    top2_rate REAL,
    top3_rate REAL,
    min_race_date TEXT,
    max_race_date TEXT,
    sample_race_ids TEXT,
    updated_at TEXT,
    UNIQUE (racer_name, prefecture, term, condition_type, condition_key)
);

CREATE INDEX IF NOT EXISTS idx_race_line_features_date
    ON race_line_features(race_date);
CREATE INDEX IF NOT EXISTS idx_race_line_features_racer
    ON race_line_features(racer_name, prefecture, term);
CREATE INDEX IF NOT EXISTS idx_racer_line_condition_stats_lookup
    ON racer_line_condition_stats(
        racer_name, prefecture, term, condition_type, line_position,
        followers, bunsen_count
    );
"""


@contextmanager
def connect(db_path: Path = DB_PATH):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for column, column_type in {
        "event_name": "TEXT",
        "race_title": "TEXT",
        "race_class": "TEXT",
        "start_time": "TEXT",
        "deadline_time": "TEXT",
        "status": "TEXT",
        "distance": "INTEGER",
        "laps": "INTEGER",
        "weather": "TEXT",
        "temperature": "REAL",
        "wind_direction": "TEXT",
        "wind_speed": "REAL",
        "lineup_text": "TEXT",
        "race_comment": "TEXT",
        "dead_heat": "INTEGER DEFAULT 0",
    }.items():
        ensure_column(conn, "race_master", column, column_type)
    conn.execute(
        """
        UPDATE race_master
        SET dead_heat = CASE WHEN EXISTS (
            SELECT 1
            FROM race_result r
            WHERE r.race_id = race_master.race_id
            GROUP BY r.rank
            HAVING COUNT(*) > 1
        ) THEN 1 ELSE 0 END
        """
    )
    for column, column_type in {
        "term": "INTEGER",
        "margin": "TEXT",
        "start_mark": "TEXT",
        "back_mark": "TEXT",
    }.items():
        ensure_column(conn, "race_result", column, column_type)
    ensure_column(conn, "payout", "popularity", "INTEGER")
    for column, column_type in {
        "gear_ratio": "REAL",
        "leg_type": "TEXT",
        "score": "REAL",
        "win_rate": "REAL",
        "quinella_rate": "REAL",
        "trifecta_rate": "REAL",
        "comment": "TEXT",
    }.items():
        ensure_column(conn, "race_entry", column, column_type)
    for column, column_type in {
        "score_detail_text": "TEXT",
        "score_detail_json": "TEXT",
        "model_version": "TEXT",
        "sample_kind": "TEXT DEFAULT 'live'",
    }.items():
        ensure_column(conn, "race_prediction", column, column_type)
    for column, column_type in {
        "actual_1st_candidates": "TEXT",
        "actual_2nd_candidates": "TEXT",
        "actual_3rd_candidates": "TEXT",
        "dead_heat": "INTEGER DEFAULT 0",
    }.items():
        ensure_column(conn, "race_prediction_result", column, column_type)
    conn.execute(
        """
        UPDATE race_prediction
        SET sample_kind = 'reference'
        WHERE DATE(created_at) > race_date
          AND COALESCE(sample_kind, 'live') = 'live'
        """
    )
    conn.execute(
        "UPDATE race_prediction SET sample_kind = 'live' WHERE sample_kind IS NULL OR sample_kind = ''"
    )
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def race_exists(conn: sqlite3.Connection, race_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM race_master WHERE race_id = ? LIMIT 1",
        (race_id,),
    ).fetchone()
    return row is not None


def save_race(conn: sqlite3.Connection, race: dict, results: list[dict], payouts: list[dict]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    try:
        conn.execute(
            """
            INSERT INTO race_master
                (
                    race_id, race_date, venue, race_no, event_name, race_title,
                    race_class, start_time, deadline_time, status, distance,
                    laps, weather, temperature, wind_direction, wind_speed,
                    lineup_text, race_comment, dead_heat, detail_url, created_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                race["race_id"],
                race["race_date"],
                race["venue"],
                race["race_no"],
                race.get("event_name"),
                race.get("race_title"),
                race.get("race_class"),
                race.get("start_time"),
                race.get("deadline_time"),
                race.get("status"),
                race.get("distance"),
                race.get("laps"),
                race.get("weather"),
                race.get("temperature"),
                race.get("wind_direction"),
                race.get("wind_speed"),
                race.get("lineup_text"),
                race.get("race_comment"),
                1 if len([item["rank"] for item in results if item.get("rank") is not None])
                != len({item["rank"] for item in results if item.get("rank") is not None}) else 0,
                race["detail_url"],
                now,
            ),
        )

        conn.executemany(
            """
            INSERT INTO race_result
                (
                    race_id, rank, car_no, racer_name, class, prefecture,
                    age, term, margin, time, kimarite, start_mark, back_mark
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    race["race_id"],
                    item.get("rank"),
                    item.get("car_no"),
                    item.get("racer_name"),
                    item.get("class"),
                    item.get("prefecture"),
                    item.get("age"),
                    item.get("term"),
                    item.get("margin"),
                    item.get("time"),
                    item.get("kimarite"),
                    item.get("start_mark"),
                    item.get("back_mark"),
                )
                for item in results
            ],
        )

        conn.executemany(
            """
            INSERT INTO payout
                (race_id, bet_type, combination, payout, popularity)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    race["race_id"],
                    item.get("bet_type"),
                    item.get("combination"),
                    item.get("payout"),
                    item.get("popularity"),
                )
                for item in payouts
            ],
        )

        conn.executemany(
            """
            INSERT INTO race_lineup
                (race_id, car_no, line_no, line_position)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    race["race_id"],
                    item.get("car_no"),
                    item.get("line_no"),
                    item.get("line_position"),
                )
                for item in race.get("lineup", [])
            ],
        )

        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def save_schedule(conn: sqlite3.Connection, race: dict, entries: list[dict]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    entry_car_nos = {
        int(item["car_no"])
        for item in entries
        if item.get("car_no") is not None
    }
    lineup = normalize_lineup(race.get("lineup", []), entry_car_nos)
    lineup_text = (
        " / ".join(
            " ".join(str(item["car_no"]) for item in group)
            for group in lineup_groups(lineup)
        )
        if lineup
        else None
    )
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO race_schedule
                (
                    race_id, race_date, venue, race_no, event_name, race_title,
                    race_class, start_time, deadline_time, status, distance,
                    laps, weather, temperature, wind_direction, wind_speed,
                    lineup_text, detail_url, created_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                race["race_id"],
                race["race_date"],
                race["venue"],
                race["race_no"],
                race.get("event_name"),
                race.get("race_title"),
                race.get("race_class"),
                race.get("start_time"),
                race.get("deadline_time"),
                race.get("status"),
                race.get("distance"),
                race.get("laps"),
                race.get("weather"),
                race.get("temperature"),
                race.get("wind_direction"),
                race.get("wind_speed"),
                lineup_text,
                race["detail_url"],
                now,
            ),
        )
        conn.execute("DELETE FROM race_entry WHERE race_id = ?", (race["race_id"],))
        conn.execute("DELETE FROM race_lineup_forecast WHERE race_id = ?", (race["race_id"],))
        conn.executemany(
            """
            INSERT INTO race_entry
                (
                    race_id, car_no, racer_name, class, prefecture, age, term,
                    gear_ratio, leg_type, score, win_rate, quinella_rate,
                    trifecta_rate, comment
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    race["race_id"],
                    item.get("car_no"),
                    item.get("racer_name"),
                    item.get("class"),
                    item.get("prefecture"),
                    item.get("age"),
                    item.get("term"),
                    item.get("gear_ratio"),
                    item.get("leg_type"),
                    item.get("score"),
                    item.get("win_rate"),
                    item.get("quinella_rate"),
                    item.get("trifecta_rate"),
                    item.get("comment"),
                )
                for item in entries
            ],
        )
        conn.executemany(
            """
            INSERT INTO race_lineup_forecast
                (race_id, car_no, line_no, line_position)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    race["race_id"],
                    item.get("car_no"),
                    item.get("line_no"),
                    item.get("line_position"),
                )
                for item in lineup
            ],
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
