"""Owner: metrics member. SQLite records and per-session aggregation.

- Plain sqlite3, parameterized queries only, one short-lived connection per call.
- request_id is the primary key, so recording a result twice never double-counts.
- impact/baseline/savings are recomputed from the attempts with
-metrics.calculate_metrics, so stored numbers cannot disagree with the attempts.
- Database path: the DATABASE_PATH environment variable (same name as
  backend/config.py), default "greenprompt.db". Generated *.db files are gitignored.
"""
import json
import os
import sqlite3

from backend.metrics import TIER_ESTIMATES, calculate_metrics

DEFAULT_DATABASE_PATH = "greenprompt.db"
QUALITY_STATUSES = ("passed", "failed", "unchecked")
CALL_STATUSES = ("success", "error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id        TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    prompt            TEXT NOT NULL,
    mode              TEXT,
    difficulty        TEXT,
    initial_model     TEXT NOT NULL,
    final_model       TEXT NOT NULL,
    answer            TEXT NOT NULL,
    quality_status    TEXT,
    quality_reason    TEXT,
    escalated         INTEGER NOT NULL,
    impact_energy_wh  REAL NOT NULL, impact_co2_g  REAL NOT NULL,
    impact_water_ml   REAL NOT NULL, impact_cost_inr  REAL NOT NULL,
    base_energy_wh    REAL NOT NULL, base_co2_g    REAL NOT NULL,
    base_water_ml     REAL NOT NULL, base_cost_inr    REAL NOT NULL,
    sav_energy_wh     REAL NOT NULL, sav_co2_g     REAL NOT NULL,
    sav_water_ml      REAL NOT NULL, sav_cost_inr     REAL NOT NULL,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id);

CREATE TABLE IF NOT EXISTS attempts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     TEXT NOT NULL REFERENCES requests(request_id),
    position       INTEGER NOT NULL,
    tier           TEXT NOT NULL,
    model_name     TEXT,
    status         TEXT,
    latency_ms     INTEGER,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    quality_status TEXT,
    quality_reason TEXT
);

-- Requests that returned no answer: logged for debugging, never in summaries.
CREATE TABLE IF NOT EXISTS failed_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT,
    session_id  TEXT,
    prompt      TEXT,
    mode        TEXT,
    error       TEXT,
    attempts    TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def get_database_path(db_path=None) -> str:
    """Explicit argument wins, then DATABASE_PATH, then the default. Read per call."""
    return str(db_path or os.getenv("DATABASE_PATH") or DEFAULT_DATABASE_PATH)


