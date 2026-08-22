#!/usr/bin/env python3
"""Street Light Outage Tracker — Streamlit + SQLite / Turso."""

from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from pypdf import PdfReader

# Sequence: 1, 1a, 1a2, 2b1 — digits and letters only, must start with a digit.
SEQ_PATTERN = re.compile(r"^[0-9]+([a-z]+[0-9]*)*$", re.IGNORECASE)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PDF_DIR = DATA_DIR / "pdfs"
DB_PATH = DATA_DIR / "tracker.db"

TICKET_TYPES = [
    "Outage",
    "Damage",
    "Cable Theft",
    "Pedestal cut",
    "Knockdown",
    "Bad fixture",
    "Bad igniter",
    "UGT",
    "Vandalism",
    "Wire stolen",
    "Other",
]
POLE_MATERIALS = ["", "Aluminum", "Concrete", "Wood", "Steel", "Unknown"]
CONDITION_FLAGS = [
    ("knockdown", "Knocked down"),
    ("bad_fixture", "Bad fixture — replace"),
    ("bad_igniter", "Bad igniter"),
    ("ugt", "UGT (underground / that light only)"),
    ("vandalism", "Vandalized"),
    ("wire_stolen", "Wire stolen from this light"),
]
# Stored values stay short; labels match Milwaukee DPW language.
CIRCUIT_TYPE_LABELS = {
    "series": "Series (legacy constant-current)",
    "multiple": "Multiple / Parallel LED",
    "unknown": "Unknown",
}
CIRCUIT_TYPES = list(CIRCUIT_TYPE_LABELS.keys())

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS circuits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        circuit_number TEXT NOT NULL UNIQUE,
        circuit_type TEXT NOT NULL DEFAULT 'unknown',
        description TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS circuit_lights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        circuit_id INTEGER NOT NULL,
        light_number TEXT NOT NULL,
        map_number TEXT,
        street TEXT,
        side TEXT,
        nth INTEGER,
        from_dir TEXT,
        cross_street TEXT,
        sequence TEXT,
        location_note TEXT,
        pole_material TEXT,
        pole_height TEXT,
        fixture_type TEXT,
        UNIQUE(circuit_id, light_number),
        FOREIGN KEY (circuit_id) REFERENCES circuits(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS light_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        circuit_number TEXT NOT NULL,
        light_number TEXT NOT NULL,
        map_number TEXT,
        ticket_id INTEGER,
        event_type TEXT NOT NULL,
        knockdown INTEGER NOT NULL DEFAULT 0,
        bad_fixture INTEGER NOT NULL DEFAULT 0,
        bad_igniter INTEGER NOT NULL DEFAULT 0,
        ugt INTEGER NOT NULL DEFAULT 0,
        vandalism INTEGER NOT NULL DEFAULT 0,
        wire_stolen INTEGER NOT NULL DEFAULT 0,
        pole_material TEXT,
        pole_height TEXT,
        fixture_type TEXT,
        notes TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_light_events_light ON light_events(circuit_number, light_number)",
    "CREATE INDEX IF NOT EXISTS idx_light_events_map ON light_events(map_number)",
    """
    CREATE TABLE IF NOT EXISTS circuit_pdfs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        circuit_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        filepath TEXT NOT NULL,
        extracted_text TEXT,
        uploaded_at TEXT NOT NULL,
        FOREIGN KEY (circuit_id) REFERENCES circuits(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_type TEXT NOT NULL,
        circuit_number TEXT NOT NULL,
        light_number TEXT,
        map_number TEXT,
        lub TEXT,
        fud TEXT,
        pedestal_cut INTEGER NOT NULL DEFAULT 0,
        location TEXT,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        is_flagged INTEGER NOT NULL DEFAULT 0,
        flag_reason TEXT,
        parent_ticket_id INTEGER,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        completion_notes TEXT,
        FOREIGN KEY (parent_ticket_id) REFERENCES tickets(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_circuit ON tickets(circuit_number)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_light ON tickets(light_number)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(created_at)",
]


def circuit_label(code: str | None) -> str:
    return CIRCUIT_TYPE_LABELS.get(code or "unknown", code or "unknown")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)


def _secret(name: str) -> str | None:
    """Read from Streamlit secrets, then environment."""
    try:
        val = st.secrets.get(name)
        if val:
            return str(val)
    except Exception:
        pass
    return os.environ.get(name) or None


def using_turso() -> bool:
    return bool(_secret("TURSO_DATABASE_URL") and _secret("TURSO_AUTH_TOKEN"))


def _auth_passwords() -> dict[str, str]:
    """
    Secrets options (any one works):
      APP_PASSWORD = "shared-code"
      or
      [passwords]
      crew = "code1"
      supervisor = "code2"
    """
    out: dict[str, str] = {}
    single = _secret("APP_PASSWORD")
    if single:
        out["crew"] = single
    try:
        block = st.secrets.get("passwords")
        if block:
            for k, v in dict(block).items():
                if v:
                    out[str(k)] = str(v)
    except Exception:
        pass
    return out


def require_login() -> bool:
    """Gate the app behind a password from secrets. Returns True if allowed."""
    passwords = _auth_passwords()
    if not passwords:
        # No password configured — allow access (local dev convenience).
        # On Streamlit Cloud you should always set APP_PASSWORD.
        return True

    if st.session_state.get("authenticated"):
        with st.sidebar:
            who = st.session_state.get("auth_user", "crew")
            st.caption(f"Signed in as **{who}**")
            if st.button("Log out"):
                st.session_state.authenticated = False
                st.session_state.auth_user = None
                st.rerun()
        return True

    st.title("Street Light Tracker")
    st.caption("Sign in to continue")
    with st.form("login"):
        user = st.text_input("Name / role (optional)", placeholder="crew")
        pwd = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in", type="primary")
    if ok:
        user_key = (user or "crew").strip() or "crew"
        # Accept either: exact user match, or the shared APP_PASSWORD for any name
        expected = passwords.get(user_key) or passwords.get("crew")
        if expected and pwd == expected:
            st.session_state.authenticated = True
            st.session_state.auth_user = user_key
            st.rerun()
        st.error("Wrong password.")
    st.info("Ask your supervisor for the app password.")
    return False


def get_conn():
    """Turso (cloud) if credentials exist, otherwise local SQLite."""
    url = _secret("TURSO_DATABASE_URL")
    token = _secret("TURSO_AUTH_TOKEN")
    if url and token:
        import libsql

        conn = libsql.connect(database=url, auth_token=token)
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        return conn

    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _run_sql(conn, sql: str, params: tuple | list | None = None):
    if params is None:
        return conn.execute(sql)
    return conn.execute(sql, params)


def rows_as_dicts(cursor) -> list[dict]:
    """Normalize fetchall() results for sqlite3 and libsql/Turso."""
    raw = cursor.fetchall()
    if not raw:
        return []
    first = raw[0]
    if isinstance(first, sqlite3.Row):
        return [dict(r) for r in raw]
    if isinstance(first, dict):
        return list(raw)
    cols = []
    try:
        cols = [d[0] for d in cursor.description]
    except Exception:
        pass
    if cols:
        return [dict(zip(cols, r)) for r in raw]
    return [{"_col0": r[0]} if isinstance(r, (tuple, list)) and len(r) == 1 else {"row": r} for r in raw]


def row_as_dict(cursor, row) -> dict | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if isinstance(row, dict):
        return row
    cols = []
    try:
        cols = [d[0] for d in cursor.description]
    except Exception:
        pass
    if cols and isinstance(row, (tuple, list)):
        return dict(zip(cols, row))
    return None


def q_all(conn, sql: str, params: tuple | list | None = None) -> list[dict]:
    cur = _run_sql(conn, sql, params)
    return rows_as_dicts(cur)


def q_one(conn, sql: str, params: tuple | list | None = None) -> dict | None:
    cur = _run_sql(conn, sql, params)
    return row_as_dict(cur, cur.fetchone())


def row_get(row, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def init_db() -> None:
    conn = get_conn()
    for stmt in SCHEMA_STATEMENTS:
        _run_sql(conn, stmt)
    conn.commit()
    _migrate_tickets(conn)
    _migrate_lights(conn)
    conn.close()


def _pragma_cols(conn, table: str) -> set[str]:
    cols = set()
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        for row in rows:
            cols.add(row[1] if not isinstance(row, dict) else row.get("name") or row.get("name", row[1]))
    except Exception:
        pass
    # Turso may not support PRAGMA the same way; ignore
    clean = set()
    for c in cols:
        if isinstance(c, str):
            clean.add(c)
    return clean


def _migrate_tickets(conn) -> None:
    try:
        cols = _pragma_cols(conn, "tickets")
        adds = {
            "lub": "ALTER TABLE tickets ADD COLUMN lub TEXT",
            "fud": "ALTER TABLE tickets ADD COLUMN fud TEXT",
            "pedestal_cut": "ALTER TABLE tickets ADD COLUMN pedestal_cut INTEGER NOT NULL DEFAULT 0",
            "map_number": "ALTER TABLE tickets ADD COLUMN map_number TEXT",
        }
        for name, sql in adds.items():
            if name not in cols:
                try:
                    conn.execute(sql)
                except Exception:
                    pass
        conn.commit()
    except Exception:
        pass


def _migrate_lights(conn) -> None:
    try:
        cols = _pragma_cols(conn, "circuit_lights")
        adds = {
            "map_number": "ALTER TABLE circuit_lights ADD COLUMN map_number TEXT",
            "street": "ALTER TABLE circuit_lights ADD COLUMN street TEXT",
            "side": "ALTER TABLE circuit_lights ADD COLUMN side TEXT",
            "nth": "ALTER TABLE circuit_lights ADD COLUMN nth INTEGER",
            "from_dir": "ALTER TABLE circuit_lights ADD COLUMN from_dir TEXT",
            "cross_street": "ALTER TABLE circuit_lights ADD COLUMN cross_street TEXT",
            "pole_material": "ALTER TABLE circuit_lights ADD COLUMN pole_material TEXT",
            "pole_height": "ALTER TABLE circuit_lights ADD COLUMN pole_height TEXT",
            "fixture_type": "ALTER TABLE circuit_lights ADD COLUMN fixture_type TEXT",
        }
        for name, sql in adds.items():
            if name not in cols:
                try:
                    conn.execute(sql)
                except Exception:
                    pass
        conn.commit()
    except Exception:
        pass


SIDES = ["", "N", "S", "E", "W"]
DIRS = ["", "N", "S", "E", "W"]


def format_callout(street: str, side: str, nth, from_dir: str, cross: str) -> str:
    """1 W 1 N Mason -> 1W-1N-Mason"""
    street = (street or "").strip()
    side = (side or "").strip().upper()
    from_dir = (from_dir or "").strip().upper()
    cross = (cross or "").strip()
    nth_s = str(nth).strip() if nth not in (None, "") else ""
    if not street:
        return ""
    parts = [street.replace(" ", "")]
    if side:
        parts[0] = parts[0] + side
    mid = ""
    if nth_s:
        mid += nth_s
    if from_dir:
        mid += from_dir
    bits = [parts[0]]
    if mid:
        bits.append(mid)
    if cross:
        bits.append(cross.replace(" ", "-"))
    return "-".join(bits)


def spoken_callout(street: str, side: str, nth, from_dir: str, cross: str) -> str:
    """1 W 1 N Mason"""
    bits = []
    if street:
        bits.append(str(street).strip())
    if side:
        bits.append(str(side).strip().upper())
    if nth not in (None, ""):
        bits.append(str(nth).strip())
    if from_dir:
        bits.append(str(from_dir).strip().upper())
    if cross:
        bits.append(str(cross).strip())
    return " ".join(bits)


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def extract_pdf_text(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        parts = []
        for page in reader.pages:
            text = page.extract_text() or ""
            parts.append(text)
        return "\n".join(parts).strip()
    except Exception as exc:
        return f"[Could not extract text: {exc}]"


def get_circuit(conn, circuit_number: str) -> dict | None:
    return q_one(
        conn,
        "SELECT * FROM circuits WHERE circuit_number = ?",
        (circuit_number.strip(),),
    )


def get_or_create_circuit(
    conn, circuit_number: str, circuit_type: str = "unknown"
) -> dict:
    row = get_circuit(conn, circuit_number)
    if row:
        return row
    conn.execute(
        "INSERT INTO circuits (circuit_number, circuit_type, created_at) VALUES (?, ?, ?)",
        (circuit_number.strip(), circuit_type, now_iso()),
    )
    conn.commit()
    created = get_circuit(conn, circuit_number)
    return created or {"circuit_number": circuit_number.strip(), "id": None}


def normalize_seq(value: str | int | None) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("-", "")


def is_valid_sequence(value: str | int | None) -> bool:
    """Regex validation: 1, 1a, 1a2, 2b1 — no symbols or leading letters."""
    s = normalize_seq(value)
    if not s:
        return True  # empty allowed (optional field)
    return bool(SEQ_PATTERN.fullmatch(s))


def validate_sequence_or_error(value: str | int | None) -> str | None:
    """Return error message or None if OK."""
    s = str(value or "").strip()
    if not s:
        return None
    if is_valid_sequence(s):
        return None
    return (
        f"Invalid sequence '{s}'. Use forms like 1, 1a, 1b, 1a1, 1a2 "
        "(digits and letters only, must start with a number)."
    )


def seq_tokens(value: str | int | None) -> list:
    """Turn 1a2 into [1, 'a', 2] so branches compare correctly."""
    s = normalize_seq(value)
    if not s:
        return []
    tokens: list = []
    i = 0
    while i < len(s):
        if s[i].isdigit():
            j = i
            while j < len(s) and s[j].isdigit():
                j += 1
            tokens.append(int(s[i:j]))
            i = j
        elif s[i].isalpha():
            j = i
            while j < len(s) and s[j].isalpha():
                j += 1
            tokens.append(s[i:j])
            i = j
        else:
            i += 1
    return tokens


def seq_sort_key(value: str | int | None) -> tuple:
    parts = []
    for t in seq_tokens(value):
        if isinstance(t, int):
            parts.append((0, t, ""))
        else:
            parts.append((1, 0, t))
    return tuple(parts)


def branch_label(value: str | int | None) -> str:
    """Visual branch indicator from sequence (1a2 → A leg, 1 → Main / feed)."""
    tokens = seq_tokens(value)
    if not tokens:
        return "—"
    for t in tokens:
        if isinstance(t, str) and t:
            letter = t[0].upper()
            return f"{letter} leg"
    return "Main / feed"


def branch_depth(value: str | int | None) -> int:
    """How deep in the tree (number of letter segments)."""
    return sum(1 for t in seq_tokens(value) if isinstance(t, str))


def format_sequence_tree(lights: list[dict]) -> str:
    """
    Hierarchical tree view of lights by sequence.
    Example:
      1 — #12  [Main / feed]
      ├── 1a — #18  [A leg]
      │   └── 1a1 — #19  [A leg]
      └── 1b — #21  [B leg]
    """
    if not lights:
        return "(no lights)"

    items = []
    for r in lights:
        seq = normalize_seq(r.get("sequence"))
        if not seq:
            continue
        toks = seq_tokens(seq)
        items.append(
            {
                "seq": seq,
                "tokens": toks,
                "depth": max(0, len(toks) - 1),
                "light": r.get("light_number") or "?",
                "loc": (r.get("location_note") or "").strip(),
                "branch": branch_label(seq),
            }
        )
    items.sort(key=lambda x: seq_sort_key(x["seq"]))

    def has_later_sibling(idx: int, depth: int) -> bool:
        cur = items[idx]["tokens"]
        parent = cur[:depth]
        for j in range(idx + 1, len(items)):
            ot = items[j]["tokens"]
            if len(ot) <= depth:
                if ot[:depth] != parent and len(ot) < depth + 1:
                    return False
                if len(ot) < depth:
                    return False
            if ot[:depth] != parent:
                return False
            if len(ot) > depth and ot[:depth] == parent:
                # another node under same parent
                if ot[: depth + 1] != cur[: depth + 1]:
                    return True
        return False

    def is_last_child(idx: int) -> bool:
        cur = items[idx]["tokens"]
        depth = items[idx]["depth"]
        if depth == 0:
            return True
        parent = cur[:depth]
        for j in range(idx + 1, len(items)):
            ot = items[j]["tokens"]
            if len(ot) < depth:
                return True
            if ot[:depth] != parent:
                return True
            if len(ot) == depth + 0:  # pragma: no cover
                pass
            if len(ot) >= depth and ot[:depth] == parent:
                # peer or descendant of peer
                if len(ot) >= depth + 1 and ot[:depth] == parent and ot[: depth + 1] != cur[: depth + 1]:
                    return False
                if len(ot) == len(cur) and ot[:depth] == parent and ot != cur:
                    return False
        return True

    lines: list[str] = []
    for i, item in enumerate(items):
        depth = item["depth"]
        if depth == 0:
            prefix = ""
        else:
            parts = []
            for d in range(1, depth):
                parts.append("│   " if has_later_sibling(i, d) else "    ")
            parts.append("└── " if is_last_child(i) else "├── ")
            prefix = "".join(parts)

        loc_bit = f" — {item['loc']}" if item["loc"] else ""
        lines.append(
            f"{prefix}{item['seq']} — #{item['light']}  [{item['branch']}]{loc_bit}"
        )

    # Unsequenced lights at the end
    orphan = [
        r
        for r in lights
        if not normalize_seq(r.get("sequence"))
    ]
    for r in orphan:
        loc = (r.get("location_note") or "").strip()
        loc_bit = f" — {loc}" if loc else ""
        lines.append(f"(no seq) — #{r.get('light_number') or '?'}  [—]{loc_bit}")

    return "\n".join(lines)


def same_branch_at_or_after(newer, older) -> bool:
    """True if newer is on older's branch at or past older (1a2 after 1a, not after 1b)."""
    a = seq_tokens(older)
    b = seq_tokens(newer)
    if not a or not b:
        return False
    if b == a:
        return True
    return len(b) > len(a) and b[: len(a)] == a


def same_branch_after(newer, older) -> bool:
    a = seq_tokens(older)
    b = seq_tokens(newer)
    if not a or not b:
        return False
    return len(b) > len(a) and b[: len(a)] == a


def ranges_overlap_same_branch(new_lub, new_fud, old_lub, old_fud) -> bool:
    """Overlap only if both ranges sit on a shared branch prefix."""
    pairs = [
        (new_lub, old_lub),
        (new_lub, old_fud),
        (new_fud, old_lub),
        (new_fud, old_fud),
    ]
    return any(
        same_branch_at_or_after(x, y) or same_branch_at_or_after(y, x) for x, y in pairs
    )


def get_light_sequence(conn, circuit_number: str, light_number: str) -> str | None:
    row = q_one(
        conn,
        """
        SELECT cl.sequence
        FROM circuit_lights cl
        JOIN circuits c ON c.id = cl.circuit_id
        WHERE c.circuit_number = ? AND cl.light_number = ?
        """,
        (circuit_number.strip(), str(light_number).strip()),
    )
    if row and row.get("sequence") not in (None, ""):
        return normalize_seq(row["sequence"])
    return None


def _norm(value: str | None) -> str:
    return (value or "").strip()


def ticket_units(row) -> list[str]:
    units = []
    for key in ("light_number", "lub", "fud"):
        val = _norm(str(row_get(row, key, "") or ""))
        if val:
            units.append(val)
    return units


def check_duplicate(
    conn,
    circuit_number: str,
    light_number: str | None,
    lub: str | None = None,
    fud: str | None = None,
) -> tuple[bool, str, int | None]:
    """Flag same-break / LUB-FUD overlap on series or parallel (pedestal cut)."""
    circuit_number = circuit_number.strip()
    light_number = _norm(light_number)
    lub = _norm(lub)
    fud = _norm(fud)
    new_units = [u for u in (light_number, lub, fud) if u]

    active = q_all(
        conn,
        """
        SELECT id, light_number, lub, fud, ticket_type, created_at, pedestal_cut
        FROM tickets
        WHERE status = 'active' AND circuit_number = ?
        ORDER BY created_at
        """,
        (circuit_number,),
    )

    if not active:
        return False, "", None

    circuit = get_circuit(conn, circuit_number)
    ctype = (circuit.get("circuit_type") if circuit else "unknown") or "unknown"

    for t in active:
        existing = ticket_units(t)
        if new_units and set(new_units) & set(existing):
            reason = (
                f"Same unit already on ticket #{t.get('id')} "
                f"({t.get('ticket_type')}, LUB {t.get('lub') or '—'} / FUD {t.get('fud') or '—'}, {t.get('created_at')})"
            )
            return True, reason, t.get("id")

        new_seqs = [get_light_sequence(conn, circuit_number, u) for u in new_units]
        new_seqs = [s for s in new_seqs if s is not None]
        t_fud_seq = get_light_sequence(conn, circuit_number, t.get("fud") or "")
        t_lub_seq = get_light_sequence(conn, circuit_number, t.get("lub") or "")
        t_light_seq = get_light_sequence(conn, circuit_number, t.get("light_number") or "")
        break_after = t_lub_seq
        dark_from = t_fud_seq if t_fud_seq is not None else t_light_seq

        if new_seqs and (dark_from is not None or break_after is not None):
            for ns in new_seqs:
                after_lub = break_after is not None and same_branch_after(ns, break_after)
                at_or_after_fud = dark_from is not None and same_branch_at_or_after(
                    ns, dark_from
                )
                if after_lub or at_or_after_fud:
                    reason = (
                        f"Same break as ticket #{t.get('id')} — new unit is on the same "
                        f"leg at/after LUB {t.get('lub') or '—'} / FUD "
                        f"{t.get('fud') or t.get('light_number') or '—'}. "
                        f"Other legs (e.g. 1b vs 1a) are not treated as downstream."
                    )
                    return True, reason, t.get("id")

        new_fud_seq = get_light_sequence(conn, circuit_number, fud) if fud else None
        new_lub_seq = get_light_sequence(conn, circuit_number, lub) if lub else None
        if (
            new_lub_seq is not None
            and new_fud_seq is not None
            and t_lub_seq is not None
            and t_fud_seq is not None
            and ranges_overlap_same_branch(new_lub_seq, new_fud_seq, t_lub_seq, t_fud_seq)
        ):
            reason = (
                f"LUB/FUD range overlaps ticket #{t.get('id')} on the same leg "
                f"(existing LUB {t.get('lub')} / FUD {t.get('fud')})."
            )
            return True, reason, t.get("id")

    ids = ", ".join(f"#{t.get('id')}" for t in active)
    summary = "; ".join(
        f"#{t.get('id')} LUB {t.get('lub') or '—'} FUD {t.get('fud') or t.get('light_number') or '—'}"
        for t in active
    )
    extra = (
        "Same circuit already has an active call. "
        "If this is the same pedestal cut or series break, use the existing LUB/FUD."
    )
    if ctype == "multiple":
        extra += " Parallel LED: a single lamp can fail alone, but a pedestal cut still darkens everything after the cut."
    elif ctype == "series":
        extra += " Series: everything after the break stays dark."
    reason = f"{len(active)} active ticket(s) on circuit {circuit_number} ({ids}). {summary}. {extra}"
    return True, reason, active[0].get("id")


def insert_ticket(
    conn: sqlite3.Connection,
    ticket_type: str,
    circuit_number: str,
    light_number: str,
    location: str,
    description: str,
    lub: str = "",
    fud: str = "",
    pedestal_cut: bool = False,
    map_number: str = "",
) -> tuple[int, bool, str]:
    flagged, reason, parent = check_duplicate(conn, circuit_number, light_number, lub, fud)
    cur = conn.execute(
        """
        INSERT INTO tickets (
            ticket_type, circuit_number, light_number, map_number, lub, fud, pedestal_cut,
            location, description,
            status, is_flagged, flag_reason, parent_ticket_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
        """,
        (
            ticket_type,
            circuit_number.strip(),
            _norm(light_number) or None,
            _norm(map_number) or None,
            _norm(lub) or None,
            _norm(fud) or None,
            1 if pedestal_cut else 0,
            location.strip() or None,
            description.strip() or None,
            1 if flagged else 0,
            reason or None,
            parent,
            now_iso(),
        ),
    )
    conn.commit()
    tid = cur.lastrowid
    return tid, flagged, reason


def log_light_event(
    conn,
    circuit_number: str,
    light_number: str,
    map_number: str = "",
    ticket_id: int | None = None,
    event_type: str = "update",
    flags: dict | None = None,
    pole_material: str = "",
    pole_height: str = "",
    fixture_type: str = "",
    notes: str = "",
) -> None:
    flags = flags or {}
    if not _norm(circuit_number) or not _norm(light_number):
        return
    conn.execute(
        """
        INSERT INTO light_events (
            circuit_number, light_number, map_number, ticket_id, event_type,
            knockdown, bad_fixture, bad_igniter, ugt, vandalism, wire_stolen,
            pole_material, pole_height, fixture_type, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            circuit_number.strip(),
            _norm(light_number),
            _norm(map_number) or None,
            ticket_id,
            event_type,
            1 if flags.get("knockdown") else 0,
            1 if flags.get("bad_fixture") else 0,
            1 if flags.get("bad_igniter") else 0,
            1 if flags.get("ugt") else 0,
            1 if flags.get("vandalism") else 0,
            1 if flags.get("wire_stolen") else 0,
            _norm(pole_material) or None,
            _norm(pole_height) or None,
            _norm(fixture_type) or None,
            _norm(notes) or None,
            now_iso(),
        ),
    )
    circ = get_circuit(conn, circuit_number)
    if circ and circ.get("id") and (pole_material or pole_height or fixture_type or map_number):
        conn.execute(
            """
            UPDATE circuit_lights
            SET
                map_number = COALESCE(?, map_number),
                pole_material = COALESCE(?, pole_material),
                pole_height = COALESCE(?, pole_height),
                fixture_type = COALESCE(?, fixture_type)
            WHERE circuit_id = ? AND light_number = ?
            """,
            (
                _norm(map_number) or None,
                _norm(pole_material) or None,
                _norm(pole_height) or None,
                _norm(fixture_type) or None,
                circ.get("id"),
                _norm(light_number),
            ),
        )
    conn.commit()


def complete_ticket(conn: sqlite3.Connection, ticket_id: int, notes: str) -> None:
    conn.execute(
        """
        UPDATE tickets
        SET status = 'completed', completed_at = ?, completion_notes = ?
        WHERE id = ?
        """,
        (now_iso(), notes.strip() or None, ticket_id),
    )
    conn.commit()


def reopen_ticket(conn: sqlite3.Connection, ticket_id: int) -> None:
    conn.execute(
        """
        UPDATE tickets
        SET status = 'active', completed_at = NULL, completion_notes = NULL
        WHERE id = ?
        """,
        (ticket_id,),
    )
    conn.commit()


def tickets_df(conn, status: str | None = None) -> pd.DataFrame:
    if status:
        rows = q_all(
            conn,
            "SELECT * FROM tickets WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
    else:
        rows = q_all(conn, "SELECT * FROM tickets ORDER BY created_at DESC")
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def filter_tickets(
    df: pd.DataFrame,
    q: str,
    circuit: str,
    light: str,
    ttype: str,
    start: date | None,
    end: date | None,
) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if circuit:
        out = out[out["circuit_number"].astype(str).str.contains(circuit, case=False, na=False)]
    if light:
        hit = (
            out["light_number"].astype(str).str.contains(light, case=False, na=False)
            | out.get("map_number", pd.Series("", index=out.index)).astype(str).str.contains(light, case=False, na=False)
            | out.get("lub", pd.Series("", index=out.index)).astype(str).str.contains(light, case=False, na=False)
            | out.get("fud", pd.Series("", index=out.index)).astype(str).str.contains(light, case=False, na=False)
        )
        out = out[hit]
    if ttype and ttype != "All":
        out = out[out["ticket_type"] == ttype]
    if q:
        blob = (
            out["circuit_number"].astype(str)
            + " "
            + out["light_number"].fillna("").astype(str)
            + " "
            + out.get("map_number", pd.Series("", index=out.index)).fillna("").astype(str)
            + " "
            + out.get("lub", pd.Series("", index=out.index)).fillna("").astype(str)
            + " "
            + out.get("fud", pd.Series("", index=out.index)).fillna("").astype(str)
            + " "
            + out["location"].fillna("").astype(str)
            + " "
            + out["description"].fillna("").astype(str)
            + " "
            + out["flag_reason"].fillna("").astype(str)
            + " "
            + out["completion_notes"].fillna("").astype(str)
        )
        out = out[blob.str.contains(q, case=False, na=False)]
    if start:
        out = out[pd.to_datetime(out["created_at"]) >= pd.Timestamp(start)]
    if end:
        out = out[pd.to_datetime(out["created_at"]) < pd.Timestamp(end) + pd.Timedelta(days=1)]
    return out


def page_new_call(conn: sqlite3.Connection) -> None:
    st.header("New call")
    st.caption(
        "**LUB** = last unit burning (still on). **FUD** = first unit dark. "
        "The break is between them — series fault or wires cut in a pedestal on a parallel LED circuit."
    )

    circuits = [
        r["circuit_number"]
        for r in q_all(conn, "SELECT circuit_number FROM circuits ORDER BY circuit_number")
    ]

    with st.form("new_call", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        ticket_type = c1.selectbox("Type", TICKET_TYPES)
        circuit_number = c2.text_input("Circuit number *", placeholder="e.g. T1S-A")
        map_number = c3.text_input("Map / plate #", placeholder="305 (can repeat on other streets)")
        st.caption(
            "Identify the **head** by street callout, not plate # alone. "
            "Example: west side of 1st, first light north of Mason → **1 W 1 N Mason** (`1W-1N-Mason`)."
        )
        a1, a2, a3, a4, a5 = st.columns(5)
        street = a1.text_input("Street", placeholder="1 or 1st")
        side = a2.selectbox("Side of street", SIDES)
        nth = a3.number_input("Nth light from cross", min_value=0, step=1, value=0)
        from_dir = a4.selectbox("Direction from cross", DIRS)
        cross = a5.text_input("Cross street", placeholder="Mason")
        callout_override = st.text_input(
            "Callout (optional if you filled the boxes)",
            placeholder="1W-1N-Mason or 1 W 1 N Mason",
        )
        l1, l2, l3 = st.columns(3)
        lub = l1.text_input("LUB — last unit burning", placeholder="1W-1N-Mason")
        fud = l2.text_input("FUD — first unit dark", placeholder="1W-2N-Mason")
        pedestal_cut = l3.checkbox("Wires cut in pedestal")
        location = st.text_input("Location / intersection", placeholder="1st & Mason")
        st.markdown("**Condition of this light**")
        f1, f2, f3 = st.columns(3)
        knockdown = f1.checkbox("Knocked down")
        bad_fixture = f2.checkbox("Bad fixture — replace")
        bad_igniter = f3.checkbox("Bad igniter")
        f4, f5, f6 = st.columns(3)
        ugt = f4.checkbox("UGT (this light only)")
        vandalism = f5.checkbox("Vandalized")
        wire_stolen = f6.checkbox("Wire stolen from this light")
        p1, p2, p3 = st.columns(3)
        pole_material = p1.selectbox("Pole type", POLE_MATERIALS)
        pole_height = p2.text_input("Pole height", placeholder="e.g. 30 ft")
        fixture_type = p3.text_input("Fixture type", placeholder="e.g. LED cobra / acorn")
        description = st.text_area("Notes", placeholder="LUB / FUD. Pedestal between them cut.")
        submitted = st.form_submit_button("Log call", type="primary")

    if submitted:
        if not circuit_number.strip():
            st.error("Circuit number is required.")
            return
        built = format_callout(street, side, nth if nth else "", from_dir, cross)
        spoken = spoken_callout(street, side, nth if nth else "", from_dir, cross)
        light_id = (callout_override or "").strip() or built or (map_number or "").strip()
        loc = location.strip() or spoken
        get_or_create_circuit(conn, circuit_number)
        flags = {
            "knockdown": knockdown or ticket_type == "Knockdown",
            "bad_fixture": bad_fixture or ticket_type == "Bad fixture",
            "bad_igniter": bad_igniter or ticket_type == "Bad igniter",
            "ugt": ugt or ticket_type == "UGT",
            "vandalism": vandalism or ticket_type == "Vandalism",
            "wire_stolen": wire_stolen or ticket_type == "Wire stolen",
        }
        extra = []
        for key, label in CONDITION_FLAGS:
            if flags.get(key):
                extra.append(label)
        if pole_material:
            extra.append(f"pole {pole_material} {pole_height}".strip())
        if fixture_type:
            extra.append(f"fixture {fixture_type}")
        desc = description
        if extra:
            desc = ((description or "").strip() + "\n" + "; ".join(extra)).strip()
        tid, flagged, reason = insert_ticket(
            conn,
            ticket_type,
            circuit_number,
            light_id,
            loc,
            desc,
            lub=lub,
            fud=fud,
            pedestal_cut=pedestal_cut or ticket_type == "Pedestal cut",
            map_number=map_number,
        )
        log_light_event(
            conn,
            circuit_number,
            light_id,
            map_number=map_number,
            ticket_id=tid,
            event_type=ticket_type,
            flags=flags,
            pole_material=pole_material,
            pole_height=pole_height,
            fixture_type=fixture_type,
            notes=desc,
        )
        if flagged:
            st.warning(f"Ticket #{tid} logged but **flagged**.\n\n{reason}")
        else:
            st.success(f"Ticket #{tid} logged as active.")
        st.rerun()

    if circuits:
        st.caption("Known circuits: " + ", ".join(circuits[:40]) + ("…" if len(circuits) > 40 else ""))


def page_active(conn: sqlite3.Connection) -> None:
    st.header("Active call log")
    df = tickets_df(conn, "active")

    c1, c2, c3, c4 = st.columns(4)
    q = c1.text_input("Search")
    circuit = c2.text_input("Circuit")
    light = c3.text_input("Light #")
    ttype = c4.selectbox("Type", ["All"] + TICKET_TYPES)

    filtered = filter_tickets(df, q, circuit, light, ttype, None, None)

    if filtered.empty:
        st.info("No active calls match.")
        return

    st.write(f"**{len(filtered)}** active call(s)")

    cols = [
        "id",
        "created_at",
        "ticket_type",
        "circuit_number",
        "light_number",
        "map_number",
        "lub",
        "fud",
        "pedestal_cut",
        "location",
        "description",
        "is_flagged",
        "flag_reason",
        "parent_ticket_id",
    ]
    show = filtered[[c for c in cols if c in filtered.columns]].rename(
        columns={
            "id": "Ticket",
            "created_at": "Logged",
            "ticket_type": "Type",
            "circuit_number": "Circuit",
            "light_number": "Callout",
            "map_number": "Map #",
            "lub": "LUB",
            "fud": "FUD",
            "pedestal_cut": "Pedestal cut",
            "location": "Location",
            "description": "Notes",
            "is_flagged": "Flagged",
            "flag_reason": "Flag reason",
            "parent_ticket_id": "Related #",
        }
    )
    show["Flagged"] = show["Flagged"].map({1: "YES", 0: ""})
    if "Pedestal cut" in show.columns:
        show["Pedestal cut"] = show["Pedestal cut"].map({1: "YES", 0: ""})
    st.dataframe(show, use_container_width=True, hide_index=True)

    st.subheader("Complete a call")
    ids = filtered["id"].tolist()
    col_a, col_b = st.columns([1, 2])
    with col_a:
        pick = st.selectbox("Ticket #", ids)
    with col_b:
        notes = st.text_input("Completion notes", placeholder="Replaced fuse / repaired cable / no trouble found")
    if st.button("Mark completed", type="primary"):
        complete_ticket(conn, int(pick), notes)
        st.success(f"Ticket #{pick} moved to history.")
        st.rerun()


def page_history(conn: sqlite3.Connection) -> None:
    st.header("History")
    df = tickets_df(conn)
    if df.empty:
        st.info("No tickets yet.")
        return

    c1, c2, c3, c4 = st.columns(4)
    q = c1.text_input("Keyword")
    circuit = c2.text_input("Circuit")
    light = c3.text_input("Light #")
    ttype = c4.selectbox("Type", ["All"] + TICKET_TYPES)

    c5, c6, c7 = st.columns(3)
    status = c5.selectbox("Status", ["All", "active", "completed"])
    start = c6.date_input("From", value=None)
    end = c7.date_input("To", value=None)

    work = df if status == "All" else df[df["status"] == status]
    filtered = filter_tickets(work, q, circuit, light, ttype, start, end)

    st.write(f"**{len(filtered)}** record(s)")
    if filtered.empty:
        return

    hcols = [
        "id",
        "status",
        "created_at",
        "completed_at",
        "ticket_type",
        "circuit_number",
        "light_number",
        "map_number",
        "lub",
        "fud",
        "pedestal_cut",
        "location",
        "description",
        "flag_reason",
        "completion_notes",
    ]
    show = filtered[[c for c in hcols if c in filtered.columns]].rename(
        columns={
            "id": "Ticket",
            "status": "Status",
            "created_at": "Logged",
            "completed_at": "Completed",
            "ticket_type": "Type",
            "circuit_number": "Circuit",
            "light_number": "Callout",
            "map_number": "Map #",
            "lub": "LUB",
            "fud": "FUD",
            "pedestal_cut": "Pedestal cut",
            "location": "Location",
            "description": "Notes",
            "flag_reason": "Flag",
            "completion_notes": "Closeout",
        }
    )
    if "Pedestal cut" in show.columns:
        show["Pedestal cut"] = show["Pedestal cut"].map({1: "YES", 0: ""})
    st.dataframe(show, use_container_width=True, hide_index=True)

    csv_bytes = show.to_csv(index=False).encode("utf-8")
    st.download_button("Download results as CSV", csv_bytes, "streetlight_history.csv", "text/csv")

    completed_ids = filtered.loc[filtered["status"] == "completed", "id"].tolist()
    if completed_ids:
        st.subheader("Reopen a completed ticket")
        rid = st.selectbox("Ticket # to reopen", completed_ids)
        if st.button("Reopen"):
            reopen_ticket(conn, int(rid))
            st.success(f"Ticket #{rid} is active again.")
            st.rerun()


def page_circuits(conn: sqlite3.Connection) -> None:
    st.header("Circuits & maps")
    st.caption(
        "Upload circuit PDFs and enter light order from the source. "
        "Sequences can be nested (`1`, `1a`, `1a1`, `1b`) so parallel legs stay separate. "
        "Same-leg downstream only is flagged as the same break."
    )

    tab_list, tab_add, tab_import, tab_pdf = st.tabs(
        ["Circuit list", "Add / edit circuit", "Import light order", "Upload PDF"]
    )

    with tab_list:
        circuits = q_all(
            conn,
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM circuit_lights WHERE circuit_id = c.id) AS light_count,
                   (SELECT COUNT(*) FROM circuit_pdfs WHERE circuit_id = c.id) AS pdf_count
            FROM circuits c
            ORDER BY c.circuit_number
            """,
        )
        if not circuits:
            st.info("No circuits defined yet. Add one, or log a call — the circuit will be created automatically.")
        else:
            rows = []
            for c in circuits:
                rows.append(
                    {
                        "Circuit": c.get("circuit_number"),
                        "Type": circuit_label(c.get("circuit_type")),
                        "Lights mapped": c.get("light_count"),
                        "PDFs": c.get("pdf_count"),
                        "Notes": c.get("description") or "",
                    }
                )
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            pick = st.selectbox(
                "View lights on circuit",
                [c.get("circuit_number") for c in circuits],
            )
            lights = q_all(
                conn,
                """
                SELECT cl.sequence, cl.light_number, cl.map_number, cl.street, cl.side,
                       cl.nth, cl.from_dir, cl.cross_street, cl.location_note,
                       cl.pole_material, cl.pole_height, cl.fixture_type
                FROM circuit_lights cl
                JOIN circuits c ON c.id = cl.circuit_id
                WHERE c.circuit_number = ?
                """,
                (pick,),
            )
            lights = sorted(
                lights,
                key=lambda r: (seq_sort_key(r.get("sequence")), str(r.get("light_number") or "")),
            )
            if lights:
                st.subheader("Branch tree")
                st.code(format_sequence_tree(lights), language=None)

                table_rows = []
                for r in lights:
                    seq = r.get("sequence")
                    table_rows.append(
                        {
                            "Seq": seq or "—",
                            "Branch": branch_label(seq),
                            "Callout": r.get("light_number"),
                            "Map #": r.get("map_number") or "",
                            "Spoken": spoken_callout(
                                r.get("street") or "",
                                r.get("side") or "",
                                r.get("nth"),
                                r.get("from_dir") or "",
                                r.get("cross_street") or "",
                            ),
                            "Location": r.get("location_note") or "",
                            "Pole": " ".join(
                                x for x in (r.get("pole_material"), r.get("pole_height")) if x
                            ),
                            "Fixture": r.get("fixture_type") or "",
                        }
                    )
                st.subheader("Light list")
                st.dataframe(
                    pd.DataFrame(table_rows),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.caption("No light order stored for this circuit yet.")

            pdfs = q_all(
                conn,
                """
                SELECT p.id, p.filename, p.uploaded_at, length(p.extracted_text) AS chars
                FROM circuit_pdfs p
                JOIN circuits c ON c.id = p.circuit_id
                WHERE c.circuit_number = ?
                ORDER BY p.uploaded_at DESC
                """,
                (pick,),
            )
            if pdfs:
                st.write("PDFs")
                for p in pdfs:
                    with st.expander(f"{p.get('filename')}  ·  {p.get('uploaded_at')}"):
                        text_row = q_one(
                            conn,
                            "SELECT extracted_text FROM circuit_pdfs WHERE id = ?",
                            (p.get("id"),),
                        )
                        text = (text_row or {}).get("extracted_text")
                        st.text_area(
                            "Extracted text",
                            text or "(no text)",
                            height=200,
                            key=f"pdftext_{p.get('id')}",
                        )

            search_pdf = st.text_input("Search extracted PDF text across all circuits")
            if search_pdf.strip():
                hits = q_all(
                    conn,
                    """
                    SELECT c.circuit_number, p.filename, p.extracted_text
                    FROM circuit_pdfs p
                    JOIN circuits c ON c.id = p.circuit_id
                    WHERE p.extracted_text LIKE ?
                    """,
                    (f"%{search_pdf.strip()}%",),
                )
                if not hits:
                    st.write("No matches.")
                else:
                    for h in hits:
                        st.markdown(
                            f"**Circuit {h.get('circuit_number')}** — {h.get('filename')}"
                        )
                        st.caption((h.get("extracted_text") or "")[:500])

    with tab_add:
        with st.form("add_circuit"):
            cn = st.text_input("Circuit number")
            ct = st.selectbox(
                "Circuit type",
                CIRCUIT_TYPES,
                format_func=circuit_label,
                help="Series: a fault can take out every light after the break. "
                "Multiple / Parallel LED: one fixture can go dark without killing the rest of the circuit.",
            )
            desc = st.text_area("Description / notes")
            save = st.form_submit_button("Save circuit")
        if save and cn.strip():
            existing = get_circuit(conn, cn)
            if existing:
                conn.execute(
                    "UPDATE circuits SET circuit_type = ?, description = ? WHERE id = ?",
                    (ct, desc.strip() or None, existing["id"]),
                )
                conn.commit()
                st.success(f"Updated circuit {cn.strip()}.")
            else:
                get_or_create_circuit(conn, cn, ct)
                conn.execute(
                    "UPDATE circuits SET description = ? WHERE circuit_number = ?",
                    (desc.strip() or None, cn.strip()),
                )
                conn.commit()
                st.success(f"Added circuit {cn.strip()}.")
            st.rerun()

        st.subheader("Add a single light to the sequence")
        st.caption(
            "Unique ID is the **callout** (1W-1N-Mason), not map #305. "
            "Sequence: `1`, `1a`, `1a1`, `1b`."
        )
        with st.form("add_light"):
            cn2 = st.text_input("Circuit number", key="al_cn")
            map_n = st.text_input("Map / plate #", placeholder="305")
            b1, b2, b3, b4, b5 = st.columns(5)
            st_street = b1.text_input("Street", placeholder="1")
            st_side = b2.selectbox("Side", SIDES, key="al_side")
            st_nth = b3.number_input("Nth from cross", min_value=0, step=1, value=0, key="al_nth")
            st_dir = b4.selectbox("Dir from cross", DIRS, key="al_dir")
            st_cross = b5.text_input("Cross street", placeholder="Mason")
            ln = st.text_input("Callout override (optional)", placeholder="leave blank to auto-build 1W-1N-Mason")
            seq = st.text_input(
                "Sequence from source",
                placeholder="1   or  1a   or  1a2",
                help="Use 1, 1a, 1b, 1a1, 1a2 for splits. Same-leg only is treated as downstream.",
            )
            loc = st.text_input("Location note")
            p1, p2, p3 = st.columns(3)
            pole_material = p1.selectbox("Pole type", POLE_MATERIALS, key="al_pole")
            pole_height = p2.text_input("Pole height", placeholder="30 ft", key="al_ht")
            fixture_type = p3.text_input("Fixture type", placeholder="LED cobra", key="al_fx")
            add_l = st.form_submit_button("Add light")
        if add_l and cn2.strip():
            built = format_callout(st_street, st_side, st_nth if st_nth else "", st_dir, st_cross)
            callout = (ln or "").strip() or built
            if not callout:
                st.error("Enter a callout or street + side + cross so two #305s stay distinct.")
            else:
                err = validate_sequence_or_error(seq)
                if err:
                    st.error(err)
                else:
                    circ = get_or_create_circuit(conn, cn2)
                    seq_val = normalize_seq(seq) or None
                    br = branch_label(seq_val) if seq_val else "—"
                    nth_val = int(st_nth) if st_nth else None
                    try:
                        conn.execute(
                            """
                            INSERT INTO circuit_lights (
                                circuit_id, light_number, map_number, street, side, nth,
                                from_dir, cross_street, sequence, location_note,
                                pole_material, pole_height, fixture_type
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(circuit_id, light_number) DO UPDATE SET
                                map_number = excluded.map_number,
                                street = excluded.street,
                                side = excluded.side,
                                nth = excluded.nth,
                                from_dir = excluded.from_dir,
                                cross_street = excluded.cross_street,
                                sequence = excluded.sequence,
                                location_note = excluded.location_note,
                                pole_material = excluded.pole_material,
                                pole_height = excluded.pole_height,
                                fixture_type = excluded.fixture_type
                            """,
                            (
                                circ.get("id"),
                                callout,
                                (map_n or "").strip() or None,
                                (st_street or "").strip() or None,
                                (st_side or "").strip() or None,
                                nth_val,
                                (st_dir or "").strip() or None,
                                (st_cross or "").strip() or None,
                                seq_val,
                                loc.strip() or spoken_callout(st_street, st_side, nth_val, st_dir, st_cross) or None,
                                (pole_material or "").strip() or None,
                                (pole_height or "").strip() or None,
                                (fixture_type or "").strip() or None,
                            ),
                        )
                        conn.commit()
                        st.success(
                            f"Saved **{callout}** (map #{map_n or '—'}) on {cn2.strip()} "
                            f"at **{seq_val or '(none)'}** [{br}]."
                        )
                    except sqlite3.Error as e:
                        st.error(str(e))

    with tab_import:
        st.write(
            "CSV columns: `circuit_number,light_number,sequence,location` "
            "plus optional `map_number,street,side,nth,from_dir,cross_street`."
        )
        st.code(
            "circuit_number,light_number,map_number,street,side,nth,from_dir,cross_street,sequence,location\n"
            "T1S-A,1W-1N-Mason,305,1,W,1,N,Mason,1a,W side 1st N of Mason\n"
            "T1S-A,2W-1N-Mason,305,2,W,1,N,Mason,1b,W side 2nd N of Mason",
            language="csv",
        )
        up = st.file_uploader("CSV file", type=["csv"])
        if up and st.button("Import CSV"):
            text = up.getvalue().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            needed = {"circuit_number"}
            if not reader.fieldnames or not needed.issubset({f.strip() for f in reader.fieldnames}):
                st.error("CSV must include circuit_number.")
            else:
                n = 0
                skipped = []
                for row in reader:
                    cn = (row.get("circuit_number") or "").strip()
                    built = format_callout(
                        row.get("street") or "",
                        row.get("side") or "",
                        row.get("nth") or "",
                        row.get("from_dir") or "",
                        row.get("cross_street") or "",
                    )
                    ln = (row.get("light_number") or "").strip() or built
                    if not cn or not ln:
                        continue
                    seq_raw = (row.get("sequence") or "").strip()
                    err = validate_sequence_or_error(seq_raw)
                    if err:
                        skipped.append(f"{cn}/{ln}: {err}")
                        continue
                    circ = get_or_create_circuit(conn, cn)
                    seq = normalize_seq(seq_raw) or None
                    loc = (row.get("location") or "").strip() or None
                    nth_raw = (row.get("nth") or "").strip()
                    nth_val = int(nth_raw) if nth_raw.isdigit() else None
                    conn.execute(
                        """
                        INSERT INTO circuit_lights (
                            circuit_id, light_number, map_number, street, side, nth,
                            from_dir, cross_street, sequence, location_note
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(circuit_id, light_number) DO UPDATE SET
                            map_number = COALESCE(excluded.map_number, circuit_lights.map_number),
                            street = COALESCE(excluded.street, circuit_lights.street),
                            side = COALESCE(excluded.side, circuit_lights.side),
                            nth = COALESCE(excluded.nth, circuit_lights.nth),
                            from_dir = COALESCE(excluded.from_dir, circuit_lights.from_dir),
                            cross_street = COALESCE(excluded.cross_street, circuit_lights.cross_street),
                            sequence = COALESCE(excluded.sequence, circuit_lights.sequence),
                            location_note = COALESCE(excluded.location_note, circuit_lights.location_note)
                        """,
                        (
                            circ.get("id"),
                            ln,
                            (row.get("map_number") or "").strip() or None,
                            (row.get("street") or "").strip() or None,
                            (row.get("side") or "").strip() or None,
                            nth_val,
                            (row.get("from_dir") or "").strip() or None,
                            (row.get("cross_street") or "").strip() or None,
                            seq,
                            loc,
                        ),
                    )
                    n += 1
                conn.commit()
                st.success(f"Imported {n} light row(s).")
                if skipped:
                    st.warning("Skipped invalid sequences:\n" + "\n".join(skipped[:20]))
                st.rerun()

    with tab_pdf:
        cn = st.text_input("Attach PDF to circuit number")
        pdf = st.file_uploader("Circuit PDF", type=["pdf"])
        if st.button("Upload PDF") and cn.strip() and pdf:
            circ = get_or_create_circuit(conn, cn)
            safe_name = f"{circ['id']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{pdf.name}"
            dest = PDF_DIR / safe_name
            data = pdf.getvalue()
            dest.write_bytes(data)
            extracted = extract_pdf_text(data)
            conn.execute(
                """
                INSERT INTO circuit_pdfs (circuit_id, filename, filepath, extracted_text, uploaded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (circ.get("id"), pdf.name, str(dest), extracted, now_iso()),
            )
            conn.commit()
            st.success(f"Stored {pdf.name} on circuit {cn.strip()}. Extracted {len(extracted)} characters of text.")
            if extracted:
                st.text_area("Preview of extracted text", extracted[:4000], height=240)
            else:
                st.warning(
                    "No selectable text found. This is common with scanned drawings. "
                    "You can still keep the file here, but downstream detection needs a light-order list."
                )


STREET_WORDS = re.compile(
    r"\b(street|st|avenue|ave|road|rd|drive|dr|boulevard|blvd|lane|ln|way|place|pl|court|ct)\b",
    re.I,
)
AND_SPLIT = re.compile(r"\s+(?:and|&|@|at)\s+", re.I)


def parse_address_query(text: str) -> dict:
    """Pull house number, streets, side, and leftover words from a typed address."""
    raw = (text or "").strip()
    out = {
        "raw": raw,
        "house": None,
        "side": "",
        "streets": [],
        "tokens": [],
    }
    if not raw:
        return out
    cleaned = STREET_WORDS.sub(" ", raw)
    cleaned = re.sub(r"[.,#]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    out["tokens"] = [t for t in re.split(r"\s+", cleaned.lower()) if t]

    m = re.search(r"\b(\d{1,6})\b", cleaned)
    if m:
        try:
            out["house"] = int(m.group(1))
        except ValueError:
            out["house"] = None

    for tok in out["tokens"]:
        if tok.upper() in {"N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST"}:
            letter = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}.get(
                tok.upper(), tok.upper()[:1]
            )
            if letter in {"N", "S", "E", "W"}:
                out["side"] = letter
                break

    parts = AND_SPLIT.split(cleaned)
    streets = []
    for part in parts:
        p = STREET_WORDS.sub(" ", part)
        p = re.sub(r"\b\d{1,6}\b", " ", p)
        p = re.sub(r"\b[nsew]\b", " ", p, flags=re.I)
        p = re.sub(r"\s+", " ", p).strip(" -")
        if p:
            streets.append(p)
    # also keep individual leftover words longer than 1 char as street-ish
    if not streets:
        streets = [t for t in out["tokens"] if t.isalpha() and t.upper() not in {"N", "S", "E", "W"}]
    out["streets"] = streets
    return out


def _num(val) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(str(val).strip())
    except ValueError:
        m = re.search(r"\d+", str(val))
        return int(m.group()) if m else None


def address_match_score(light: dict, parsed: dict) -> tuple[int, list[str]]:
    """Higher is better. Reasons explain the hit."""
    reasons = []
    score = 0
    blob = " ".join(
        str(light.get(k) or "")
        for k in (
            "light_number",
            "map_number",
            "street",
            "side",
            "cross_street",
            "location_note",
            "circuit_number",
        )
    ).lower()

    for street in parsed.get("streets") or []:
        s = street.lower()
        if len(s) < 2:
            continue
        if s in str(light.get("street") or "").lower():
            score += 40
            reasons.append(f"street {street}")
        elif s in str(light.get("cross_street") or "").lower():
            score += 35
            reasons.append(f"cross {street}")
        elif s in blob:
            score += 20
            reasons.append(f"mentions {street}")

    if parsed.get("side"):
        side = parsed["side"]
        if str(light.get("side") or "").upper() == side:
            score += 8
            reasons.append(f"side {side}")

    house = parsed.get("house")
    map_n = _num(light.get("map_number"))
    if house is not None and map_n is not None:
        diff = abs(house - map_n)
        if diff == 0:
            score += 50
            reasons.append(f"map # matches {house}")
        elif diff <= 20:
            score += 30
            reasons.append(f"map #{map_n} near {house}")
        elif diff <= 80:
            score += 12
            reasons.append(f"map #{map_n} same block-ish as {house}")

    if house is not None and str(house) in blob and map_n != house:
        score += 6
        reasons.append(f"text has {house}")

    if not reasons:
        return 0, []
    return score, reasons


def search_lights_by_address(conn, query: str) -> list[dict]:
    parsed = parse_address_query(query)
    lights = q_all(
        conn,
        """
        SELECT cl.*, c.circuit_number, c.circuit_type
        FROM circuit_lights cl
        JOIN circuits c ON c.id = cl.circuit_id
        """,
    )
    scored = []
    for row in lights:
        score, reasons = address_match_score(row, parsed)
        if score <= 0:
            continue
        item = dict(row)
        item["score"] = score
        item["reasons"] = reasons
        scored.append(item)
    scored.sort(key=lambda r: (-r["score"], str(r.get("circuit_number") or "")))
    return scored, parsed


def _fmt_when(val) -> str:
    s = str(val or "").replace("T", " ")
    if len(s) >= 16:
        return s[:16]
    return s or "—"


def _yes(val) -> str:
    return "Yes" if val else ""


def page_light_history(conn) -> None:
    st.header("Light history")
    st.caption(
        "One row per job or update on a head. Filter first, then scan flags as Yes / blank."
    )
    c1, c2, c3, c4 = st.columns(4)
    circuit = c1.text_input("Circuit")
    callout = c2.text_input("Callout / light ID")
    mapn = c3.text_input("Map #")
    kind = c4.selectbox("Event type", ["All"] + TICKET_TYPES + ["update"])

    events = q_all(conn, "SELECT * FROM light_events ORDER BY created_at DESC")
    if not events:
        st.info("No light history yet. Log a call with condition boxes checked.")
        return

    filtered = []
    for e in events:
        if circuit and circuit.lower() not in str(e.get("circuit_number") or "").lower():
            continue
        if callout and callout.lower() not in str(e.get("light_number") or "").lower():
            continue
        if mapn and mapn.lower() not in str(e.get("map_number") or "").lower():
            continue
        if kind != "All" and str(e.get("event_type") or "") != kind:
            continue
        filtered.append(e)

    st.write(f"**{len(filtered)}** event(s)")
    if not filtered:
        st.warning("Nothing matches those filters.")
        return

    detail_rows = []
    for e in filtered:
        pole = " ".join(x for x in (e.get("pole_material"), e.get("pole_height")) if x)
        detail_rows.append(
            {
                "Date": _fmt_when(e.get("created_at")),
                "Circuit": e.get("circuit_number") or "",
                "Callout": e.get("light_number") or "",
                "Map #": e.get("map_number") or "",
                "Ticket": e.get("ticket_id") or "",
                "Type": e.get("event_type") or "",
                "Down": _yes(e.get("knockdown")),
                "Fixture bad": _yes(e.get("bad_fixture")),
                "Igniter": _yes(e.get("bad_igniter")),
                "UGT": _yes(e.get("ugt")),
                "Vandal": _yes(e.get("vandalism")),
                "Wire stolen": _yes(e.get("wire_stolen")),
                "Pole": pole,
                "Fixture": e.get("fixture_type") or "",
                "Notes": (e.get("notes") or "").replace("\n", " · ")[:160],
            }
        )
    detail = pd.DataFrame(detail_rows)

    # Roll up: latest event per light
    latest = {}
    ever = {}
    for e in reversed(filtered):
        key = (e.get("circuit_number"), e.get("light_number"))
        latest[key] = e
        bag = ever.setdefault(key, set())
        if e.get("knockdown"):
            bag.add("Knockdown")
        if e.get("bad_fixture"):
            bag.add("Bad fixture")
        if e.get("bad_igniter"):
            bag.add("Bad igniter")
        if e.get("ugt"):
            bag.add("UGT")
        if e.get("vandalism"):
            bag.add("Vandalism")
        if e.get("wire_stolen"):
            bag.add("Wire stolen")

    summary_rows = []
    for (cn, ln), e in sorted(latest.items(), key=lambda kv: kv[0]):
        pole = " ".join(x for x in (e.get("pole_material"), e.get("pole_height")) if x)
        summary_rows.append(
            {
                "Circuit": cn or "",
                "Callout": ln or "",
                "Map #": e.get("map_number") or "",
                "Last date": _fmt_when(e.get("created_at")),
                "Last type": e.get("event_type") or "",
                "All flags on file": ", ".join(sorted(ever.get((cn, ln), []))) or "—",
                "Pole": pole or "—",
                "Fixture": e.get("fixture_type") or "—",
                "Events": sum(
                    1
                    for x in filtered
                    if x.get("circuit_number") == cn and x.get("light_number") == ln
                ),
            }
        )
    summary = pd.DataFrame(summary_rows)

    tab_sum, tab_log = st.tabs(["By light", "Full log"])

    col_cfg_sum = {
        "Circuit": st.column_config.TextColumn(width="small"),
        "Callout": st.column_config.TextColumn(width="medium"),
        "Map #": st.column_config.TextColumn(width="small"),
        "Last date": st.column_config.TextColumn(width="small"),
        "Last type": st.column_config.TextColumn(width="small"),
        "All flags on file": st.column_config.TextColumn(width="medium"),
        "Pole": st.column_config.TextColumn(width="small"),
        "Fixture": st.column_config.TextColumn(width="small"),
        "Events": st.column_config.NumberColumn(width="small"),
    }
    col_cfg_log = {
        "Date": st.column_config.TextColumn(width="small"),
        "Circuit": st.column_config.TextColumn(width="small"),
        "Callout": st.column_config.TextColumn(width="medium"),
        "Map #": st.column_config.TextColumn(width="small"),
        "Ticket": st.column_config.TextColumn(width="small"),
        "Type": st.column_config.TextColumn(width="small"),
        "Down": st.column_config.TextColumn(width="small"),
        "Fixture bad": st.column_config.TextColumn(width="small"),
        "Igniter": st.column_config.TextColumn(width="small"),
        "UGT": st.column_config.TextColumn(width="small"),
        "Vandal": st.column_config.TextColumn(width="small"),
        "Wire stolen": st.column_config.TextColumn(width="small"),
        "Pole": st.column_config.TextColumn(width="small"),
        "Fixture": st.column_config.TextColumn(width="small"),
        "Notes": st.column_config.TextColumn(width="large"),
    }

    with tab_sum:
        st.dataframe(
            summary,
            hide_index=True,
            use_container_width=True,
            column_config=col_cfg_sum,
        )
    with tab_log:
        st.dataframe(
            detail,
            hide_index=True,
            use_container_width=True,
            column_config=col_cfg_log,
        )

    st.download_button(
        "Download full log CSV",
        detail.to_csv(index=False).encode("utf-8"),
        "light_history.csv",
        "text/csv",
    )

    if circuit.strip() and callout.strip():
        st.subheader(f"Current file — {circuit.strip()}  ·  {callout.strip()}")
        cur = q_one(
            conn,
            """
            SELECT cl.*
            FROM circuit_lights cl
            JOIN circuits c ON c.id = cl.circuit_id
            WHERE c.circuit_number = ? AND cl.light_number = ?
            """,
            (circuit.strip(), callout.strip()),
        )
        if cur:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Map #", cur.get("map_number") or "—")
            m2.metric("Pole", f"{cur.get('pole_material') or '—'} {cur.get('pole_height') or ''}".strip())
            m3.metric("Fixture", cur.get("fixture_type") or "—")
            m4.metric("Sequence", cur.get("sequence") or "—")
            if cur.get("location_note"):
                st.caption(cur.get("location_note"))
        else:
            st.caption("No Circuits & maps record for that callout yet.")


def page_address_search(conn) -> None:
    st.header("Address search")
    st.caption(
        "Type a house address or intersection. The app looks up **stored lights** "
        "(street, cross street, map #, callout, notes) and lists likely **circuits**. "
        "This is a best-guess from your light list — not GPS distance."
    )
    q = st.text_input(
        "Physical address or intersection",
        placeholder="312 W Mason St    or    1st and Mason",
    )
    if not q.strip():
        n = q_one(conn, "SELECT COUNT(*) AS n FROM circuit_lights")
        st.info(
            f"{(n or {}).get('n', 0)} light(s) on file. "
            "Add lights under Circuits & maps (street + map # + callout) so this search has something to match."
        )
        return

    hits, parsed = search_lights_by_address(conn, q)
    bits = []
    if parsed.get("house") is not None:
        bits.append(f"house **{parsed['house']}**")
    if parsed.get("side"):
        bits.append(f"side **{parsed['side']}**")
    if parsed.get("streets"):
        bits.append("streets **" + ", ".join(parsed["streets"]) + "**")
    st.caption("Parsed: " + (", ".join(bits) if bits else parsed["raw"]))

    if not hits:
        st.warning("No stored lights matched. Add the nearby heads on Circuits & maps, then search again.")
        return

    st.write(f"**{len(hits)}** matching light(s)")

    # Circuit summary first
    by_c: dict[str, int] = {}
    for h in hits:
        cn = h.get("circuit_number") or "?"
        by_c[cn] = by_c.get(cn, 0) + 1
    top = sorted(by_c.items(), key=lambda kv: -kv[1])
    st.subheader("Likely circuit(s)")
    st.dataframe(
        pd.DataFrame(
            [{"Circuit": c, "Matching lights": n} for c, n in top]
        ),
        hide_index=True,
        use_container_width=True,
    )

    rows = []
    for h in hits[:80]:
        rows.append(
            {
                "Score": h["score"],
                "Circuit": h.get("circuit_number"),
                "Callout": h.get("light_number"),
                "Map #": h.get("map_number") or "",
                "Spoken": spoken_callout(
                    h.get("street") or "",
                    h.get("side") or "",
                    h.get("nth"),
                    h.get("from_dir") or "",
                    h.get("cross_street") or "",
                ),
                "Seq": h.get("sequence") or "",
                "Branch": branch_label(h.get("sequence")),
                "Why": "; ".join(h["reasons"]),
                "Note": h.get("location_note") or "",
            }
        )
    st.subheader("Nearest stored lights (by map # and street text)")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def page_reports(conn: sqlite3.Connection) -> None:
    st.header("Reports")
    df = tickets_df(conn)
    if df.empty:
        st.info("No data yet.")
        return

    active = df[df["status"] == "active"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Active calls", len(active))
    c2.metric("All-time tickets", len(df))
    c3.metric("Flagged (all time)", int(df["is_flagged"].sum()))
    c4.metric("Circuits used", df["circuit_number"].nunique())

    st.subheader("Active by type")
    if not active.empty:
        st.bar_chart(active["ticket_type"].value_counts())
    else:
        st.caption("Nothing active.")

    st.subheader("Tickets by circuit (all time)")
    by_c = df.groupby("circuit_number").size().sort_values(ascending=False).head(25)
    st.bar_chart(by_c)

    st.subheader("Repeat circuits (history of outages)")
    repeats = (
        df.groupby(["circuit_number", "light_number"])
        .size()
        .reset_index(name="times")
        .sort_values("times", ascending=False)
    )
    repeats = repeats[repeats["times"] > 1]
    if repeats.empty:
        st.caption("No repeated circuit + light combinations yet.")
    else:
        st.dataframe(repeats.rename(columns={"circuit_number": "Circuit", "light_number": "Light"}), hide_index=True, use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="Street Light Tracker", page_icon="💡", layout="wide")

    if not require_login():
        return

    init_db()
    conn = get_conn()

    st.title("Street Light Tracker")
    st.caption("Outages · damage · cable theft — LUB / FUD, active log, history")

    page = st.sidebar.radio(
        "Go to",
        [
            "Active calls",
            "New call",
            "Address search",
            "Light history",
            "History",
            "Circuits & maps",
            "Reports",
        ],
    )

    backend = "Turso (cloud)" if using_turso() else "Local SQLite"
    st.sidebar.caption(f"Database: **{backend}**")

    row = q_one(conn, "SELECT COUNT(*) AS n FROM tickets WHERE status = 'active'")
    active_n = (row or {}).get("n", 0)
    st.sidebar.metric("Active now", active_n)

    if page == "Active calls":
        page_active(conn)
    elif page == "New call":
        page_new_call(conn)
    elif page == "Address search":
        page_address_search(conn)
    elif page == "Light history":
        page_light_history(conn)
    elif page == "History":
        page_history(conn)
    elif page == "Circuits & maps":
        page_circuits(conn)
    else:
        page_reports(conn)

    conn.close()


if __name__ == "__main__":
    main()
