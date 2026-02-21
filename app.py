#!/usr/bin/env python3
"""Simple QR scanning web app."""

import csv
import io
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, render_template_string, request


def resolve_db_path() -> str:
    configured = os.environ.get("DB_PATH", "").strip()
    if configured:
        return configured
    if os.environ.get("RENDER", "").lower() == "true":
        return "/tmp/qr_scans.db"
    # On hosted runtimes (Render and similar), PORT is injected and the
    # app directory may be read-only. Default to /tmp for SQLite writes.
    if os.environ.get("PORT"):
        return "/tmp/qr_scans.db"
    return "qr_scans.db"


DB_PATH = resolve_db_path()
DOOR_2_TIMEOUT_SECONDS = 20

app = Flask(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_iso(value: str):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_iso_utc_to_sgt(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        dt_utc = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return raw
    dt_sgt = dt_utc + timedelta(hours=8)
    month_abbr = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )[dt_sgt.month - 1]
    return f"{dt_sgt.day:02d}-{month_abbr}-{dt_sgt.year:04d} {dt_sgt:%H:%M:%S} SGT"


def normalize_match_value(value: str) -> str:
    # Normalize scanner payloads and configured door values so matching is robust.
    normalized = " ".join(str(value or "").split()).upper()
    # Normalize common unicode dash variants to ASCII hyphen.
    return normalized.translate(
        {
            ord("\u2010"): "-",
            ord("\u2011"): "-",
            ord("\u2012"): "-",
            ord("\u2013"): "-",
            ord("\u2014"): "-",
            ord("\u2015"): "-",
            ord("\u2212"): "-",
        }
    )


def build_match_candidates(value: str):
    base = normalize_match_value(value)
    if not base:
        return []

    forms = {base}

    def add_numeric_variants(token: str):
        normalized = normalize_match_value(token)
        if not normalized or not re.fullmatch(r"\d+", normalized):
            return
        canonical = str(int(normalized))
        forms.add(canonical)
        forms.add(canonical.zfill(2))
        forms.add(canonical.zfill(3))

    compact_dash = re.sub(r"\s*-\s*", "-", base)
    forms.add(compact_dash)
    forms.add(compact_dash.replace("-", " - "))

    if "-" in base:
        parts = [part.strip() for part in base.split("-") if part.strip()]
        forms.update(parts)
        if len(parts) >= 2:
            forms.add(parts[-1])
        for part in parts:
            add_numeric_variants(part)

    door_match = re.search(r"DOOR\s*([A-Z0-9]+)", base)
    if door_match:
        number = door_match.group(1)
        forms.add(f"DOOR {number}")
        forms.add(f"DOOR{number}")
        forms.add(number)
        if re.fullmatch(r"\d+", number):
            canonical = str(int(number))
            for variant in (canonical, canonical.zfill(2), canonical.zfill(3)):
                forms.add(variant)
                forms.add(f"DOOR {variant}")
                forms.add(f"DOOR{variant}")

    tail_match = re.search(r"([A-Z0-9]+)$", base)
    if tail_match:
        tail = tail_match.group(1)
        forms.add(tail)
        add_numeric_variants(tail)

    expanded = set()
    for item in forms:
        normalized_item = normalize_match_value(item)
        if normalized_item:
            expanded.add(normalized_item)
            expanded.add(normalized_item.replace(" ", ""))

    return sorted(expanded)


def build_gate_hints(scanned_qr: str):
    base = normalize_match_value(scanned_qr)
    if not base:
        return []

    hints = set()
    parts = [normalize_match_value(part) for part in re.split(r"\s*-\s*", base) if normalize_match_value(part)]
    if parts:
        first = parts[0]
        if first and not first.startswith("DOOR"):
            gate_part_match = re.match(r"^GATE\s*([A-Z0-9]+)$", first)
            if gate_part_match:
                gate_suffix = normalize_match_value(gate_part_match.group(1)).replace(" ", "")
                if gate_suffix:
                    hints.add(f"G{gate_suffix}")
                    hints.add(f"GATE{gate_suffix}")
                    hints.add(f"GATE {gate_suffix}")
                    hints.add(gate_suffix)
            elif re.match(r"^[A-Z]{1,6}\d[A-Z0-9]*$", first):
                hints.add(first)

    for token in re.findall(r"\b[A-Z]{1,6}\d[A-Z0-9]*\b", base):
        normalized_token = normalize_match_value(token)
        if normalized_token.startswith("DOOR"):
            continue
        hints.add(normalized_token)

    for gate_suffix in re.findall(r"\bGATE\s*[- ]*\s*([A-Z0-9]+)\b", base):
        normalized_suffix = normalize_match_value(gate_suffix).replace(" ", "")
        if not normalized_suffix:
            continue
        hints.add(f"G{normalized_suffix}")
        hints.add(f"GATE{normalized_suffix}")
        hints.add(f"GATE {normalized_suffix}")
        hints.add(normalized_suffix)

    return sorted(hints)


def db_connect():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with db_connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at_utc TEXT NOT NULL,
                qr_text TEXT NOT NULL,
                source TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS gates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gate_code TEXT NOT NULL UNIQUE,
                door_count INTEGER NOT NULL CHECK(door_count BETWEEN 2 AND 6),
                created_at_utc TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_doors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gate_id INTEGER NOT NULL,
                door_no INTEGER NOT NULL,
                door_code TEXT NOT NULL UNIQUE,
                created_at_utc TEXT NOT NULL,
                FOREIGN KEY(gate_id) REFERENCES gates(id) ON DELETE CASCADE,
                UNIQUE(gate_id, door_no)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gate_code TEXT NOT NULL UNIQUE,
                created_at_utc TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_config_doors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gate_id INTEGER NOT NULL,
                door_no INTEGER NOT NULL,
                door_number TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                FOREIGN KEY(gate_id) REFERENCES gate_configs(id) ON DELETE CASCADE,
                UNIQUE(gate_id, door_no),
                UNIQUE(gate_id, door_number)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_cycle_state (
                gate_id INTEGER PRIMARY KEY,
                last_completed_scan_id INTEGER NOT NULL DEFAULT 0,
                updated_at_utc TEXT NOT NULL,
                next_expected_door_no INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(gate_id) REFERENCES gate_configs(id) ON DELETE CASCADE
            )
            """
        )
        gate_cycle_state_columns = connection.execute("PRAGMA table_info(gate_cycle_state)").fetchall()
        if not any(row["name"] == "next_expected_door_no" for row in gate_cycle_state_columns):
            connection.execute(
                "ALTER TABLE gate_cycle_state ADD COLUMN next_expected_door_no INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            """
            UPDATE gate_cycle_state
            SET next_expected_door_no = 1
            WHERE next_expected_door_no IS NULL OR next_expected_door_no < 1
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_cycle_door_state (
                gate_id INTEGER NOT NULL,
                door_no INTEGER NOT NULL,
                last_scan_id INTEGER NOT NULL,
                PRIMARY KEY(gate_id, door_no),
                FOREIGN KEY(gate_id) REFERENCES gate_configs(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS action_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gate_id INTEGER NOT NULL,
                completed_scan_id INTEGER NOT NULL,
                completed_at_utc TEXT NOT NULL,
                closed_at_utc TEXT,
                FOREIGN KEY(gate_id) REFERENCES gate_configs(id) ON DELETE CASCADE,
                UNIQUE(gate_id, completed_scan_id)
            )
            """
        )
        action_event_columns = connection.execute("PRAGMA table_info(action_events)").fetchall()
        if not any(row["name"] == "closed_at_utc" for row in action_event_columns):
            connection.execute("ALTER TABLE action_events ADD COLUMN closed_at_utc TEXT")
        if not any(row["name"] == "is_red_card" for row in action_event_columns):
            connection.execute("ALTER TABLE action_events ADD COLUMN is_red_card INTEGER NOT NULL DEFAULT 0")
        if not any(row["name"] == "door2_elapsed_seconds" for row in action_event_columns):
            connection.execute("ALTER TABLE action_events ADD COLUMN door2_elapsed_seconds INTEGER")
        connection.commit()


def add_scan(qr_text: str, source: str):
    if not qr_text.strip():
        raise ValueError("qr_text is required")

    scanned_at = utc_now_iso()
    normalized_qr = qr_text.strip()
    match_qr = normalize_match_value(normalized_qr)
    normalized_source = source.strip().upper() or "UNKNOWN"

    with db_connect() as connection:
        cursor = connection.execute(
            "INSERT INTO scans(scanned_at_utc, qr_text, source) VALUES(?, ?, ?)",
            (scanned_at, normalized_qr, normalized_source),
        )
        scan_id = cursor.lastrowid
        process_scan_for_actions(connection, match_qr, scan_id, scanned_at)
        connection.commit()

    return scan_id


def list_scans(limit: int = 300):
    with db_connect() as connection:
        rows = connection.execute(
            """
            SELECT id, scanned_at_utc, qr_text, source
            FROM scans
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    scans = []
    for row in rows:
        item = dict(row)
        item["scanned_at_sgt"] = format_iso_utc_to_sgt(item.get("scanned_at_utc"))
        scans.append(item)
    return scans


def list_gate_summary(limit: int = 300):
    with db_connect() as connection:
        rows = connection.execute(
            """
            SELECT
                qr_text AS gate_code,
                COUNT(*) AS scan_count,
                MAX(scanned_at_utc) AS last_scanned_at_utc
            FROM scans
            GROUP BY qr_text
            ORDER BY last_scanned_at_utc DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    summary = []
    for row in rows:
        item = dict(row)
        item["last_scanned_at_sgt"] = format_iso_utc_to_sgt(item.get("last_scanned_at_utc"))
        summary.append(item)
    return summary


def process_scan_for_actions(connection, scanned_qr: str, scan_id: int, scanned_at_utc: str):
    candidates = build_match_candidates(scanned_qr)
    if not candidates:
        return

    gate_hints = build_gate_hints(scanned_qr)
    door_placeholders = ", ".join("?" for _ in candidates)
    query_params = list(candidates)

    if gate_hints:
        gate_placeholders = ", ".join("?" for _ in gate_hints)
        query = f"""
        SELECT d.gate_id, d.door_no
        FROM gate_config_doors d
        JOIN gate_configs g ON g.id = d.gate_id
        WHERE UPPER(d.door_number) IN ({door_placeholders})
          AND UPPER(g.gate_code) IN ({gate_placeholders})
        """
        query_params.extend(gate_hints)
    else:
        query = f"""
        SELECT gate_id, door_no
        FROM gate_config_doors
        WHERE UPPER(door_number) IN ({door_placeholders})
        """

    matches = connection.execute(query, query_params).fetchall()
    if not matches:
        return

    matches_by_gate = {}
    for row in matches:
        gate_id = row["gate_id"]
        matches_by_gate.setdefault(gate_id, set()).add(int(row["door_no"]))
        connection.execute(
            """
            INSERT OR IGNORE INTO gate_cycle_state(
                gate_id, last_completed_scan_id, updated_at_utc, next_expected_door_no
            )
            VALUES(?, 0, ?, 1)
            """,
            (gate_id, scanned_at_utc),
        )

    for gate_id, matched_door_nos in matches_by_gate.items():
        state_row = connection.execute(
            """
            SELECT last_completed_scan_id, next_expected_door_no
            FROM gate_cycle_state
            WHERE gate_id = ?
            """,
            (gate_id,),
        ).fetchone()
        if state_row is None:
            continue

        required_doors = connection.execute(
            """
            SELECT door_no
            FROM gate_config_doors
            WHERE gate_id = ?
            ORDER BY door_no ASC
            """,
            (gate_id,),
        ).fetchall()
        if not required_doors:
            continue

        required_count = len(required_doors)
        expected_index = int(state_row["next_expected_door_no"] or 1)
        if expected_index < 1 or expected_index > required_count:
            expected_index = 1
        expected_door_no = int(required_doors[expected_index - 1]["door_no"])
        first_door_no = int(required_doors[0]["door_no"])

        if expected_door_no in matched_door_nos:
            connection.execute(
                """
                INSERT INTO gate_cycle_door_state(gate_id, door_no, last_scan_id)
                VALUES(?, ?, ?)
                ON CONFLICT(gate_id, door_no) DO UPDATE SET last_scan_id = excluded.last_scan_id
                """,
                (gate_id, expected_door_no, scan_id),
            )
            if expected_index >= required_count:
                is_red_card = 0
                door2_elapsed_seconds = None
                if required_count == 2:
                    first_scan_row = connection.execute(
                        """
                        SELECT last_scan_id
                        FROM gate_cycle_door_state
                        WHERE gate_id = ? AND door_no = ?
                        """,
                        (gate_id, first_door_no),
                    ).fetchone()
                    if first_scan_row is not None:
                        first_scan_at_row = connection.execute(
                            "SELECT scanned_at_utc FROM scans WHERE id = ?",
                            (int(first_scan_row["last_scan_id"]),),
                        ).fetchone()
                        first_dt = parse_utc_iso(first_scan_at_row["scanned_at_utc"]) if first_scan_at_row else None
                        current_dt = parse_utc_iso(scanned_at_utc)
                        if first_dt and current_dt:
                            door2_elapsed_seconds = max(0, int((current_dt - first_dt).total_seconds()))
                            if door2_elapsed_seconds > DOOR_2_TIMEOUT_SECONDS:
                                is_red_card = 1
                connection.execute(
                    """
                    INSERT OR IGNORE INTO action_events(
                        gate_id, completed_scan_id, completed_at_utc, is_red_card, door2_elapsed_seconds
                    )
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (gate_id, scan_id, scanned_at_utc, is_red_card, door2_elapsed_seconds),
                )
                connection.execute(
                    """
                    UPDATE gate_cycle_state
                    SET last_completed_scan_id = ?, updated_at_utc = ?, next_expected_door_no = 1
                    WHERE gate_id = ?
                    """,
                    (scan_id, scanned_at_utc, gate_id),
                )
                connection.execute("DELETE FROM gate_cycle_door_state WHERE gate_id = ?", (gate_id,))
            else:
                connection.execute(
                    """
                    UPDATE gate_cycle_state
                    SET updated_at_utc = ?, next_expected_door_no = ?
                    WHERE gate_id = ?
                    """,
                    (scanned_at_utc, expected_index + 1, gate_id),
                )
            continue

        # Wrong order: reset sequence progress for this gate.
        connection.execute("DELETE FROM gate_cycle_door_state WHERE gate_id = ?", (gate_id,))

        if first_door_no in matched_door_nos:
            connection.execute(
                """
                INSERT INTO gate_cycle_door_state(gate_id, door_no, last_scan_id)
                VALUES(?, ?, ?)
                ON CONFLICT(gate_id, door_no) DO UPDATE SET last_scan_id = excluded.last_scan_id
                """,
                (gate_id, first_door_no, scan_id),
            )
            connection.execute(
                """
                UPDATE gate_cycle_state
                SET updated_at_utc = ?, next_expected_door_no = ?
                WHERE gate_id = ?
                """,
                (scanned_at_utc, 2, gate_id),
            )
        else:
            connection.execute(
                """
                UPDATE gate_cycle_state
                SET updated_at_utc = ?, next_expected_door_no = 1
                WHERE gate_id = ?
                """,
                (scanned_at_utc, gate_id),
            )


def normalize_gate_code(gate_code: str) -> str:
    code = str(gate_code or "").strip().upper()
    if not code:
        raise ValueError("gate_code is required")
    return code


def validate_door_count(door_count) -> int:
    try:
        count = int(door_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("door_count must be an integer between 2 and 6") from exc
    if count < 2 or count > 6:
        raise ValueError("door_count must be between 2 and 6")
    return count


def validate_door_numbers(door_numbers):
    if not isinstance(door_numbers, list):
        raise ValueError("door_numbers must be a list")

    count = len(door_numbers)
    if count < 2 or count > 6:
        raise ValueError("door_numbers must contain between 2 and 6 items")
    normalized = []
    seen = set()
    for idx, raw in enumerate(door_numbers, start=1):
        value = normalize_match_value(raw)
        if not value:
            raise ValueError(f"door number {idx} is required")
        if value in seen:
            raise ValueError("door numbers must be unique for the gate")
        seen.add(value)
        normalized.append(value)
    return count, normalized


def fetch_gate_config_with_doors(connection, gate_id: int):
    gate_row = connection.execute(
        """
        SELECT id, gate_code, created_at_utc
        FROM gate_configs
        WHERE id = ?
        """,
        (gate_id,),
    ).fetchone()
    if gate_row is None:
        return None

    door_rows = connection.execute(
        """
        SELECT door_no, door_number
        FROM gate_config_doors
        WHERE gate_id = ?
        ORDER BY door_no ASC
        """,
        (gate_id,),
    ).fetchall()
    doors = [dict(row) for row in door_rows]
    return {
        "id": gate_row["id"],
        "gate_code": gate_row["gate_code"],
        "door_count": len(doors),
        "created_at_utc": gate_row["created_at_utc"],
        "created_at_sgt": format_iso_utc_to_sgt(gate_row["created_at_utc"]),
        "doors": doors,
    }


def create_gate(gate_code: str):
    code = normalize_gate_code(gate_code)
    now = utc_now_iso()
    with db_connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO gate_configs(gate_code, created_at_utc)
            VALUES(?, ?)
            """,
            (code, now),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO gate_cycle_state(
                gate_id, last_completed_scan_id, updated_at_utc, next_expected_door_no
            )
            VALUES(?, 0, ?, 1)
            """,
            (cursor.lastrowid, now),
        )
        connection.commit()
        return fetch_gate_config_with_doors(connection, cursor.lastrowid)


def set_gate_doors(gate_id: int, door_numbers):
    count, normalized = validate_door_numbers(door_numbers)
    now = utc_now_iso()

    with db_connect() as connection:
        existing = fetch_gate_config_with_doors(connection, gate_id)
        if existing is None:
            raise ValueError("gate not found")

        connection.execute("DELETE FROM gate_config_doors WHERE gate_id = ?", (gate_id,))
        connection.execute("DELETE FROM gate_cycle_door_state WHERE gate_id = ?", (gate_id,))
        connection.execute(
            """
            INSERT OR IGNORE INTO gate_cycle_state(
                gate_id, last_completed_scan_id, updated_at_utc, next_expected_door_no
            )
            VALUES(?, 0, ?, 1)
            """,
            (gate_id, now),
        )
        connection.execute(
            """
            UPDATE gate_cycle_state
            SET last_completed_scan_id = 0, updated_at_utc = ?, next_expected_door_no = 1
            WHERE gate_id = ?
            """,
            (now, gate_id),
        )
        for idx, door_number in enumerate(normalized, start=1):
            connection.execute(
                """
                INSERT INTO gate_config_doors(gate_id, door_no, door_number, created_at_utc)
                VALUES(?, ?, ?, ?)
                """,
                (gate_id, idx, door_number, now),
            )
        connection.commit()
        gate = fetch_gate_config_with_doors(connection, gate_id)
        gate["door_count"] = count
        return gate


def list_gates(limit: int = 300):
    with db_connect() as connection:
        gate_rows = connection.execute(
            """
            SELECT id
            FROM gate_configs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [fetch_gate_config_with_doors(connection, row["id"]) for row in gate_rows]


def list_action_events(limit: int = 200, include_closed: bool = False):
    with db_connect() as connection:
        where_clause = "" if include_closed else "WHERE e.closed_at_utc IS NULL"
        rows = connection.execute(
            f"""
            SELECT
                e.id,
                e.completed_at_utc,
                e.completed_scan_id,
                e.closed_at_utc,
                e.is_red_card,
                e.door2_elapsed_seconds,
                g.id AS gate_id,
                g.gate_code
            FROM action_events e
            JOIN gate_configs g ON g.id = e.gate_id
            {where_clause}
            ORDER BY e.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        events = []
        for row in rows:
            door_rows = connection.execute(
                """
                SELECT door_no, door_number
                FROM gate_config_doors
                WHERE gate_id = ?
                ORDER BY door_no ASC
                """,
                (row["gate_id"],),
            ).fetchall()
            doors = [dict(door) for door in door_rows]
            events.append(
                {
                    "id": row["id"],
                    "gate_id": row["gate_id"],
                    "gate_code": row["gate_code"],
                    "door_count": len(doors),
                    "doors": doors,
                    "completed_at_utc": row["completed_at_utc"],
                    "completed_at_sgt": format_iso_utc_to_sgt(row["completed_at_utc"]),
                    "closed_at_utc": row["closed_at_utc"],
                    "closed_at_sgt": format_iso_utc_to_sgt(row["closed_at_utc"]),
                    "completed_scan_id": row["completed_scan_id"],
                    "is_red_card": bool(row["is_red_card"]),
                    "door2_elapsed_seconds": row["door2_elapsed_seconds"],
                }
            )
        return events


def close_action_event(event_id: int):
    closed_at = utc_now_iso()
    with db_connect() as connection:
        cursor = connection.execute(
            """
            UPDATE action_events
            SET closed_at_utc = ?
            WHERE id = ? AND closed_at_utc IS NULL
            """,
            (closed_at, event_id),
        )
        connection.commit()
        return cursor.rowcount > 0


INDEX_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gate Scanner</title>
  <style>
    :root {
      --ink: #f8fafc;
      --muted: #cbd5e1;
      --glass: rgba(15, 23, 42, 0.55);
      --glass-strong: rgba(2, 6, 23, 0.74);
      --line: rgba(255, 255, 255, 0.22);
      --accent: #22c55e;
      --danger: #f87171;
      --button: rgba(30, 41, 59, 0.76);
      --button-soft: rgba(15, 23, 42, 0.7);
    }
    * {
      box-sizing: border-box;
    }
    html, body {
      height: 100%;
    }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      background: #020617;
      color: var(--ink);
      overflow: hidden;
    }
    .scanner-shell {
      position: fixed;
      inset: 0;
      background: #000;
    }
    video {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      object-fit: cover;
      background: #000;
    }
    #qr-canvas {
      display: none;
    }
    .top-overlay {
      position: absolute;
      left: 0;
      right: 0;
      top: 0;
      padding: max(env(safe-area-inset-top), 14px) 14px 16px;
      background: linear-gradient(180deg, rgba(2, 6, 23, 0.88), rgba(2, 6, 23, 0));
      pointer-events: none;
      z-index: 4;
    }
    .topbar {
      pointer-events: auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .topbar h1 {
      margin: 0;
      font-size: 30px;
      letter-spacing: 0.01em;
      text-shadow: 0 4px 22px rgba(0, 0, 0, 0.45);
    }
    .topbar .badge {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      backdrop-filter: blur(8px);
      background: rgba(15, 23, 42, 0.4);
    }
    .scan-zone {
      position: absolute;
      left: 50%;
      top: 50%;
      width: min(74vw, 360px);
      aspect-ratio: 1;
      transform: translate(-50%, -50%);
      z-index: 3;
      pointer-events: none;
    }
    .scan-zone::before {
      content: "";
      position: absolute;
      inset: 0;
      border-radius: 28px;
      box-shadow: 0 0 0 999vmax rgba(2, 6, 23, 0.34);
    }
    .corner {
      position: absolute;
      width: 56px;
      height: 56px;
      border: 4px solid #fff;
      opacity: 0.88;
    }
    .detected-chip {
      position: absolute;
      left: 50%;
      top: 50%;
      transform: translate(-50%, -50%);
      min-width: 150px;
      max-width: 82%;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.9);
      color: #f8fafc;
      border: 1px solid rgba(255, 255, 255, 0.18);
      text-align: center;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: 0.02em;
      padding: 10px 24px;
      overflow-wrap: anywhere;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
    }
    .corner.tl {
      left: 0;
      top: 0;
      border-right: 0;
      border-bottom: 0;
      border-top-left-radius: 24px;
    }
    .corner.tr {
      right: 0;
      top: 0;
      border-left: 0;
      border-bottom: 0;
      border-top-right-radius: 24px;
    }
    .corner.bl {
      left: 0;
      bottom: 0;
      border-right: 0;
      border-top: 0;
      border-bottom-left-radius: 24px;
    }
    .corner.br {
      right: 0;
      bottom: 0;
      border-left: 0;
      border-top: 0;
      border-bottom-right-radius: 24px;
    }
    .bottom-overlay {
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      z-index: 5;
      padding: 18px 14px max(env(safe-area-inset-bottom), 16px);
      background: linear-gradient(0deg, rgba(2, 6, 23, 0.96), rgba(2, 6, 23, 0.64) 45%, rgba(2, 6, 23, 0.02));
    }
    .result {
      min-height: 22px;
      margin-bottom: 10px;
      color: #86efac;
      font-size: 14px;
      font-weight: 600;
      text-shadow: 0 2px 18px rgba(0, 0, 0, 0.45);
    }
    .result.error {
      color: var(--danger);
    }
    .control-row {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }
    .capture-row {
      margin-top: 8px;
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 8px;
    }
    button {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 11px 12px;
      font-weight: 700;
      font-size: 14px;
      color: #fff;
      background: var(--button);
      backdrop-filter: blur(10px);
      cursor: pointer;
    }
    button.primary {
      border-color: rgba(74, 222, 128, 0.7);
      background: linear-gradient(180deg, rgba(34, 197, 94, 0.85), rgba(22, 163, 74, 0.86));
    }
    button.capture {
      border-color: rgba(134, 239, 172, 0.9);
      background: linear-gradient(180deg, rgba(34, 197, 94, 0.95), rgba(22, 163, 74, 0.96));
    }
    button.capture:disabled {
      border-color: rgba(148, 163, 184, 0.55);
      background: rgba(100, 116, 139, 0.7);
      color: rgba(226, 232, 240, 0.9);
      cursor: not-allowed;
    }
    button.ghost {
      background: var(--button-soft);
    }
    .hidden {
      display: none;
    }
    body.scanning .badge-dot {
      animation: pulse 1.1s ease-in-out infinite;
      background: #22c55e;
    }
    .badge-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: #94a3b8;
      display: inline-block;
      margin-right: 6px;
      vertical-align: middle;
    }
    @keyframes pulse {
      0%, 100% { box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.8); }
      50% { box-shadow: 0 0 0 9px rgba(34, 197, 94, 0); }
    }
    @media (min-width: 900px) {
      .scan-zone {
        width: min(38vw, 420px);
      }
      .bottom-overlay {
        max-width: 540px;
        left: 50%;
        transform: translateX(-50%);
        border-radius: 18px 18px 0 0;
        border: 1px solid rgba(255, 255, 255, 0.14);
        border-bottom: 0;
      }
    }
  </style>