def _connect(db_path=None) -> sqlite3.Connection:
    path = get_database_path(db_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _field(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _quality_parts(quality):
    """Contract shape is {"status", "reason"}; a bare status string is tolerated."""
    if quality is None:
        return None, None
    if isinstance(quality, str):
        return quality, None
    return _field(quality, "status"), _field(quality, "reason")


def _validate(record) -> None:
    for key in ("request_id", "session_id", "prompt", "initial_model", "final_model", "attempts"):
        if _field(record, key) in (None, ""):
            raise ValueError(f"record is missing required field {key!r}")
    if _field(record, "answer") is None:
        raise ValueError("record has no answer; requests without an answer are not recorded here "
                         "(use log_failed_request)")
    for key in ("initial_model", "final_model"):
        if _field(record, key) not in TIER_ESTIMATES:
            raise ValueError(f"{key} must be one of {sorted(TIER_ESTIMATES)}")
    status, _ = _quality_parts(_field(record, "quality"))
    if status is not None and status not in QUALITY_STATUSES:
        raise ValueError(f"quality status must be one of {QUALITY_STATUSES}, got {status!r}")
    for index, attempt in enumerate(_field(record, "attempts")):
        call_status = _field(attempt, "status")
        if call_status is not None and call_status not in CALL_STATUSES:
            raise ValueError(f"attempt {index}: status must be one of {CALL_STATUSES}")
        q_status = _field(attempt, "quality_status")
        if q_status is not None and q_status not in QUALITY_STATUSES:
            raise ValueError(f"attempt {index}: quality_status must be one of {QUALITY_STATUSES}")


def _session_summary(conn: sqlite3.Connection, session_id: str) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*)                                AS n,
               COALESCE(SUM(final_model = 'small'), 0) AS small_n,
               COALESCE(SUM(escalated), 0)             AS esc,
               COALESCE(SUM(sav_energy_wh), 0.0)       AS energy_wh,
               COALESCE(SUM(sav_co2_g), 0.0)           AS co2_g,
               COALESCE(SUM(sav_water_ml), 0.0)        AS water_ml,
               COALESCE(SUM(sav_cost_inr), 0.0)        AS cost_inr
        FROM requests WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    total = row["n"]
    return {
        "total_prompts": total,
        "small_model_percentage": (100.0 * row["small_n"] / total) if total else 0,
        "escalations": row["esc"],
        "cumulative_savings": {
            "energy_wh": row["energy_wh"],
            "co2_g": row["co2_g"],
            "water_ml": row["water_ml"],
            "cost_inr": row["cost_inr"],
        },
    }


def record_result(record: dict, db_path=None) -> dict:
    """Store once by request_id and return this session's updated summary.

    Re-recording an existing request_id changes nothing and returns the current summary.
    Raises ValueError for malformed records; nothing is written in that case.
    """
    _validate(record)
    attempts = list(_field(record, "attempts"))
    metrics = calculate_metrics(attempts)
    impact, baseline, savings = metrics["impact"], metrics["baseline"], metrics["savings"]
    q_status, q_reason = _quality_parts(_field(record, "quality"))
    request_id = _field(record, "request_id")
    session_id = _field(record, "session_id")

    conn = _connect(db_path)
    try:
        with conn:  # single transaction: commit on success, roll back on error
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO requests (
                    request_id, session_id, prompt, mode, difficulty, initial_model,
                    final_model, answer, quality_status, quality_reason, escalated,
                    impact_energy_wh, impact_co2_g, impact_water_ml, impact_cost_inr,
                    base_energy_wh, base_co2_g, base_water_ml, base_cost_inr,
                    sav_energy_wh, sav_co2_g, sav_water_ml, sav_cost_inr
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?)
                """,
                (
                    request_id, session_id, _field(record, "prompt"), _field(record, "mode"),
                    _field(record, "difficulty"), _field(record, "initial_model"),
                    _field(record, "final_model"), _field(record, "answer"),
                    q_status, q_reason, 1 if _field(record, "escalated") else 0,
                    impact["energy_wh"], impact["co2_g"], impact["water_ml"], impact["cost_inr"],
                    baseline["energy_wh"], baseline["co2_g"], baseline["water_ml"], baseline["cost_inr"],
                    savings["energy_wh"], savings["co2_g"], savings["water_ml"], savings["cost_inr"],
                ),
            )
            if cursor.rowcount == 1:  # first time this request_id is seen
                conn.executemany(
                    """
                    INSERT INTO attempts (request_id, position, tier, model_name, status,
                        latency_ms, input_tokens, output_tokens, quality_status, quality_reason)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            request_id, position, _field(a, "tier"), _field(a, "model_name"),
                            _field(a, "status"), _field(a, "latency_ms"), _field(a, "input_tokens"),
                            _field(a, "output_tokens"), _field(a, "quality_status"),
                            _field(a, "quality_reason"),
                        )
                        for position, a in enumerate(attempts)
                    ],
                )
        return _session_summary(conn, session_id)
    finally:
        conn.close()


def get_session_summary(session_id: str, db_path=None) -> dict:
    """Summary for one session; zeros if the session has no recorded prompts."""
    conn = _connect(db_path)
    try:
        return _session_summary(conn, session_id)
    finally:
        conn.close()


def get_request(request_id: str, db_path=None):
    """Return one stored request with its ordered attempts, or None. For integration checks."""
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            return None
        attempts = conn.execute(
            "SELECT * FROM attempts WHERE request_id = ? ORDER BY position", (request_id,)
        ).fetchall()
        result = dict(row)
        result["attempts"] = [dict(a) for a in attempts]
        return result
    finally:
        conn.close()


def log_failed_request(session_id, prompt, mode, error, attempts=None, request_id=None, db_path=None) -> None:
    """Log a request that returned no answer. Never counted in session summaries."""
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO failed_requests (request_id, session_id, prompt, mode, error, attempts)"
                " VALUES (?,?,?,?,?,?)",
                (request_id, session_id, prompt, mode, error, json.dumps(attempts or [])),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Extra session analytics and export (additive; NOT part of the v1 API contract).
# ---------------------------------------------------------------------------
import csv  # noqa: E402  (kept next to the helpers that use it)

from backend.metrics import split_useful_and_wasted  # noqa: E402


def get_session_details(session_id: str, db_path=None) -> dict:
    """Richer per-session statistics. Same inclusion rules as the session summary."""
    conn = _connect(db_path)
    try:
        requests = conn.execute(
            """SELECT request_id, final_model, quality_status, escalated,
                      impact_energy_wh, base_energy_wh, sav_energy_wh
               FROM requests WHERE session_id = ?""", (session_id,)).fetchall()
        attempts = conn.execute(
            """SELECT a.request_id, a.tier, a.status, a.latency_ms
               FROM attempts a JOIN requests r ON r.request_id = a.request_id
               WHERE r.session_id = ? ORDER BY a.request_id, a.position""", (session_id,)).fetchall()
    finally:
        conn.close()

    total = len(requests)
    by_request = {}
    for row in attempts:
        by_request.setdefault(row["request_id"], []).append(dict(row))

    final_counts = {tier: 0 for tier in TIER_ESTIMATES}
    quality_counts = {"passed": 0, "failed": 0, "unchecked": 0, "unknown": 0}
    used = baseline = saved = wasted = 0.0
    escalated = 0
    for row in requests:
        final_counts[row["final_model"]] += 1
        quality_counts[row["quality_status"] if row["quality_status"] in QUALITY_STATUSES else "unknown"] += 1
        escalated += row["escalated"]
        used += row["impact_energy_wh"]
        baseline += row["base_energy_wh"]
        saved += row["sav_energy_wh"]
        split = split_useful_and_wasted(
            {"final_model": row["final_model"], "attempts": by_request.get(row["request_id"], [])})
        wasted += split["wasted"]["energy_wh"]

    latencies = {tier: [] for tier in TIER_ESTIMATES}
    for row in attempts:
        if row["status"] == "success" and row["latency_ms"] is not None:
            latencies[row["tier"]].append(row["latency_ms"])

    return {
        "total_prompts": total,
        "final_model_counts": final_counts,
        "quality_counts": quality_counts,
        "escalation_rate_percentage": (100.0 * escalated / total) if total else 0,
        "total_attempts": len(attempts),
        "average_attempts_per_prompt": (len(attempts) / total) if total else 0,
        "error_attempts": sum(1 for a in attempts if a["status"] == "error"),
        "energy_used_wh": used,
        "baseline_energy_wh": baseline,
        "energy_saved_wh": saved,
        "wasted_energy_wh": wasted,
        "wasted_share_percentage": (100.0 * wasted / used) if used else 0,
        "average_latency_ms_by_tier": {
            tier: (sum(v) / len(v) if v else None) for tier, v in latencies.items()},
    }


def _csv_safe(value):
    """Stop spreadsheet apps from running a prompt that starts with = + - @ as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


CSV_COLUMNS = (
    "request_id", "prompt", "mode", "difficulty", "initial_model", "final_model", "quality_status",
    "escalated", "attempt_path", "energy_used_wh", "baseline_energy_wh", "energy_savings_wh",
    "co2_savings_g", "water_savings_ml", "cost_savings_inr", "created_at",
)


def export_session_csv(session_id: str, path, db_path=None) -> int:
    """Write one row per recorded request to a CSV file (backup-demo evidence). Returns row count.

    Values are illustrative estimates. Attempt path marks provider errors with '!', e.g. small!>medium.
    """
    conn = _connect(db_path)
    try:
        requests = conn.execute(
            "SELECT * FROM requests WHERE session_id = ? ORDER BY created_at, rowid", (session_id,)).fetchall()
        paths = {}
        for row in conn.execute(
                """SELECT a.request_id, a.tier, a.status FROM attempts a
                   JOIN requests r ON r.request_id = a.request_id
                   WHERE r.session_id = ? ORDER BY a.request_id, a.position""", (session_id,)):
            paths.setdefault(row["request_id"], []).append(row["tier"] + ("!" if row["status"] == "error" else ""))
    finally:
        conn.close()

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for r in requests:
            writer.writerow([_csv_safe(v) for v in (
                r["request_id"], r["prompt"], r["mode"], r["difficulty"], r["initial_model"],
                r["final_model"], r["quality_status"], r["escalated"],
                ">".join(paths.get(r["request_id"], [])), r["impact_energy_wh"], r["base_energy_wh"],
                r["sav_energy_wh"], r["sav_co2_g"], r["sav_water_ml"], r["sav_cost_inr"], r["created_at"])])
    return len(requests)