</head>
<body>
  <div class="scanner-shell">
    <video id="qr-video" autoplay playsinline muted></video>
    <canvas id="qr-canvas"></canvas>

    <div class="top-overlay">
      <div class="topbar">
        <h1>Gate Scanner</h1>
        <div class="badge"><span class="badge-dot"></span>Live</div>
      </div>
    </div>

    <div class="scan-zone">
      <span class="corner tl"></span>
      <span class="corner tr"></span>
      <span class="corner bl"></span>
      <span class="corner br"></span>
      <div id="detected-chip" class="detected-chip hidden"></div>
    </div>

    <div class="bottom-overlay">
      <div id="scan-result" class="result">Ready to scan gate code</div>
      <div class="control-row">
        <button id="start-scan" class="primary" type="button">Start</button>
        <button id="capture-scan" class="capture hidden" type="button" disabled>Capture</button>
        <button id="stop-scan" class="ghost" type="button">Stop</button>
      </div>
      <div class="capture-row">
        <button id="clear-detected" class="ghost hidden" type="button">Reset</button>
      </div>

    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.min.js"></script>
  <script>
    const video = document.getElementById('qr-video');
    const canvas = document.getElementById('qr-canvas');
    const canvasCtx = canvas.getContext('2d', { willReadFrequently: true });
    const resultBox = document.getElementById('scan-result');
    const startButton = document.getElementById('start-scan');
    const captureButton = document.getElementById('capture-scan');
    const clearButton = document.getElementById('clear-detected');
    const detectedChip = document.getElementById('detected-chip');
    let stream = null;
    let detector = null;
    let detectorMode = null;
    let scanning = false;
    let pendingDetectedText = '';
    let lastSentText = '';
    let lastSentAt = 0;
    const AUTO_STOP_IDLE_MS = 10000;
    let autoStopTimer = null;

    function setResult(text, isError = false) {
      resultBox.textContent = text;
      resultBox.className = isError ? 'result error' : 'result';
    }

    function setScanningState(isOn) {
      document.body.classList.toggle('scanning', isOn);
    }

    function clearAutoStopTimer() {
      if (autoStopTimer) {
        clearTimeout(autoStopTimer);
        autoStopTimer = null;
      }
    }

    function bumpAutoStopTimer() {
      clearAutoStopTimer();
      if (!scanning) {
        return;
      }
      autoStopTimer = setTimeout(() => {
        if (!scanning) {
          return;
        }
        stopCameraScan('Auto-stopped after 10s inactivity. Tap Start to scan again.');
      }, AUTO_STOP_IDLE_MS);
    }

    function setCaptureMode(showCapture) {
      startButton.classList.toggle('hidden', showCapture);
      captureButton.classList.toggle('hidden', !showCapture);
      if (!showCapture) {
        captureButton.disabled = true;
      }
    }

    function setPendingDetection(text) {
      pendingDetectedText = (text || '').trim();
      const hasPending = Boolean(pendingDetectedText);
      captureButton.disabled = !hasPending;
      clearButton.classList.toggle('hidden', !hasPending);
      detectedChip.classList.toggle('hidden', !hasPending);

      if (hasPending) {
        detectedChip.textContent = pendingDetectedText;
        setResult(`Detected: ${pendingDetectedText}. Tap Capture to submit.`);
      } else {
        detectedChip.textContent = '';
      }
    }

    async function submitScan(qrText, source) {
      const payload = (qrText || '').trim();
      if (!payload) {
        return false;
      }

      const now = Date.now();
      if (payload === lastSentText && now - lastSentAt < 1600) {
        return false;
      }

      let res;
      try {
        res = await fetch('/api/scan', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ qr_text: payload, source }),
        });
      } catch (err) {
        setResult(`Submit failed: ${err.message || err}`, true);
        return false;
      }

      let data = null;
      let textBody = '';
      const contentType = res.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {
        data = await res.json().catch(() => null);
      } else {
        textBody = await res.text().catch(() => '');
      }

      if (!res.ok) {
        const backendError = data && data.error ? data.error : '';
        const genericError = textBody || `Scan rejected (${res.status})`;
        setResult(backendError || genericError, true);
        return false;
      }

      lastSentText = payload;
      lastSentAt = now;
      setResult('Scan saved');
      return true;
    }

    async function detectQrFromFrame() {
      if (detectorMode === 'barcode' && detector) {
        const barcodes = await detector.detect(video);
        if (barcodes && barcodes.length > 0 && barcodes[0].rawValue) {
          return barcodes[0].rawValue;
        }
        return null;
      }

      if (detectorMode === 'jsqr' && window.jsQR) {
        const width = video.videoWidth;
        const height = video.videoHeight;
        if (!width || !height) {
          return null;
        }
        canvas.width = width;
        canvas.height = height;
        canvasCtx.drawImage(video, 0, 0, width, height);
        const imageData = canvasCtx.getImageData(0, 0, width, height);
        const code = window.jsQR(imageData.data, width, height, { inversionAttempts: 'dontInvert' });
        return code && code.data ? code.data : null;
      }

      return null;
    }

    async function scanLoop() {
      if (!scanning) {
        return;
      }

      try {
        if (pendingDetectedText) {
          if (scanning) {
            requestAnimationFrame(scanLoop);
          }
          return;
        }

        const qrText = await detectQrFromFrame();
        if (qrText) {
          setPendingDetection(qrText);
          bumpAutoStopTimer();
        }
      } catch (_) {
        // keep loop alive
      }

      if (scanning) {
        requestAnimationFrame(scanLoop);
      }
    }

    async function startCameraScan() {
      if (scanning) {
        return;
      }

      detector = null;
      detectorMode = null;

      if ('BarcodeDetector' in window) {
        try {
          detector = new BarcodeDetector({ formats: ['qr_code'] });
          detectorMode = 'barcode';
        } catch (_) {
          detector = null;
        }
      }

      if (!detectorMode && window.jsQR) {
        detectorMode = 'jsqr';
      }

      if (!detectorMode) {
        setResult('Browser camera QR scan not supported on this device.', true);
        return;
      }

      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: 'environment' } },
          audio: false,
        });
      } catch (err) {
        setResult(`Camera access failed: ${err.message || err}`, true);
        return;
      }

      video.srcObject = stream;
      try {
        await video.play();
      } catch (_) {
        // ignored
      }

      scanning = true;
      setScanningState(true);
      setCaptureMode(true);
      setPendingDetection('');
      setResult('Scan Door QR code');
      bumpAutoStopTimer();
      scanLoop();
    }

    function stopCameraScan(stopMessage = 'Camera scan stopped') {
      scanning = false;
      clearAutoStopTimer();
      setScanningState(false);
      setCaptureMode(false);
      setPendingDetection('');
      if (stream) {
        stream.getTracks().forEach((track) => track.stop());
        stream = null;
      }
      video.srcObject = null;
      setResult(stopMessage);
    }

    document.getElementById('start-scan').addEventListener('click', startCameraScan);
    document.getElementById('stop-scan').addEventListener('click', stopCameraScan);
    captureButton.addEventListener('click', async () => {
      bumpAutoStopTimer();
      if (!pendingDetectedText) {
        setResult('No detected gate code yet.', true);
        return;
      }
      const codeToSubmit = pendingDetectedText;
      const ok = await submitScan(codeToSubmit, 'CAMERA');
      if (ok) {
        setPendingDetection('');
        bumpAutoStopTimer();
      }
    });
    clearButton.addEventListener('click', () => {
      bumpAutoStopTimer();
      setPendingDetection('');
      setResult('Ready to detect next gate code');
    });

    setScanningState(false);
    setCaptureMode(false);
  </script>
</body>
</html>
"""


OFFICE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gate Office Dashboard</title>
  <style>
    :root {
      --bg: #f1f5f9;
      --card: #ffffff;
      --ink: #0f172a;
      --muted: #64748b;
      --border: #dbe4ef;
      --accent: #0f766e;
      --warn: #b91c1c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background: radial-gradient(circle at top right, #dcfce7 0, var(--bg) 42%);
    }
    .wrap {
      max-width: 1160px;
      margin: 24px auto 28px;
      padding: 0 14px;
    }
    .top {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin-bottom: 14px;
    }
    h1 {
      margin: 0;
      font-size: 28px;
    }
    .muted { color: var(--muted); font-size: 14px; }
    .links {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .btn {
      border: 1px solid #cbd5e1;
      border-radius: 999px;
      background: #fff;
      color: var(--ink);
      text-decoration: none;
      padding: 9px 13px;
      font-size: 14px;
      font-weight: 700;
    }
    .btn.primary {
      background: var(--accent);
      color: #fff;
      border-color: #0f766e;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 4px 16px rgba(15, 23, 42, 0.05);
    }
    .kpi-title {
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .kpi-value {
      margin-top: 7px;
      font-size: 30px;
      font-weight: 800;
    }
    .grid {
      display: grid;
      grid-template-columns: 1.1fr 1fr;
      gap: 10px;
    }
    .panel-title {
      margin: 0 0 10px;
      font-size: 17px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    th, td {
      border-bottom: 1px solid var(--border);
      text-align: left;
      padding: 9px 8px;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    td.mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-weight: 700;
    }
    .status {
      min-height: 18px;
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .status.err {
      color: var(--warn);
      font-weight: 700;
    }
    @media (max-width: 940px) {
      .stats { grid-template-columns: 1fr; }
      .grid { grid-template-columns: 1fr; }
      h1 { font-size: 24px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1>Gate Office Dashboard</h1>
        <div class="muted">Live monitor of scanned gate codes.</div>
      </div>
      <div class="links">
        <a class="btn" href="/office/gates">Gate Setup</a>
        <a class="btn primary" href="/api/export.csv">Export CSV</a>
      </div>
    </div>

    <div class="stats">
      <div class="card">
        <div class="kpi-title">Total Scans</div>
        <div class="kpi-value" id="kpi-total">0</div>
      </div>
      <div class="card">
        <div class="kpi-title">Unique Gates</div>
        <div class="kpi-value" id="kpi-gates">0</div>
      </div>
      <div class="card">
        <div class="kpi-title">Last Scan (SGT)</div>
        <div class="kpi-value" id="kpi-last" style="font-size:18px;">-</div>
      </div>
    </div>

    <div class="status" id="status">Refreshing...</div>

    <div class="grid">
      <div class="card">
        <h2 class="panel-title">Gate Summary</h2>
        <table>
          <thead>
            <tr>
              <th>Gate</th>
              <th>Total Scans</th>
              <th>Last Scanned (SGT)</th>
            </tr>
          </thead>
          <tbody id="gate-rows"></tbody>
        </table>
      </div>

      <div class="card">
        <h2 class="panel-title">Recent Activity</h2>
        <table>
          <thead>
            <tr>
              <th>Time (SGT)</th>
              <th>Gate</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody id="scan-rows"></tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    const gateRows = document.getElementById('gate-rows');
    const scanRows = document.getElementById('scan-rows');
    const kpiTotal = document.getElementById('kpi-total');
    const kpiGates = document.getElementById('kpi-gates');
    const kpiLast = document.getElementById('kpi-last');
    const statusBox = document.getElementById('status');

    function esc(text) {
      return String(text || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function formatSgtDateTimeFromDate(value) {
      const date = value instanceof Date ? value : new Date(value);
      if (Number.isNaN(date.getTime())) {
        return String(value || '-');
      }
      const parts = new Intl.DateTimeFormat('en-SG', {
        timeZone: 'Asia/Singapore',
        day: '2-digit',
        month: 'short',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      }).formatToParts(date);
      const map = {};
      parts.forEach((part) => {
        if (part.type !== 'literal') {
          map[part.type] = part.value;
        }
      });
      return `${map.day}-${map.month}-${map.year} ${map.hour}:${map.minute}:${map.second} SGT`;
    }

    function formatSgtDateTimeFromDate(value) {
      const date = value instanceof Date ? value : new Date(value);
      if (Number.isNaN(date.getTime())) {
        return String(value || '-');
      }
      const parts = new Intl.DateTimeFormat('en-SG', {
        timeZone: 'Asia/Singapore',
        day: '2-digit',
        month: 'short',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      }).formatToParts(date);
      const map = {};
      parts.forEach((part) => {
        if (part.type !== 'literal') {
          map[part.type] = part.value;
        }
      });
      return `${map.day}-${map.month}-${map.year} ${map.hour}:${map.minute}:${map.second} SGT`;
    }

    function setStatus(text, isError = false) {
      statusBox.textContent = text;
      statusBox.className = isError ? 'status err' : 'status';
    }

    async function refreshDashboard() {
      try {
        const [summaryRes, scansRes] = await Promise.all([
          fetch('/api/gate-summary?limit=1000'),
          fetch('/api/scans?limit=200'),
        ]);

        if (!summaryRes.ok || !scansRes.ok) {
          setStatus(`Failed to refresh (${summaryRes.status}/${scansRes.status})`, true);
          return;
        }

        const summary = await summaryRes.json();
        const scans = await scansRes.json();

        if (!Array.isArray(summary) || !Array.isArray(scans)) {
          setStatus('Unexpected API response', true);
          return;
        }

        let totalScans = 0;
        summary.forEach((row) => {
          totalScans += Number(row.scan_count || 0);
        });
        kpiTotal.textContent = String(totalScans);
        kpiGates.textContent = String(summary.length);
        kpiLast.textContent = scans.length > 0 ? (scans[0].scanned_at_sgt || scans[0].scanned_at_utc || '-') : '-';

        gateRows.innerHTML = '';
        summary.forEach((row) => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td class="mono">${esc(row.gate_code)}</td>
            <td>${esc(row.scan_count)}</td>
            <td>${esc(row.last_scanned_at_sgt || row.last_scanned_at_utc || '-')}</td>
          `;
          gateRows.appendChild(tr);
        });

        scanRows.innerHTML = '';
        scans.slice(0, 40).forEach((row) => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>${esc(row.scanned_at_sgt || row.scanned_at_utc || '-')}</td>
            <td class="mono">${esc(row.qr_text)}</td>
            <td>${esc(row.source)}</td>
          `;
          scanRows.appendChild(tr);
        });

        setStatus(`Updated at ${formatSgtDateTimeFromDate(new Date())}`);
      } catch (err) {
        setStatus(`Refresh error: ${err.message || err}`, true);
      }
    }

    refreshDashboard();
    setInterval(refreshDashboard, 3000);
  </script>
</body>
</html>
"""


ACTION_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Action Page</title>
  <style>
    :root {
      --bg: #eef2ff;
      --card: #ffffff;
      --ink: #0f172a;
      --muted: #64748b;
      --border: #dbe4ef;
      --ok: #166534;
      --ok-bg: #dcfce7;
      --danger: #991b1b;
      --danger-bg: #fee2e2;
      --danger-border: #fecaca;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background: radial-gradient(circle at top right, #dbeafe 0, var(--bg) 48%);
    }
    .wrap {
      max-width: 1080px;
      margin: 24px auto 28px;
      padding: 0 14px;
    }
    .top {
      margin-bottom: 14px;
    }
    h1 {
      margin: 0;
      font-size: 28px;
    }
    .muted { color: var(--muted); font-size: 14px; }
    .status {
      margin-top: 8px;
      min-height: 18px;
      color: var(--muted);
      font-size: 13px;
    }
    .empty {
      border: 1px dashed #cbd5e1;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.75);
      padding: 18px;
      text-align: center;
      color: var(--muted);
      font-weight: 600;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 10px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 4px 18px rgba(15, 23, 42, 0.06);
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      background: var(--ok-bg);
      color: var(--ok);
      border: 1px solid #86efac;
      padding: 4px 8px;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.02em;
    }
    .badge.bad {
      background: var(--danger-bg);
      color: var(--danger);
      border-color: #fca5a5;
    }
    .card.red {
      background: #fff5f5;
      border-color: var(--danger-border);
    }
    .gate {
      margin-top: 8px;
      font-size: 24px;
      font-weight: 800;
      letter-spacing: 0.01em;
    }
    .meta {
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
    }
    .doors {
      margin-top: 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .chip {
      border: 1px solid #bfdbfe;
      background: #eff6ff;
      color: #1e3a8a;
      border-radius: 999px;
      padding: 4px 8px;
      font-size: 12px;
      font-weight: 700;
    }
    .card-actions {
      margin-top: 12px;
      display: flex;
      justify-content: flex-end;
    }
    .close-btn {
      border: 1px solid #cbd5e1;
      background: #f8fafc;
      color: #0f172a;
      border-radius: 999px;
      padding: 7px 12px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
    }
    .close-btn:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h1>Action Page</h1>
      <div class="muted">Cards appear only when all door QR codes for a gate are scanned.</div>
      <div id="status" class="status">Refreshing...</div>
    </div>
    <div id="empty" class="empty">No completed gate yet.</div>
    <div id="cards" class="grid"></div>
  </div>

  <script>
    const statusBox = document.getElementById('status');
    const emptyBox = document.getElementById('empty');
    const cardsBox = document.getElementById('cards');

    function esc(text) {
      return String(text || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function formatSgtDateTimeFromDate(value) {
      const date = value instanceof Date ? value : new Date(value);
      if (Number.isNaN(date.getTime())) {
        return String(value || '-');
      }
      const parts = new Intl.DateTimeFormat('en-SG', {
        timeZone: 'Asia/Singapore',
        day: '2-digit',
        month: 'short',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      }).formatToParts(date);
      const map = {};
      parts.forEach((part) => {
        if (part.type !== 'literal') {
          map[part.type] = part.value;
        }
      });
      return `${map.day}-${map.month}-${map.year} ${map.hour}:${map.minute}:${map.second} SGT`;
    }

    function renderCards(events) {
      cardsBox.innerHTML = '';
      if (!events.length) {
        emptyBox.style.display = 'block';
        return;
      }
      emptyBox.style.display = 'none';

      events.forEach((event) => {
        const doors = Array.isArray(event.doors) ? event.doors : [];
        const chips = doors.map((door) => `<span class="chip">${esc(door.door_number)}</span>`).join('');
        const card = document.createElement('div');
        const isRed = Boolean(event.is_red_card);
        const isTwoDoor = Number(event.door_count || 0) === 2;
        const elapsedRaw = event.door2_elapsed_seconds;
        const hasElapsed = elapsedRaw !== null && elapsedRaw !== undefined && elapsedRaw !== '';
        const elapsed = hasElapsed ? Number(elapsedRaw) : null;
        const timingText = isTwoDoor
          ? (elapsed === null
            ? 'Door 2 timing unavailable'
            : (isRed
              ? `Door 2 scanned after ${elapsed}s (limit: 20s)`
              : `Door 2 scanned in ${elapsed}s (limit: 20s)`))
          : 'Sequence completed';
        card.className = isRed ? 'card red' : 'card';
        card.innerHTML = `
          <span class="badge ${isRed ? 'bad' : ''}">${isRed ? 'Timeout' : 'Completed'}</span>
          <div class="gate">${esc(event.gate_code)}</div>
          <div class="meta">${esc(event.door_count)} doors scanned</div>
          <div class="meta">${esc(timingText)}</div>
          <div class="meta">${esc(event.completed_at_sgt || event.completed_at_utc || '-')}</div>
          <div class="doors">${chips}</div>
          <div class="card-actions">
            <button class="close-btn" data-action-id="${esc(event.id)}" type="button">Closed</button>
          </div>
        `;
        cardsBox.appendChild(card);
      });
    }

    async function closeEvent(eventId, buttonEl) {
      buttonEl.disabled = true;
      let res;
      try {
        res = await fetch(`/api/actions/${eventId}/close`, { method: 'POST' });
      } catch (err) {
        buttonEl.disabled = false;
        statusBox.textContent = `Close failed: ${err.message || err}`;
        return;
      }

      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        buttonEl.disabled = false;
        statusBox.textContent = data.error || `Close failed (${res.status})`;
        return;
      }

      statusBox.textContent = `Closed at ${formatSgtDateTimeFromDate(new Date())}`;
      await refreshActions();
    }

    async function refreshActions() {
      try {
        const res = await fetch('/api/actions?limit=80');
        if (!res.ok) {
          statusBox.textContent = `Failed to refresh (${res.status})`;
          return;
        }
        const events = await res.json();
        if (!Array.isArray(events)) {
          statusBox.textContent = 'Unexpected API response';
          return;
        }
        renderCards(events);
        statusBox.textContent = `Updated at ${formatSgtDateTimeFromDate(new Date())}`;
      } catch (err) {
        statusBox.textContent = `Refresh error: ${err.message || err}`;
      }
    }

    cardsBox.addEventListener('click', async (event) => {
      const button = event.target.closest('.close-btn');
      if (!button) {
        return;
      }
      const actionId = Number(button.dataset.actionId);
      if (!actionId) {
        return;
      }
      await closeEvent(actionId, button);
    });

    refreshActions();
    setInterval(refreshActions, 2000);
  </script>
</body>
</html>
"""


GATE_SETUP_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gate Setup</title>
  <style>
    :root {
      --bg: #f1f5f9;
      --card: #ffffff;
      --ink: #0f172a;
      --muted: #64748b;
      --border: #dbe4ef;
      --accent: #0f766e;
      --warn: #b91c1c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background: radial-gradient(circle at top right, #dcfce7 0, var(--bg) 42%);
    }
    .wrap {
      max-width: 1080px;
      margin: 24px auto 28px;
      padding: 0 14px;
    }
    .top {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }
    h1 {
      margin: 0;
      font-size: 28px;
    }
    .muted { color: var(--muted); font-size: 14px; }
    .btn {
      border: 1px solid #cbd5e1;
      border-radius: 999px;
      background: #fff;
      color: var(--ink);
      text-decoration: none;
      padding: 9px 13px;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
    }
    .btn.primary {
      background: var(--accent);
      color: #fff;
      border-color: #0f766e;
    }
    .grid {
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 10px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 4px 16px rgba(15, 23, 42, 0.05);
    }
    .card h2 {
      margin: 0 0 10px;
      font-size: 18px;
    }
    .field {
      margin-bottom: 10px;
    }
    .field label {
      display: block;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .door-list {
      display: grid;
      gap: 8px;
      margin-top: 8px;
    }
    .door-item {
      display: grid;
      grid-template-columns: 80px 1fr;
      align-items: center;
      gap: 8px;
    }
    .door-tag {
      border: 1px solid #cbd5e1;
      border-radius: 999px;
      padding: 6px 8px;
      background: #f8fafc;
      font-size: 12px;
      text-align: center;
      color: #334155;
      font-weight: 700;
    }
    input, select {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      font-size: 15px;
      background: #fff;
      color: var(--ink);
    }
    .status {
      min-height: 18px;
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .status.err {
      color: var(--warn);
      font-weight: 700;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    th, td {
      border-bottom: 1px solid var(--border);
      text-align: left;
      padding: 9px 8px;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    td.mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-weight: 700;
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .chip {
      border: 1px solid #bfdbfe;
      background: #eff6ff;
      color: #1e3a8a;
      border-radius: 999px;
      padding: 4px 8px;
      font-size: 12px;
      font-weight: 700;
    }
    @media (max-width: 940px) {
      .grid {
        grid-template-columns: 1fr;
      }
      h1 {
        font-size: 24px;
      }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1>Gate Setup</h1>
        <div class="muted">Step 1: create a gate. Step 2: enter door numbers in required scan sequence (Door 1, then Door 2, etc.).</div>
      </div>
      <a href="/office" class="btn">Back to Dashboard</a>
    </div>

    <div class="grid">
      <div class="card">
        <h2>Create Gate</h2>
        <div id="create-status" class="status">Ready</div>
        <form id="create-gate-form">
          <div class="field">
            <label for="gate-code">Gate Code</label>
            <input id="gate-code" placeholder="e.g. G12" required>
          </div>
          <button class="btn primary" type="submit">Create Gate</button>
        </form>

        <hr style="border:0;border-top:1px solid var(--border);margin:14px 0;">

        <h2 style="margin-top:0;">Set Door Numbers</h2>
        <div id="doors-status" class="status">Select a gate and set its doors. Action card appears only if scanned in this sequence.</div>
        <form id="set-doors-form">
          <div class="field">
            <label for="gate-select">Gate</label>
            <select id="gate-select" required>
              <option value="">Select gate</option>
            </select>
          </div>
          <div class="field">
            <label for="door-count">Number of Doors</label>
            <select id="door-count" required>
              <option value="2">2 doors</option>
              <option value="3">3 doors</option>
              <option value="4" selected>4 doors</option>
              <option value="5">5 doors</option>
              <option value="6">6 doors</option>
            </select>
          </div>
          <div class="field">
            <label>Door Numbers</label>
            <div id="door-fields" class="door-list"></div>
          </div>
          <button class="btn primary" type="submit">Save Doors</button>
        </form>
      </div>

      <div class="card">
        <h2>Configured Gates</h2>
        <table>
          <thead>
            <tr>
              <th>Gate</th>
              <th>Doors</th>
              <th>Door Numbers</th>
              <th>Created At (SGT)</th>
            </tr>
          </thead>
          <tbody id="gate-rows"></tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    const gateRows = document.getElementById('gate-rows');
    const createStatusBox = document.getElementById('create-status');
    const doorsStatusBox = document.getElementById('doors-status');
    const createGateForm = document.getElementById('create-gate-form');
    const setDoorsForm = document.getElementById('set-doors-form');
    const gateCodeInput = document.getElementById('gate-code');
    const gateSelect = document.getElementById('gate-select');
    const doorCountInput = document.getElementById('door-count');
    const doorFields = document.getElementById('door-fields');
    let gateCache = [];

    function esc(text) {
      return String(text || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function setCreateStatus(text, isError = false) {
      createStatusBox.textContent = text;
      createStatusBox.className = isError ? 'status err' : 'status';
    }

    function setDoorsStatus(text, isError = false) {
      doorsStatusBox.textContent = text;
      doorsStatusBox.className = isError ? 'status err' : 'status';
    }

    function normalizeGateCode(value) {
      return String(value || '').trim().toUpperCase();
    }

    function ordinalWord(index) {
      const words = ['First', 'Second', 'Third', 'Fourth', 'Fifth', 'Sixth'];
      return words[index - 1] || `${index}th`;
    }

    function getSelectedGate() {
      const selectedId = Number(gateSelect.value);
      if (!selectedId) {
        return null;
      }
      return gateCache.find((gate) => Number(gate.id) === selectedId) || null;
    }

    function buildDoorInputs(initialValues = null) {
      const doorCount = Number(doorCountInput.value);
      const previousValues = Array.from(doorFields.querySelectorAll('input')).map((el) => el.value);
      const seedValues = Array.isArray(initialValues) ? initialValues : previousValues;
      doorFields.innerHTML = '';

      for (let i = 1; i <= doorCount; i += 1) {
        const wrap = document.createElement('div');
        wrap.className = 'door-item';

        const tag = document.createElement('div');
        tag.className = 'door-tag';
        tag.textContent = `Scan ${ordinalWord(i)} Door`;

        const input = document.createElement('input');
        input.type = 'text';
        input.required = true;
        input.dataset.doorNo = String(i);
        input.placeholder = `${ordinalWord(i)} door QR`;
        input.value = seedValues[i - 1] || '';

        wrap.appendChild(tag);
        wrap.appendChild(input);
        doorFields.appendChild(wrap);
      }
    }

    function renderGateRows(gates) {
      gateRows.innerHTML = '';
      gates.forEach((gate) => {
        const doors = Array.isArray(gate.doors) ? gate.doors : [];
        const chips = doors.map((door) => `<span class="chip">#${esc(door.door_no)} ${esc(door.door_number)}</span>`).join('');
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td class="mono">${esc(gate.gate_code)}</td>
          <td>${esc(gate.door_count)}</td>
          <td><div class="chips">${chips}</div></td>
          <td>${esc(gate.created_at_sgt || gate.created_at_utc || '-')}</td>
        `;
        gateRows.appendChild(tr);
      });
    }

    function renderGateSelect(gates) {
      const selectedBefore = gateSelect.value;
      gateSelect.innerHTML = '<option value="">Select gate</option>';
      gates.forEach((gate) => {
        const option = document.createElement('option');
        option.value = String(gate.id);
        option.textContent = gate.gate_code;
        gateSelect.appendChild(option);
      });

      if (selectedBefore && gates.some((gate) => String(gate.id) === selectedBefore)) {
        gateSelect.value = selectedBefore;
      } else if (gates.length > 0) {
        gateSelect.value = String(gates[0].id);
      }
    }

    function syncDoorEditorFromSelectedGate() {
      const gate = getSelectedGate();
      if (!gate) {
        doorCountInput.value = '2';
        buildDoorInputs();
        return;
      }
      const doors = Array.isArray(gate.doors) ? gate.doors : [];
      if (doors.length >= 2 && doors.length <= 6) {
        doorCountInput.value = String(doors.length);
      }
      buildDoorInputs(doors.map((door) => door.door_number));
    }

    async function refreshGates() {
      const res = await fetch('/api/gates?limit=500');
      if (!res.ok) {
        setDoorsStatus(`Failed to load gates (${res.status})`, true);
        return;
      }
      const gates = await res.json();
      if (!Array.isArray(gates)) {
        setDoorsStatus('Unexpected gate list response', true);
        return;
      }
      gateCache = gates;
      renderGateRows(gates);
      renderGateSelect(gates);
      syncDoorEditorFromSelectedGate();
      setDoorsStatus(`Loaded ${gates.length} gates`);
    }

    createGateForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const gateCode = normalizeGateCode(gateCodeInput.value);
      if (!gateCode) {
        setCreateStatus('Gate code is required', true);
        return;
      }

      const res = await fetch('/api/gates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gate_code: gateCode }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setCreateStatus(data.error || `Create gate failed (${res.status})`, true);
        return;
      }

      gateCodeInput.value = '';
      setCreateStatus(`Created gate ${data.gate_code}`);
      await refreshGates();
      gateSelect.value = String(data.id);
      syncDoorEditorFromSelectedGate();
    });

    setDoorsForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const gate = getSelectedGate();
      if (!gate) {
        setDoorsStatus('Please select a gate first', true);
        return;
      }

      const doorNumbers = Array.from(doorFields.querySelectorAll('input')).map((el) => el.value.trim());
      const res = await fetch(`/api/gates/${gate.id}/doors`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ door_numbers: doorNumbers }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setDoorsStatus(data.error || `Save doors failed (${res.status})`, true);
        return;
      }

      setDoorsStatus(`Saved ${data.door_count} doors for gate ${data.gate_code}`);
      await refreshGates();
      gateSelect.value = String(data.id);
      syncDoorEditorFromSelectedGate();
    });

    gateSelect.addEventListener('change', syncDoorEditorFromSelectedGate);
    doorCountInput.addEventListener('change', () => buildDoorInputs());
    gateCodeInput.addEventListener('input', () => {
      gateCodeInput.value = normalizeGateCode(gateCodeInput.value);
    });
    buildDoorInputs();
    refreshGates();
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_TEMPLATE)


@app.route("/office")
def office():
    return render_template_string(OFFICE_TEMPLATE)


@app.route("/action")
def action_page():
    return render_template_string(ACTION_TEMPLATE)


@app.route("/office/gates")
def office_gates():
    return render_template_string(GATE_SETUP_TEMPLATE)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    payload = request.get_json(silent=True) or {}
    qr_text = str(payload.get("qr_text", "")).strip()
    source = str(payload.get("source", "MANUAL")).strip().upper() or "UNKNOWN"

    try:
        add_scan(qr_text, source)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500

    return jsonify({"ok": True})


@app.route("/api/scans", methods=["GET"])
def api_scans():
    try:
        limit = int(request.args.get("limit", "300"))
    except ValueError:
        limit = 300
    limit = max(1, min(limit, 5000))
    try:
        return jsonify(list_scans(limit=limit))
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500


@app.route("/api/gate-summary", methods=["GET"])
def api_gate_summary():
    try:
        limit = int(request.args.get("limit", "300"))
    except ValueError:
        limit = 300
    limit = max(1, min(limit, 5000))
    try:
        return jsonify(list_gate_summary(limit=limit))
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500


@app.route("/api/actions", methods=["GET"])
def api_actions():
    try:
        limit = int(request.args.get("limit", "200"))
    except ValueError:
        limit = 200
    limit = max(1, min(limit, 5000))
    include_closed = str(request.args.get("include_closed", "")).strip().lower() in {"1", "true", "yes"}
    try:
        return jsonify(list_action_events(limit=limit, include_closed=include_closed))
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500


@app.route("/api/actions/<int:event_id>/close", methods=["POST"])
def api_close_action(event_id: int):
    try:
        updated = close_action_event(event_id)
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500

    if not updated:
        return jsonify({"error": "action event not found or already closed"}), 404
    return jsonify({"ok": True})


@app.route("/api/gates", methods=["GET"])
def api_gates():
    try:
        limit = int(request.args.get("limit", "300"))
    except ValueError:
        limit = 300
    limit = max(1, min(limit, 5000))
    try:
        return jsonify(list_gates(limit=limit))
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500


@app.route("/api/gates", methods=["POST"])
def api_create_gate():
    payload = request.get_json(silent=True) or {}
    gate_code = payload.get("gate_code", "")

    try:
        gate = create_gate(gate_code)
        return jsonify(gate), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError as exc:
        msg = str(exc).lower()
        if "gate_configs.gate_code" in msg:
            return jsonify({"error": "gate_code already exists"}), 409
        return jsonify({"error": "integrity constraint failed"}), 409
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500


@app.route("/api/gates/<int:gate_id>/doors", methods=["POST"])
def api_set_gate_doors(gate_id: int):
    payload = request.get_json(silent=True) or {}
    door_numbers = payload.get("door_numbers", [])

    try:
        gate = set_gate_doors(gate_id, door_numbers)
        return jsonify(gate)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "door number already exists for this gate"}), 409
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500


@app.route("/api/export.csv")
def api_export_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "scanned_at_sgt", "qr_text", "source"])

    try:
        rows = list_scans(limit=200000)
    except sqlite3.Error as exc:
        return jsonify({"error": f"database error: {exc}"}), 500
    for row in reversed(rows):
        writer.writerow([row["id"], row["scanned_at_sgt"], row["qr_text"], row["source"]])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=qr_scans.csv"},
    )


def main():
    try:
        port = int(os.environ.get("PORT", "5053"))
    except ValueError:
        port = 5053
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


init_db()


if __name__ == "__main__":
    main()
