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
    "Trouble",
    "UGT",
    "Damage",
    "Knockdown",
    "Tag Out",
    "Bad Ballast",
    "Bad Ignitor",
    "Bad Fixture",
    "Deteriorated Pole",
    "Damaged Pedestal",
    "Cable Theft",
    "Wires Cut in pedestal",
    "Vandalism",
    "Other",
]
TICKET_TYPE_ALIASES = {
    "Outage": "Trouble",
    "Wire stolen": "Cable Theft",
    "Pedestal cut": "Wires Cut in pedestal",
    "Bad igniter": "Bad Ignitor",
    "Bad fixture": "Bad Fixture",
    "Tag out": "Tag Out",
}
TAG_OUT_TYPES = {"Tag Out", "Tag out"}
PEDESTAL_CUT_TYPES = {"Wires Cut in pedestal", "Pedestal cut"}
COMPONENT_TYPES = {"Bad Fixture", "Bad fixture", "Bad Ignitor", "Bad igniter", "Bad Ballast"}
OUTAGE_CAUSES = [
    "",
    "Natural / component (lamp, photocell, igniter, driver)",
    "Damage (vehicle, storm, knockdown)",
    "Theft / vandalism / stolen wire",
    "Pedestal or cable cut",
    "UGT (underground to this light only)",
    "Unknown",
]
BOARD_FILTERS = [
    "All",
    "Circuit troubles (LUB/FUD)",
    "Damages",
    "Theft / vandalism",
    "Tag outs",
    "Knockdowns",
    "Fixture / component",
    "UGT",
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
PHOTO_DIR = DATA_DIR / "photos"
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
        work_order TEXT,
        created_by TEXT,
        completed_by TEXT,
        outage_cause TEXT,
        findings TEXT,
        photo_name TEXT,
        photo_data TEXT,
        is_tag_out INTEGER NOT NULL DEFAULT 0,
        tag_reason TEXT,
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


def display_ticket_type(val) -> str:
    s = str(val or "")
    return TICKET_TYPE_ALIASES.get(s, s)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)


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
    Secrets:
      APP_PASSWORD / SUPERVISOR_PASSWORD  (shared role codes)
      [passwords]
      mike = "his-code"
      liz = "her-code"
      crew = "shared-truck"
      supervisor = "shared-office"
    """
    out: dict[str, str] = {}
    crew = _secret("APP_PASSWORD")
    if crew:
        out["crew"] = crew
    sup = _secret("SUPERVISOR_PASSWORD")
    if sup:
        out["supervisor"] = sup
    try:
        block = st.secrets.get("passwords")
        if block:
            for k, v in dict(block).items():
                if v:
                    out[str(k).lower().strip()] = str(v)
    except Exception:
        pass
    return out


def _auth_roles() -> dict[str, str]:
    """
    [roles]
    mike = "crew"
    liz = "supervisor"
    Names not listed default to crew (except the name 'supervisor').
    """
    out: dict[str, str] = {}
    try:
        block = st.secrets.get("roles")
        if block:
            for k, v in dict(block).items():
                role = str(v).lower().strip()
                if role in ("crew", "supervisor"):
                    out[str(k).lower().strip()] = role
    except Exception:
        pass
    return out


def current_role() -> str:
    return st.session_state.get("auth_role") or "crew"


def current_user_name() -> str:
    return st.session_state.get("auth_name") or st.session_state.get("auth_user") or "crew"


def is_supervisor() -> bool:
    return current_role() == "supervisor"


def require_login() -> bool:
    """Gate the app behind crew or supervisor password."""
    passwords = _auth_passwords()
    if not passwords:
        st.session_state.setdefault("authenticated", True)
        st.session_state.setdefault("auth_role", "supervisor")
        st.session_state.setdefault("auth_name", "local")
        return True

    if st.session_state.get("authenticated"):
        with st.sidebar:
            who = current_user_name()
            role = current_role()
            st.caption(f"**{who}** · {role}")
            if st.button("Log out"):
                for k in ("authenticated", "auth_user", "auth_name", "auth_role"):
                    st.session_state[k] = None if k != "authenticated" else False
                st.rerun()
        return True

    roles_map = _auth_roles()
    named = sorted(k for k in passwords if k not in ("crew", "supervisor"))
    st.title("Street Light Tracker")
    st.caption("Sign in with your name and password")
    with st.form("login"):
        name = st.text_input("Name", placeholder="mike")
        role_pick = st.selectbox(
            "Role (only used with the shared crew/supervisor password)",
            ["crew", "supervisor"],
        )
        pwd = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in", type="primary")
    if ok:
        key = (name or "").strip().lower()
        ok_login = False
        role = "crew"
        display = (name or "").strip() or "crew"
        # 1) Named account: passwords.mike = "..."
        if key and key in passwords and pwd == passwords[key]:
            ok_login = True
            role = roles_map.get(key) or ("supervisor" if key == "supervisor" else "crew")
            display = name.strip()
        # 2) Shared role password
        elif role_pick in passwords and pwd == passwords[role_pick]:
            ok_login = True
            role = role_pick
            display = (name or role_pick).strip() or role_pick
        if ok_login:
            st.session_state.authenticated = True
            st.session_state.auth_role = role
            st.session_state.auth_name = display
            st.session_state.auth_user = display
            st.rerun()
        st.error("Wrong name or password.")
    if named:
        st.caption("Named logins: " + ", ".join(named))
    st.info(
        "Named accounts go in Streamlit **Secrets** under `[passwords]` and `[roles]`. "
        "Shared codes still work: `APP_PASSWORD` / `SUPERVISOR_PASSWORD`."
    )
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
            "work_order": "ALTER TABLE tickets ADD COLUMN work_order TEXT",
            "created_by": "ALTER TABLE tickets ADD COLUMN created_by TEXT",
            "completed_by": "ALTER TABLE tickets ADD COLUMN completed_by TEXT",
            "outage_cause": "ALTER TABLE tickets ADD COLUMN outage_cause TEXT",
            "findings": "ALTER TABLE tickets ADD COLUMN findings TEXT",
            "photo_name": "ALTER TABLE tickets ADD COLUMN photo_name TEXT",
            "photo_data": "ALTER TABLE tickets ADD COLUMN photo_data TEXT",
            "is_tag_out": "ALTER TABLE tickets ADD COLUMN is_tag_out INTEGER NOT NULL DEFAULT 0",
            "tag_reason": "ALTER TABLE tickets ADD COLUMN tag_reason TEXT",
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


SIDES = ["", "N", "S", "E", "W", "C"]
DIRS = ["", "N", "S", "E", "W", "@"]


def format_callout(street: str, side: str, nth, from_dir: str, cross: str) -> str:
    """1 W 1 N Mason -> 1W-1N-Mason"""
    street = (street or "").strip()
    side = (side or "").strip().upper()
    from_dir = (from_dir or "").strip()
    if from_dir != "@":
        from_dir = from_dir.upper()
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
        d = str(from_dir).strip()
        bits.append("at" if d == "@" else d.upper())
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
    work_order: str = "",
    created_by: str = "",
    outage_cause: str = "",
    photo_name: str = "",
    photo_data: str = "",
    is_tag_out: bool = False,
    tag_reason: str = "",
) -> tuple[int, bool, str]:
    skip_dup = is_tag_out or ticket_type in TAG_OUT_TYPES
    if skip_dup:
        flagged, reason, parent = False, "", None
    else:
        flagged, reason, parent = check_duplicate(conn, circuit_number, light_number, lub, fud)
    cur = conn.execute(
        """
        INSERT INTO tickets (
            ticket_type, circuit_number, light_number, map_number, lub, fud, pedestal_cut,
            location, description,
            status, is_flagged, flag_reason, parent_ticket_id, created_at,
            work_order, created_by, outage_cause, photo_name, photo_data,
            is_tag_out, tag_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            _norm(work_order) or None,
            _norm(created_by) or current_user_name(),
            _norm(outage_cause) or None,
            _norm(photo_name) or None,
            photo_data or None,
            1 if (is_tag_out or ticket_type in TAG_OUT_TYPES) else 0,
            _norm(tag_reason) or None,
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


UNDO_SECONDS = 45

CONFIRM_STYLE = """
<style>
div[data-testid="stDialog"] div[role="dialog"],
div[data-testid="stExpander"] {
  border: 2px solid #b42318 !important;
}
.slt-confirm {
  background: #fff4f2;
  border: 2px solid #b42318;
  border-radius: 10px;
  padding: 0.85rem 1rem 1rem;
  margin-bottom: 0.5rem;
}
.slt-confirm h3 { color: #b42318; margin: 0 0 0.4rem 0; font-size: 1.15rem; }
.slt-confirm p { margin: 0.25rem 0; }
.slt-confirm .target { font-size: 1.05rem; font-weight: 700; }
</style>
"""


def _insert_row(conn, table: str, row: dict) -> None:
    if not row:
        return
    cols = [k for k in row.keys() if not str(k).startswith("_")]
    placeholders = ", ".join(["?"] * len(cols))
    colsql = ", ".join(cols)
    vals = [row.get(c) for c in cols]
    conn.execute(f"INSERT INTO {table} ({colsql}) VALUES ({placeholders})", vals)


def snapshot_light(conn, light_id) -> dict | None:
    row = q_one(conn, "SELECT * FROM circuit_lights WHERE id = ?", (light_id,))
    return dict(row) if row else None


def snapshot_circuit(conn, circuit_id) -> dict | None:
    circ = q_one(conn, "SELECT * FROM circuits WHERE id = ?", (circuit_id,))
    if not circ:
        return None
    lights = q_all(conn, "SELECT * FROM circuit_lights WHERE circuit_id = ?", (circuit_id,))
    pdfs = q_all(conn, "SELECT * FROM circuit_pdfs WHERE circuit_id = ?", (circuit_id,))
    return {"circuit": dict(circ), "lights": [dict(r) for r in lights], "pdfs": [dict(r) for r in pdfs]}


def restore_undo(conn, payload: dict) -> str:
    kind = payload.get("kind")
    if kind == "light":
        _insert_row(conn, "circuit_lights", payload.get("row") or {})
        conn.commit()
        name = (payload.get("row") or {}).get("light_number") or "light"
        return f"Restored light {name}."
    if kind == "circuit":
        snap = payload.get("snap") or {}
        _insert_row(conn, "circuits", snap.get("circuit") or {})
        for r in snap.get("lights") or []:
            _insert_row(conn, "circuit_lights", r)
        for r in snap.get("pdfs") or []:
            _insert_row(conn, "circuit_pdfs", r)
        conn.commit()
        num = (snap.get("circuit") or {}).get("circuit_number") or "circuit"
        return f"Restored circuit {num}."
    return "Nothing to restore."


def do_delete_light(conn, light_id) -> dict | None:
    snap = snapshot_light(conn, light_id)
    conn.execute("DELETE FROM circuit_lights WHERE id = ?", (light_id,))
    conn.commit()
    return snap


def do_delete_circuit(conn, circuit_id) -> dict | None:
    snap = snapshot_circuit(conn, circuit_id)
    conn.execute("DELETE FROM circuit_pdfs WHERE circuit_id = ?", (circuit_id,))
    conn.execute("DELETE FROM circuit_lights WHERE circuit_id = ?", (circuit_id,))
    conn.execute("DELETE FROM circuits WHERE id = ?", (circuit_id,))
    conn.commit()
    return snap


def set_undo(kind: str, label: str, extra: dict) -> None:
    st.session_state["undo"] = {
        "kind": kind,
        "label": label,
        "expires": datetime.now().timestamp() + UNDO_SECONDS,
        **extra,
    }


def clear_delete_flags() -> None:
    for k in (
        "delete_light_id",
        "delete_light_name",
        "delete_light_circuit",
        "delete_circuit",
        "delete_circuit_id",
    ):
        st.session_state.pop(k, None)


def render_undo_banner() -> None:
    undo = st.session_state.get("undo")
    if not undo:
        return
    if datetime.now().timestamp() > float(undo.get("expires") or 0):
        st.session_state.pop("undo", None)
        return
    left = max(0, int(float(undo["expires"]) - datetime.now().timestamp()))
    c1, c2 = st.columns([4, 1])
    c1.warning(f"Deleted **{undo.get('label')}**. Undo available for {left}s.")
    if c2.button("Undo", type="primary", key="undo_delete_btn"):
        conn = get_conn()
        try:
            msg = restore_undo(conn, undo)
        except Exception as exc:
            conn.close()
            st.session_state.pop("undo", None)
            st.session_state["flash_err"] = f"Could not undo: {exc}"
            st.rerun()
            return
        conn.close()
        st.session_state.pop("undo", None)
        st.session_state["flash_ok"] = msg
        st.rerun()


def confirm_delete(
    title: str,
    headline: str,
    detail: str,
    confirm_key: str,
    on_confirm,
    require_typed: str | None = None,
) -> None:
    """Shared delete confirmation (styled modal when Streamlit dialog exists)."""
    st.markdown(CONFIRM_STYLE, unsafe_allow_html=True)

    def _body():
        st.markdown(
            f'<div class="slt-confirm"><h3>{title}</h3>'
            f'<p class="target">{headline}</p>'
            f"<p>{detail}</p></div>",
            unsafe_allow_html=True,
        )
        typed = ""
        if require_typed:
            typed = st.text_input(
                f"Type {require_typed} to confirm",
                key=f"{confirm_key}_typed",
            )
        c1, c2 = st.columns(2)
        if c1.button("Yes, delete", type="primary", key=f"{confirm_key}_yes"):
            if require_typed and typed.strip() != str(require_typed):
                st.error("Confirmation text did not match.")
            else:
                on_confirm()
        if c2.button("Cancel", key=f"{confirm_key}_no"):
            clear_delete_flags()
            st.rerun()

    if hasattr(st, "dialog"):
        @st.dialog(title)
        def _dlg():
            _body()

        _dlg()
    else:
        _body()


def _open_delete_light_dialog(light_id, name: str, circuit: str) -> None:
    if not is_supervisor():
        st.error("Only a **supervisor** can delete lights from the circuit map.")
        clear_delete_flags()
        return

    def _go():
        conn = get_conn()
        try:
            snap = do_delete_light(conn, light_id)
        finally:
            conn.close()
        clear_delete_flags()
        set_undo("light", f"light {name} on {circuit}", {"row": snap})
        st.session_state["flash_ok"] = f"Deleted light {name}."
        st.rerun()

    confirm_delete(
        title="Delete this light?",
        headline=f"{name}  ·  circuit {circuit}",
        detail="Removes the head from the circuit map. Tickets stay. You can Undo for a short time.",
        confirm_key=f"light_{light_id}",
        on_confirm=_go,
    )


def _open_delete_circuit_dialog(circuit_id, circuit_number: str) -> None:
    if not is_supervisor():
        st.error("Only a **supervisor** can delete circuits.")
        clear_delete_flags()
        return

    def _go():
        conn = get_conn()
        try:
            snap = do_delete_circuit(conn, circuit_id)
        finally:
            conn.close()
        clear_delete_flags()
        set_undo("circuit", f"circuit {circuit_number}", {"snap": snap})
        st.session_state["flash_ok"] = f"Deleted circuit {circuit_number}."
        st.rerun()

    confirm_delete(
        title="Delete this circuit?",
        headline=str(circuit_number),
        detail="Deletes the circuit, its lights, and PDF links. Tickets and light history stay. Undo is available briefly.",
        confirm_key=f"circ_{circuit_id}",
        on_confirm=_go,
        require_typed=str(circuit_number),
    )


def complete_ticket(
    conn: sqlite3.Connection,
    ticket_id: int,
    notes: str,
    findings: str = "",
    completed_by: str = "",
) -> None:
    conn.execute(
        """
        UPDATE tickets
        SET status = 'completed', completed_at = ?, completion_notes = ?,
            findings = COALESCE(?, findings),
            completed_by = ?
        WHERE id = ?
        """,
        (
            now_iso(),
            notes.strip() or None,
            findings.strip() or None,
            (completed_by or current_user_name()).strip() or None,
            ticket_id,
        ),
    )
    conn.commit()


def board_category(row) -> str:
    """Classify a ticket for active-board filters."""
    t = str(row.get("ticket_type") or "")
    if int(row.get("is_tag_out") or 0) == 1 or t in TAG_OUT_TYPES:
        return "Tag outs"
    if t in ("Cable Theft", "Wire stolen", "Vandalism"):
        return "Theft / vandalism"
    if t == "Knockdown":
        return "Knockdowns"
    if t in COMPONENT_TYPES:
        return "Fixture / component"
    if t == "UGT":
        return "UGT"
    if t in ("Damage", "Deteriorated Pole", "Damaged Pedestal"):
        return "Damages"
    if t in ("Trouble", "Outage") or t in PEDESTAL_CUT_TYPES or row.get("lub") or row.get("fud"):
        return "Circuit troubles (LUB/FUD)"
    return "All"


def filter_board(df: pd.DataFrame, board_filter: str) -> pd.DataFrame:
    if df.empty or board_filter == "All":
        return df
    mask = df.apply(lambda r: board_category(r) == board_filter, axis=1)
    return df[mask]


def build_print_board_html(df: pd.DataFrame, title: str = "Active board") -> str:
    rows = []
    for _, r in df.iterrows():
        tag = "TAG OUT" if int(r.get("is_tag_out") or 0) else display_ticket_type(r.get("ticket_type"))
        cause = r.get("outage_cause") or ""
        rows.append(
            f"<tr>"
            f"<td>{r.get('id')}</td>"
            f"<td>{tag}</td>"
            f"<td>{r.get('circuit_number') or ''}</td>"
            f"<td>{r.get('light_number') or ''}</td>"
            f"<td>{r.get('lub') or ''}</td>"
            f"<td>{r.get('fud') or ''}</td>"
            f"<td>{cause}</td>"
            f"<td>{r.get('work_order') or ''}</td>"
            f"<td>{r.get('created_by') or ''}</td>"
            f"<td>{r.get('location') or ''}</td>"
            f"<td>{(r.get('tag_reason') or r.get('description') or '')[:80]}</td>"
            f"</tr>"
        )
    body = "\n".join(rows) or "<tr><td colspan='11'>No calls</td></tr>"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: Arial, sans-serif; font-size: 12px; }}
h1 {{ font-size: 18px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #333; padding: 4px 6px; text-align: left; }}
th {{ background: #eee; }}
@media print {{ button {{ display: none; }} }}
</style></head><body>
<h1>{title}</h1>
<p>Printed {datetime.now().strftime("%Y-%m-%d %H:%M")} · {len(df)} call(s)</p>
<table>
<thead><tr>
<th>#</th><th>Type</th><th>Circuit</th><th>Location</th><th>LUB</th><th>FUD</th>
<th>Cause</th><th>Record #</th><th>Crew</th><th>Location</th><th>Notes / tag</th>
</tr></thead>
<tbody>{body}</tbody>
</table>
<script>window.onload = function() {{ /* optional auto print */ }}</script>
</body></html>"""


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


MAX_PHOTO_BYTES = 400_000  # ~400 KB before encode; keeps Turso rows small


def encode_photo_limited(uploaded) -> tuple[str, str, str | None]:
    """Return (name, base64_or_empty, error). Resizes/skips if too large."""
    import base64

    if uploaded is None:
        return "", "", None
    raw = uploaded.getvalue()
    name = uploaded.name or "photo.jpg"
    if len(raw) <= MAX_PHOTO_BYTES:
        return name, base64.b64encode(raw).decode("ascii"), None
    # Try to shrink with Pillow if available; else refuse
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        # progressive shrink
        for max_side in (1280, 960, 720, 540):
            w, h = img.size
            scale = min(1.0, max_side / max(w, h))
            if scale < 1.0:
                img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75, optimize=True)
            data = buf.getvalue()
            if len(data) <= MAX_PHOTO_BYTES:
                return name.rsplit(".", 1)[0] + ".jpg", base64.b64encode(data).decode("ascii"), None
        return "", "", "Photo still too large after resize. Use a smaller picture."
    except Exception:
        return (
            "",
            "",
            f"Photo is {len(raw) // 1024} KB (limit ~{MAX_PHOTO_BYTES // 1024} KB). Use a smaller photo.",
        )


def active_tagouts_for_circuit(conn, circuit_number: str) -> list[dict]:
    if not (circuit_number or "").strip():
        return []
    return q_all(
        conn,
        """
        SELECT id, tag_reason, created_by, created_at, work_order, description
        FROM tickets
        WHERE status = 'active'
          AND circuit_number = ?
          AND (is_tag_out = 1 OR ticket_type IN ('Tag out', 'Tag Out'))
        ORDER BY created_at DESC
        """,
        (circuit_number.strip(),),
    )


def release_tag_out(conn, ticket_id: int, notes: str = "", by: str = "") -> None:
    complete_ticket(
        conn,
        ticket_id,
        notes or "Tag released",
        findings=notes or "Tag released — circuit cleared for service",
        completed_by=by or current_user_name(),
    )


def show_ticket_photo(row) -> None:
    import base64

    data = row.get("photo_data") if isinstance(row, dict) else None
    if data is None and hasattr(row, "get"):
        data = row.get("photo_data")
    if not data:
        return
    try:
        raw = base64.b64decode(data)
        st.image(raw, caption=(row.get("photo_name") if hasattr(row, "get") else None) or "Photo", width=360)
    except Exception:
        st.caption("Photo could not be displayed.")


def build_shift_sheet_html(conn) -> str:
    """Opens still active from before today + tickets completed today."""
    today = date.today().isoformat()
    opens = q_all(
        conn,
        """
        SELECT * FROM tickets
        WHERE status = 'active' AND date(created_at) < date(?)
        ORDER BY circuit_number, created_at
        """,
        (today,),
    )
    closes = q_all(
        conn,
        """
        SELECT * FROM tickets
        WHERE status = 'completed' AND date(completed_at) = date(?)
        ORDER BY completed_at
        """,
        (today,),
    )
    # Also include today's new actives
    today_open = q_all(
        conn,
        """
        SELECT * FROM tickets
        WHERE status = 'active' AND date(created_at) = date(?)
        ORDER BY created_at
        """,
        (today,),
    )

    def rows_html(rows, kind: str) -> str:
        if not rows:
            return f"<p><em>None ({kind})</em></p>"
        parts = [
            "<table><thead><tr>"
            "<th>#</th><th>Type</th><th>Circuit</th><th>Location</th><th>LUB</th><th>FUD</th>"
            "<th>Cause</th><th>Record #</th><th>Crew</th><th>When</th><th>Notes</th>"
            "</tr></thead><tbody>"
        ]
        for r in rows:
            when = r.get("completed_at") or r.get("created_at") or ""
            parts.append(
                f"<tr><td>{r.get('id')}</td><td>{r.get('ticket_type') or ''}</td>"
                f"<td>{r.get('circuit_number') or ''}</td>"
                f"<td>{r.get('light_number') or ''}</td>"
                f"<td>{r.get('lub') or ''}</td><td>{r.get('fud') or ''}</td>"
                f"<td>{r.get('outage_cause') or ''}</td>"
                f"<td>{r.get('work_order') or ''}</td>"
                f"<td>{r.get('completed_by') or r.get('created_by') or ''}</td>"
                f"<td>{str(when)[:16]}</td>"
                f"<td>{(r.get('findings') or r.get('tag_reason') or r.get('description') or '')[:60]}</td>"
                f"</tr>"
            )
        parts.append("</tbody></table>")
        return "\n".join(parts)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Shift sheet {today}</title>
<style>
body {{ font-family: Arial, sans-serif; font-size: 12px; }}
h1,h2 {{ margin-bottom: 0.3rem; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem; }}
th, td {{ border: 1px solid #333; padding: 3px 5px; text-align: left; }}
th {{ background: #eee; }}
</style></head><body>
<h1>Shift sheet — {today}</h1>
<p>Generated {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
<h2>Still open from before today ({len(opens)})</h2>
{rows_html(opens, "carry-over")}
<h2>Opened today still active ({len(today_open)})</h2>
{rows_html(today_open, "today open")}
<h2>Closed today ({len(closes)})</h2>
{rows_html(closes, "closed")}
</body></html>"""


def filter_tickets(
    df: pd.DataFrame,
    q: str,
    circuit: str,
    light: str,
    ttype: str,
    start: date | None,
    end: date | None,
    work_order: str = "",
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
    if work_order and "work_order" in out.columns:
        out = out[out["work_order"].astype(str).str.contains(work_order, case=False, na=False)]
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
            + out.get("work_order", pd.Series("", index=out.index)).fillna("").astype(str)
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
        "**LUB** = last unit burning. **FUD** = first unit dark. "
        "Pick an **outage cause** so a natural burnout is not mixed with damage or theft."
    )

    circuits = [
        r["circuit_number"]
        for r in q_all(conn, "SELECT circuit_number FROM circuits ORDER BY circuit_number")
    ]

    photo = st.file_uploader("Photo (optional, max ~400 KB after compress)", type=["jpg", "jpeg", "png", "webp"])
    photo_name, photo_data, photo_err = encode_photo_limited(photo)
    if photo_err:
        st.error(photo_err)

    with st.form("new_call", clear_on_submit=True):
        r1, r2, r3 = st.columns(3)
        ticket_type = r1.selectbox("Type", TICKET_TYPES)
        work_order = r2.text_input("Record #", placeholder="city record number")
        crew_name = r3.text_input("Crew name", value=current_user_name())
        c1, c2, c3 = st.columns(3)
        circuit_number = c1.text_input("Circuit number *", placeholder="e.g. T1S-A")
        map_number = c2.text_input("Light #", placeholder="305")
        outage_cause = c3.selectbox("Trouble / LUB-FUD cause", OUTAGE_CAUSES)
        force_on_tag = st.checkbox(
            "I know this circuit is tagged — log anyway",
            value=False,
            help="Required if there is an active tag out on this circuit (unless this ticket is the tag out).",
        )
        a1, a2, a3, a4, a5 = st.columns(5)
        street = a1.text_input("Street", placeholder="1 or 1st")
        side = a2.selectbox("Side of street", SIDES)
        nth = a3.number_input("Nth light from cross", min_value=0, step=1, value=0)
        from_dir = a4.selectbox("Direction from cross", DIRS)
        cross = a5.text_input("Cross street", placeholder="Mason")
        l1, l2, l3 = st.columns(3)
        lub = l1.text_input("LUB — last unit burning", placeholder="last light still on")
        fud = l2.text_input("FUD — first unit dark", placeholder="first light that's dark")
        pedestal_cut = l3.checkbox("Wires cut in pedestal")
        location = st.text_input(
            "Location",
            placeholder="1st & Mason — or leave blank and use the boxes above",
        )
        st.markdown("**Tag out (circuit or area locked out)**")
        is_tag = st.checkbox("This is a tag out")
        tag_reason = st.text_input(
            "Tag-out reason",
            placeholder="Damage / other dept digging near circuit / feeder de-energized",
        )
        st.markdown("**Condition of this light**")
        f1, f2, f3 = st.columns(3)
        knockdown = f1.checkbox("Knocked down")
        bad_fixture = f2.checkbox("Bad fixture — replace")
        bad_igniter = f3.checkbox("Bad igniter")
        f4, f5, f6 = st.columns(3)
        ugt = f4.checkbox("UGT (this light only)")
        vandalism = f5.checkbox("Vandalized")
        wire_stolen = False
        p1, p2, p3 = st.columns(3)
        pole_material = p1.selectbox("Pole type", POLE_MATERIALS)
        pole_height = p2.text_input("Pole height", placeholder="e.g. 30 ft")
        fixture_type = p3.text_input("Fixture type", placeholder="e.g. LED cobra / acorn")
        description = st.text_area("Notes", placeholder="Details for the next shift")
        submitted = st.form_submit_button("Log call", type="primary")

    if submitted:
        if not circuit_number.strip():
            st.error("Circuit number is required.")
            return
        if photo_err:
            st.error(photo_err)
            return
        spoken = spoken_callout(street, side, nth if nth else "", from_dir, cross)
        loc = location.strip() or spoken
        light_id = loc or (map_number or "").strip()
        if not light_id and not is_tag:
            st.error("Enter Location, or Street + side + cross, or Light #.")
            return
        ttype = "Tag Out" if is_tag else ticket_type
        if is_tag and not light_id:
            light_id = "TAGOUT"
        existing_tags = active_tagouts_for_circuit(conn, circuit_number)
        if existing_tags and not is_tag and not force_on_tag:
            msgs = "; ".join(
                f"#{t.get('id')} {t.get('tag_reason') or t.get('description') or 'tagged'}"
                for t in existing_tags[:3]
            )
            st.error(
                f"Circuit **{circuit_number.strip()}** is under active tag out ({msgs}). "
                "Check **I know this circuit is tagged — log anyway** to force, or release the tag first."
            )
            return
        get_or_create_circuit(conn, circuit_number)
        flags = {
            "knockdown": knockdown or ttype == "Knockdown",
            "bad_fixture": bad_fixture or ttype in ("Bad Fixture", "Bad fixture"),
            "bad_igniter": bad_igniter or ttype in ("Bad Ignitor", "Bad igniter"),
            "ugt": ugt or ttype == "UGT",
            "vandalism": vandalism or ttype == "Vandalism",
            "wire_stolen": wire_stolen or ttype == "Wire stolen",
        }
        extra = []
        for key, label in CONDITION_FLAGS:
            if flags.get(key):
                extra.append(label)
        if outage_cause:
            extra.append(f"cause: {outage_cause}")
        if pole_material:
            extra.append(f"pole {pole_material} {pole_height}".strip())
        if fixture_type:
            extra.append(f"fixture {fixture_type}")
        desc = description
        if extra:
            desc = ((description or "").strip() + "\n" + "; ".join(extra)).strip()
        tid, flagged, reason = insert_ticket(
            conn,
            ttype,
            circuit_number,
            light_id,
            loc,
            desc,
            lub=lub,
            fud=fud,
            pedestal_cut=pedestal_cut or ttype in PEDESTAL_CUT_TYPES,
            map_number=map_number,
            work_order=work_order,
            created_by=crew_name,
            outage_cause=outage_cause,
            photo_name=photo_name,
            photo_data=photo_data,
            is_tag_out=is_tag or ttype in TAG_OUT_TYPES,
            tag_reason=tag_reason,
        )
        if light_id and light_id != "TAGOUT":
            log_light_event(
                conn,
                circuit_number,
                light_id,
                map_number=map_number,
                ticket_id=tid,
                event_type=ttype,
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
    truck = st.session_state.get("truck_mode", False)
    st.header("Active call log" + (" — TRUCK MODE" if truck else ""))
    if truck:
        st.markdown(
            "<style>html, body, [class*='css'] { font-size: 1.15rem !important; }</style>",
            unsafe_allow_html=True,
        )
    df = tickets_df(conn, "active")

    c1, c2, c3, c4 = st.columns(4)
    q = c1.text_input("Search")
    circuit = c2.text_input("Circuit")
    wo = c3.text_input("Record #")
    board = c4.selectbox("Board filter", BOARD_FILTERS)
    if not truck:
        light = st.text_input("Location")
        ttype = st.selectbox("Type", ["All"] + TICKET_TYPES)
    else:
        light, ttype = "", "All"

    filtered = filter_tickets(df, q, circuit, light, ttype, None, None, work_order=wo)
    filtered = filter_board(filtered, board)

    if filtered.empty:
        st.info("No active calls match.")
        return

    st.write(f"**{len(filtered)}** active · **{board}**")

    cols = [
        "id",
        "created_at",
        "ticket_type",
        "is_tag_out",
        "circuit_number",
        "light_number",
        "map_number",
        "lub",
        "fud",
        "outage_cause",
        "work_order",
        "created_by",
        "tag_reason",
        "pedestal_cut",
        "location",
        "description",
        "is_flagged",
        "flag_reason",
    ]
    show = filtered[[c for c in cols if c in filtered.columns]].copy()
    rename = {
        "id": "Ticket",
        "created_at": "Logged",
        "ticket_type": "Type",
        "is_tag_out": "Tag out",
        "circuit_number": "Circuit",
        "light_number": "Location",
        "map_number": "Light #",
        "lub": "LUB",
        "fud": "FUD",
        "outage_cause": "Cause",
        "work_order": "Record #",
        "created_by": "Crew",
        "tag_reason": "Tag reason",
        "pedestal_cut": "Pedestal cut",
        "location": "Location",
        "description": "Notes",
        "is_flagged": "Flagged",
        "flag_reason": "Flag reason",
    }
    show = show.rename(columns={k: v for k, v in rename.items() if k in show.columns})
    if "Tag out" in show.columns:
        show["Tag out"] = show["Tag out"].map({1: "YES", 0: ""})
    if "Flagged" in show.columns:
        show["Flagged"] = show["Flagged"].map({1: "YES", 0: ""})
    if "Pedestal cut" in show.columns:
        show["Pedestal cut"] = show["Pedestal cut"].map({1: "YES", 0: ""})
    if "Type" in show.columns:
        show["Type"] = show["Type"].map(display_ticket_type)
    st.dataframe(show, use_container_width=True, hide_index=True)

    if not truck:
        st.subheader("Print active board")
        html = build_print_board_html(filtered, title=f"Active board — {board}")
        st.download_button(
            "Download board for print (HTML)",
            html.encode("utf-8"),
            file_name=f"active_board_{datetime.now().strftime('%Y%m%d_%H%M')}.html",
            mime="text/html",
        )
        with st.expander("Preview print board"):
            st.components.v1.html(html, height=360, scrolling=True)

        st.subheader("Daily shift sheet")
        shift_html = build_shift_sheet_html(conn)
        st.download_button(
            "Download shift sheet (HTML)",
            shift_html.encode("utf-8"),
            file_name=f"shift_sheet_{date.today().isoformat()}.html",
            mime="text/html",
        )

    st.subheader("Complete or release tag")
    ids = filtered["id"].tolist()
    pick = st.selectbox("Ticket #", ids)
    row = filtered[filtered["id"] == pick].iloc[0]
    is_tag = int(row.get("is_tag_out") or 0) == 1 or str(row.get("ticket_type") or "") in TAG_OUT_TYPES
    st.caption(
        f"{display_ticket_type(row.get('ticket_type'))} · {row.get('circuit_number')} · "
        f"{row.get('light_number') or ''} · Rec {row.get('work_order') or '—'} · "
        f"cause: {row.get('outage_cause') or '—'}"
    )
    if is_tag:
        st.info(f"Tag reason: {row.get('tag_reason') or row.get('description') or '—'}")
        rel_note = st.text_input("Release note", value="Tag released — circuit clear", key="rel_note")
        if st.button("Release tag out", type="primary", key="btn_release_tag"):
            release_tag_out(conn, int(pick), notes=rel_note)
            st.success(f"Tag out #{pick} released.")
            st.rerun()
    findings = st.text_area(
        "What we found / what we did",
        placeholder="Replaced photocell · repaired cut · no trouble found",
        key="complete_findings",
    )
    notes = st.text_input("Short closeout note", placeholder="Optional one-liner")
    closer = st.text_input("Closed by", value=current_user_name())
    if st.button("Mark completed", type="primary", key="btn_complete"):
        complete_ticket(conn, int(pick), notes, findings=findings, completed_by=closer)
        st.success(f"Ticket #{pick} completed by {closer}.")
        st.rerun()

    if "photo_data" in filtered.columns:
        prow = filtered[filtered["id"] == pick]
        if not prow.empty:
            show_ticket_photo(prow.iloc[0].to_dict())


def page_backup(conn) -> None:
    st.header("Backup download")
    if not is_supervisor():
        st.warning("Backup is **supervisor only**. Sign in with the supervisor password.")
        return
    st.caption("Download tables as CSV (zip). Keep a copy outside Streamlit/Turso.")
    import zipfile

    tables = {
        "tickets": "SELECT * FROM tickets ORDER BY id",
        "circuits": "SELECT * FROM circuits ORDER BY id",
        "circuit_lights": "SELECT * FROM circuit_lights ORDER BY id",
        "light_events": "SELECT * FROM light_events ORDER BY id",
        "circuit_pdfs": "SELECT id, circuit_id, filename, uploaded_at FROM circuit_pdfs ORDER BY id",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, sql in tables.items():
            rows = q_all(conn, sql)
            df = pd.DataFrame(rows) if rows else pd.DataFrame()
            # Drop bulky photo_data from ticket export to keep zip usable
            if name == "tickets" and not df.empty and "photo_data" in df.columns:
                df = df.drop(columns=["photo_data"])
            zf.writestr(f"{name}.csv", df.to_csv(index=False))
    st.download_button(
        "Download full backup (ZIP of CSVs)",
        buf.getvalue(),
        file_name=f"streetlight_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.zip",
        mime="application/zip",
    )
    st.success("Signed in as supervisor — full backup allowed (photos omitted from CSV).")


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
    wo = c4.text_input("Record #")
    ttype = st.selectbox("Type", ["All"] + TICKET_TYPES)

    c5, c6, c7 = st.columns(3)
    status = c5.selectbox("Status", ["All", "active", "completed"])
    start = c6.date_input("From", value=None)
    end = c7.date_input("To", value=None)

    work = df if status == "All" else df[df["status"] == status]
    filtered = filter_tickets(work, q, circuit, light, ttype, start, end, work_order=wo)

    st.write(f"**{len(filtered)}** record(s)")
    if filtered.empty:
        return

    hcols = [
        "id",
        "status",
        "created_at",
        "completed_at",
        "ticket_type",
        "is_tag_out",
        "circuit_number",
        "light_number",
        "map_number",
        "lub",
        "fud",
        "outage_cause",
        "work_order",
        "created_by",
        "completed_by",
        "findings",
        "tag_reason",
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
            "is_tag_out": "Tag out",
            "circuit_number": "Circuit",
            "light_number": "Location",
            "map_number": "Light #",
            "lub": "LUB",
            "fud": "FUD",
            "outage_cause": "Cause",
            "work_order": "Record #",
            "created_by": "Opened by",
            "completed_by": "Closed by",
            "findings": "Findings",
            "tag_reason": "Tag reason",
            "pedestal_cut": "Pedestal cut",
            "location": "Location",
            "description": "Notes",
            "flag_reason": "Flag",
            "completion_notes": "Closeout",
        }
    )
    if "Tag out" in show.columns:
        show["Tag out"] = show["Tag out"].map({1: "YES", 0: ""})
    if "Pedestal cut" in show.columns:
        show["Pedestal cut"] = show["Pedestal cut"].map({1: "YES", 0: ""})
    if "Type" in show.columns:
        show["Type"] = show["Type"].map(display_ticket_type)
    st.dataframe(show, use_container_width=True, hide_index=True)

    csv_bytes = show.to_csv(index=False).encode("utf-8")
    st.download_button("Download results as CSV", csv_bytes, "streetlight_history.csv", "text/csv")

    st.subheader("View photo")
    with_photo = filtered
    if "photo_data" in filtered.columns:
        with_photo = filtered[filtered["photo_data"].notna() & (filtered["photo_data"].astype(str) != "")]
    if with_photo.empty:
        st.caption("No photos on these results.")
    else:
        pid = st.selectbox("Ticket with photo", with_photo["id"].tolist())
        prow = with_photo[with_photo["id"] == pid].iloc[0]
        show_ticket_photo(prow.to_dict())

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

    can_edit = is_supervisor()
    if can_edit:
        tab_list, tab_add, tab_edit, tab_import, tab_pdf = st.tabs(
            ["Circuit list", "Add circuit", "Edit / delete", "Import light order", "Upload PDF"]
        )
    else:
        tab_list = st.container()
        tab_add = tab_edit = tab_import = tab_pdf = None
        st.caption("Crew view — look up circuits and lights only. Supervisor can edit maps.")

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
            picked = next((c for c in circuits if c.get("circuit_number") == pick), None)
            st.write(f"Selected **{pick}**")
            if can_edit:
                a2, a3 = st.columns(2)
                if a2.button("Edit circuit", key=f"btn_edit_circ_{pick}"):
                    st.session_state["edit_circuit"] = pick
                if a3.button("Delete circuit", key=f"btn_del_circ_{pick}"):
                    if picked and picked.get("id") is not None:
                        st.session_state["delete_circuit"] = pick
                        st.session_state["delete_circuit_id"] = picked.get("id")

            if st.session_state.get("delete_circuit") == pick and picked:
                _open_delete_circuit_dialog(picked.get("id"), pick)

            if st.session_state.get("edit_circuit") == pick and picked:
                with st.form("list_edit_circuit"):
                    new_num = st.text_input("Circuit number", value=str(picked.get("circuit_number") or ""))
                    types = CIRCUIT_TYPES
                    cur_t = picked.get("circuit_type") or "unknown"
                    idx = types.index(cur_t) if cur_t in types else types.index("unknown")
                    new_type = st.selectbox("Circuit type", types, index=idx, format_func=circuit_label)
                    new_desc = st.text_area("Notes", value=str(picked.get("description") or ""))
                    save_c = st.form_submit_button("Save circuit")
                if save_c and new_num.strip():
                    conn.execute(
                        "UPDATE circuits SET circuit_number = ?, circuit_type = ?, description = ? WHERE id = ?",
                        (new_num.strip(), new_type, new_desc.strip() or None, picked.get("id")),
                    )
                    conn.commit()
                    st.session_state["edit_circuit"] = None
                    st.success("Circuit updated.")
                    st.rerun()
                if st.button("Cancel circuit edit"):
                    st.session_state["edit_circuit"] = None
                    st.rerun()

            lights = q_all(
                conn,
                """
                SELECT cl.id, cl.sequence, cl.light_number, cl.map_number, cl.street, cl.side,
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
                            "Location": r.get("light_number"),
                            "Light #": r.get("map_number") or "",
                            "Note": r.get("location_note") or "",
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
                if can_edit:
                    st.caption("Edit / delete a light below")
                    for r in lights:
                        lid = r.get("id")
                        lab = f"{r.get('light_number')}  ·  #{r.get('map_number') or '—'}  ·  {r.get('sequence') or '—'}"
                        c_l, c_e, c_d = st.columns([4, 1, 1])
                        c_l.write(lab)
                        if c_e.button("Edit", key=f"edit_l_{lid}"):
                            st.session_state["edit_light_id"] = lid
                        if c_d.button("Delete", key=f"del_l_{lid}"):
                            st.session_state["delete_light_id"] = lid

                edit_id = st.session_state.get("edit_light_id")
                if edit_id:
                    cur = next((x for x in lights if x.get("id") == edit_id), None)
                    if cur:
                        st.markdown(f"**Editing** {cur.get('light_number')}")
                        with st.form("list_edit_light"):
                            new_callout = st.text_input("Location", value=str(cur.get("light_number") or ""))
                            map_n = st.text_input("Light #", value=str(cur.get("map_number") or ""))
                            seq = st.text_input("Sequence", value=str(cur.get("sequence") or ""))
                            loc = st.text_input("Location note", value=str(cur.get("location_note") or ""))
                            e1, e2, e3 = st.columns(3)
                            st_street = e1.text_input("Street", value=str(cur.get("street") or ""))
                            side_val = str(cur.get("side") or "")
                            st_side = e2.selectbox(
                                "Side",
                                SIDES,
                                index=SIDES.index(side_val) if side_val in SIDES else 0,
                            )
                            nth_now = int(cur.get("nth") or 0)
                            st_nth = e3.number_input("Nth from cross", min_value=0, step=1, value=nth_now)
                            e4, e5 = st.columns(2)
                            dir_val = str(cur.get("from_dir") or "")
                            st_dir = e4.selectbox(
                                "Dir from cross",
                                DIRS,
                                index=DIRS.index(dir_val) if dir_val in DIRS else 0,
                            )
                            st_cross = e5.text_input("Cross street", value=str(cur.get("cross_street") or ""))
                            p1, p2, p3 = st.columns(3)
                            pm = str(cur.get("pole_material") or "")
                            pole_material = p1.selectbox(
                                "Pole type",
                                POLE_MATERIALS,
                                index=POLE_MATERIALS.index(pm) if pm in POLE_MATERIALS else 0,
                            )
                            pole_height = p2.text_input("Pole height", value=str(cur.get("pole_height") or ""))
                            fixture_type = p3.text_input("Fixture type", value=str(cur.get("fixture_type") or ""))
                            save_l = st.form_submit_button("Save light")
                        if save_l:
                            err = validate_sequence_or_error(seq)
                            if not new_callout.strip():
                                st.error("Location cannot be empty.")
                            elif err:
                                st.error(err)
                            else:
                                conn.execute(
                                    """
                                    UPDATE circuit_lights SET
                                        light_number = ?, map_number = ?, street = ?, side = ?,
                                        nth = ?, from_dir = ?, cross_street = ?, sequence = ?,
                                        location_note = ?, pole_material = ?, pole_height = ?,
                                        fixture_type = ?
                                    WHERE id = ?
                                    """,
                                    (
                                        new_callout.strip(),
                                        map_n.strip() or None,
                                        st_street.strip() or None,
                                        st_side or None,
                                        int(st_nth) if st_nth else None,
                                        st_dir or None,
                                        st_cross.strip() or None,
                                        normalize_seq(seq) or None,
                                        loc.strip() or None,
                                        pole_material or None,
                                        pole_height.strip() or None,
                                        fixture_type.strip() or None,
                                        edit_id,
                                    ),
                                )
                                conn.commit()
                                log_light_event(
                                    conn,
                                    pick,
                                    new_callout.strip(),
                                    map_number=map_n,
                                    event_type="update",
                                    pole_material=pole_material,
                                    pole_height=pole_height,
                                    fixture_type=fixture_type,
                                    notes="Edited from circuit list",
                                )
                                st.session_state["edit_light_id"] = None
                                st.success("Light saved.")
                                st.rerun()
                        if st.button("Cancel light edit"):
                            st.session_state["edit_light_id"] = None
                            st.rerun()

                del_id = st.session_state.get("delete_light_id")
                if del_id:
                    cur = next((x for x in lights if x.get("id") == del_id), None)
                    name = (cur.get("light_number") if cur else None) or str(del_id)
                    _open_delete_light_dialog(del_id, name, pick)
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

    if can_edit:
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
            "Identify the head by **Location** (street, side, nth light from the cross, and cross street) "
            "plus **Light #** if you have it. Light # can repeat on other streets. "
            "Sequence from the print: `1`, `1a`, `1a1`, `1b`."
        )
        with st.form("add_light"):
            cn2 = st.text_input("Circuit number", key="al_cn")
            map_n = st.text_input("Light #", placeholder="305")
            b1, b2, b3, b4, b5 = st.columns(5)
            st_street = b1.text_input("Street", placeholder="1")
            st_side = b2.selectbox("Side", SIDES, key="al_side")
            st_nth = b3.number_input("Nth from cross", min_value=0, step=1, value=0, key="al_nth")
            st_dir = b4.selectbox("Dir from cross", DIRS, key="al_dir")
            st_cross = b5.text_input("Cross street", placeholder="Mason")
            ln = st.text_input(
                "Location (optional if you filled the boxes)",
                placeholder="leave blank to use street / side / nth / cross",
            )
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
                st.error("Enter a location so two lights with the same Light # stay distinct.")
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

    if can_edit:
      with tab_edit:
        st.subheader("Edit or delete a light")
        circs = q_all(conn, "SELECT circuit_number FROM circuits ORDER BY circuit_number")
        circ_opts = [r.get("circuit_number") for r in circs]
        if not circ_opts:
            st.info("No circuits yet.")
        else:
            ecn = st.selectbox("Circuit", circ_opts, key="ed_cn")
            lights_ed = q_all(
                conn,
                """
                SELECT cl.*
                FROM circuit_lights cl
                JOIN circuits c ON c.id = cl.circuit_id
                WHERE c.circuit_number = ?
                """,
                (ecn,),
            )
            lights_ed = sorted(
                lights_ed,
                key=lambda r: (seq_sort_key(r.get("sequence")), str(r.get("light_number") or "")),
            )
            if not lights_ed:
                st.caption("No lights on this circuit.")
            else:
                labels = [
                    f"{r.get('light_number')}  ·  #{r.get('map_number') or '—'}  ·  seq {r.get('sequence') or '—'}"
                    for r in lights_ed
                ]
                pick_i = st.selectbox(
                    "Light",
                    range(len(lights_ed)),
                    format_func=lambda i: labels[i],
                    key="ed_light",
                )
                cur = lights_ed[pick_i]
                with st.form("edit_light"):
                    new_callout = st.text_input("Location", value=str(cur.get("light_number") or ""))
                    map_n = st.text_input("Light #", value=str(cur.get("map_number") or ""))
                    e1, e2, e3, e4, e5 = st.columns(5)
                    st_street = e1.text_input("Street", value=str(cur.get("street") or ""))
                    side_val = str(cur.get("side") or "")
                    st_side = e2.selectbox(
                        "Side",
                        SIDES,
                        index=SIDES.index(side_val) if side_val in SIDES else 0,
                    )
                    nth_now = int(cur.get("nth") or 0)
                    st_nth = e3.number_input("Nth from cross", min_value=0, step=1, value=nth_now)
                    dir_val = str(cur.get("from_dir") or "")
                    st_dir = e4.selectbox(
                        "Dir from cross",
                        DIRS,
                        index=DIRS.index(dir_val) if dir_val in DIRS else 0,
                    )
                    st_cross = e5.text_input("Cross street", value=str(cur.get("cross_street") or ""))
                    seq = st.text_input("Sequence", value=str(cur.get("sequence") or ""))
                    loc = st.text_input("Location note", value=str(cur.get("location_note") or ""))
                    p1, p2, p3 = st.columns(3)
                    pm = str(cur.get("pole_material") or "")
                    pole_material = p1.selectbox(
                        "Pole type",
                        POLE_MATERIALS,
                        index=POLE_MATERIALS.index(pm) if pm in POLE_MATERIALS else 0,
                    )
                    pole_height = p2.text_input("Pole height", value=str(cur.get("pole_height") or ""))
                    fixture_type = p3.text_input("Fixture type", value=str(cur.get("fixture_type") or ""))
                    save_l = st.form_submit_button("Save light changes")
                if save_l:
                    if not new_callout.strip():
                        st.error("Location cannot be empty.")
                    else:
                        err = validate_sequence_or_error(seq)
                        if err:
                            st.error(err)
                        else:
                            try:
                                conn.execute(
                                    """
                                    UPDATE circuit_lights SET
                                        light_number = ?, map_number = ?, street = ?, side = ?,
                                        nth = ?, from_dir = ?, cross_street = ?, sequence = ?,
                                        location_note = ?, pole_material = ?, pole_height = ?,
                                        fixture_type = ?
                                    WHERE id = ?
                                    """,
                                    (
                                        new_callout.strip(),
                                        map_n.strip() or None,
                                        st_street.strip() or None,
                                        st_side or None,
                                        int(st_nth) if st_nth else None,
                                        st_dir or None,
                                        st_cross.strip() or None,
                                        normalize_seq(seq) or None,
                                        loc.strip() or None,
                                        pole_material or None,
                                        pole_height.strip() or None,
                                        fixture_type.strip() or None,
                                        cur.get("id"),
                                    ),
                                )
                                conn.commit()
                                log_light_event(
                                    conn,
                                    ecn,
                                    new_callout.strip(),
                                    map_number=map_n,
                                    event_type="update",
                                    pole_material=pole_material,
                                    pole_height=pole_height,
                                    fixture_type=fixture_type,
                                    notes="Edited light record",
                                )
                                st.success(f"Updated {new_callout.strip()}.")
                                st.rerun()
                            except sqlite3.Error as e:
                                st.error(str(e))

                st.markdown("---")
                st.subheader("Delete this light")
                st.caption("Removes the light from the circuit map. Ticket history is kept.")
                if st.button("Delete light…", type="primary", key="tab_del_light_btn"):
                    st.session_state["delete_light_id"] = cur.get("id")
                    st.session_state["delete_light_name"] = cur.get("light_number")
                    st.session_state["delete_light_circuit"] = ecn
                if st.session_state.get("delete_light_id") == cur.get("id"):
                    _open_delete_light_dialog(
                        cur.get("id"),
                        str(cur.get("light_number") or cur.get("id")),
                        ecn,
                    )

        st.markdown("---")
        st.subheader("Delete a whole circuit")
        st.caption(
            "Deletes the circuit, its lights, and attached PDF records. "
            "Tickets and light history stay so old jobs can still be searched."
        )
        circs2 = q_all(
            conn,
            "SELECT id, circuit_number FROM circuits ORDER BY circuit_number",
        )
        opts2 = [r.get("circuit_number") for r in circs2]
        if opts2:
            dcn = st.selectbox("Circuit to delete", opts2, key="del_cn")
            if st.button("Delete circuit…", key="tab_del_circ_btn"):
                row = next((r for r in circs2 if r.get("circuit_number") == dcn), None)
                if row and row.get("id") is not None:
                    st.session_state["delete_circuit"] = dcn
                    st.session_state["delete_circuit_id"] = row.get("id")
            if st.session_state.get("delete_circuit") == dcn:
                row = next((r for r in circs2 if r.get("circuit_number") == dcn), None)
                if row:
                    _open_delete_circuit_dialog(row.get("id"), dcn)

    if can_edit:
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

    if can_edit:
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
    callout = c2.text_input("Location")
    mapn = c3.text_input("Light #")
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
                "Location": e.get("light_number") or "",
                "Light #": e.get("map_number") or "",
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
                "Location": ln or "",
                "Light #": e.get("map_number") or "",
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
        "Location": st.column_config.TextColumn(width="medium"),
        "Light #": st.column_config.TextColumn(width="small"),
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
        "Location": st.column_config.TextColumn(width="medium"),
        "Light #": st.column_config.TextColumn(width="small"),
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
            m1.metric("Light #", cur.get("map_number") or "—")
            m2.metric("Pole", f"{cur.get('pole_material') or '—'} {cur.get('pole_height') or ''}".strip())
            m3.metric("Fixture", cur.get("fixture_type") or "—")
            m4.metric("Sequence", cur.get("sequence") or "—")
            if cur.get("location_note"):
                st.caption(cur.get("location_note"))
        else:
            st.caption("No Circuits & maps record for that location yet.")


def page_address_search(conn) -> None:
    st.header("Address search")
    st.caption(
        "Type a house address or intersection. The app looks up **stored lights** "
        "(street, cross street, Light #, location, notes) and lists likely **circuits**. "
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
            "Add lights under Circuits & maps (street + Light # + location) so this search has something to match."
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
                "Location": h.get("light_number"),
                "Light #": h.get("map_number") or "",
                "Seq": h.get("sequence") or "",
                "Branch": branch_label(h.get("sequence")),
                "Why": "; ".join(h["reasons"]),
                "Note": h.get("location_note") or "",
            }
        )
    st.subheader("Nearest stored lights (by map # and street text)")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def circuit_lights_list(conn, circuit_number: str) -> list[dict]:
    return q_all(
        conn,
        """
        SELECT cl.id, cl.sequence, cl.light_number, cl.map_number, cl.location_note
        FROM circuit_lights cl
        JOIN circuits c ON c.id = cl.circuit_id
        WHERE c.circuit_number = ?
        """,
        (circuit_number.strip(),),
    )


def match_sequence_on_circuit(lights: list[dict], ref: str) -> str | None:
    """Match LUB/FUD/location text to a mapped light's sequence."""
    ref_n = normalize_seq(ref)
    if not ref_n:
        return None
    for r in lights:
        seq = normalize_seq(r.get("sequence"))
        if seq and (seq == ref_n or normalize_seq(r.get("light_number")) == ref_n):
            return seq
        if normalize_seq(r.get("map_number")) == ref_n:
            return seq or None
        ln = str(r.get("light_number") or "").strip().lower()
        if ln and ln == str(ref or "").strip().lower():
            return seq or None
    # bare sequence typed as LUB/FUD
    if is_valid_sequence(ref) and normalize_seq(ref):
        return normalize_seq(ref)
    return None


def dark_keys_for_ticket(conn, ticket: dict, cache: dict) -> tuple[set[str], str]:
    """
    Unique keys for lights treated as out on this ticket.
    Key format: circuit|sequence_or_id
    Returns (keys, method label).
    """
    cn = str(ticket.get("circuit_number") or "").strip()
    ttype = display_ticket_type(ticket.get("ticket_type"))
    is_tag = int(ticket.get("is_tag_out") or 0) == 1 or ttype in TAG_OUT_TYPES
    keys: set[str] = set()

    if not cn:
        return keys, "no circuit"

    if cn not in cache:
        cache[cn] = circuit_lights_list(conn, cn)
    lights = cache[cn]

    lub = _norm(str(ticket.get("lub") or ""))
    fud = _norm(str(ticket.get("fud") or ""))
    loc = _norm(str(ticket.get("light_number") or ticket.get("location") or ""))
    map_n = _norm(str(ticket.get("map_number") or ""))

    # Tag out / feeder down: all mapped heads on the circuit
    if is_tag:
        if lights:
            for r in lights:
                ident = normalize_seq(r.get("sequence")) or str(r.get("id"))
                keys.add(f"{cn}|{ident}")
            return keys, f"tag out — all {len(keys)} mapped"
        keys.add(f"{cn}|TAG")
        return keys, "tag out — circuit not mapped (count 1)"

    # LUB/FUD stretch: FUD and everything same-branch after it
    fud_seq = match_sequence_on_circuit(lights, fud) if fud else None
    lub_seq = match_sequence_on_circuit(lights, lub) if lub else None
    if fud_seq and lights:
        for r in lights:
            seq = normalize_seq(r.get("sequence"))
            if not seq:
                continue
            if same_branch_at_or_after(seq, fud_seq):
                keys.add(f"{cn}|{seq}")
        if keys:
            return keys, f"LUB/FUD stretch from {fud_seq} ({len(keys)} mapped)"
    if (lub or fud) and not keys:
        # has LUB/FUD text but no map match — still at least the reported stretch
        keys.add(f"{cn}|FUD:{fud or lub}")
        return keys, "LUB/FUD — not fully mapped (count 1)"

    # Single-head / local trouble
    single_types = {
        "UGT",
        "Knockdown",
        "Bad Fixture",
        "Bad Ignitor",
        "Bad Ballast",
        "Deteriorated Pole",
        "Damaged Pedestal",
        "Vandalism",
        "Damage",
        "Cable Theft",
        "Wires Cut in pedestal",
        "Trouble",
        "Other",
    }
    if ttype in single_types or True:
        # Prefer matching one mapped light by location / light #
        target = loc or map_n
        if target and lights:
            for r in lights:
                if normalize_seq(r.get("light_number")) == normalize_seq(target):
                    ident = normalize_seq(r.get("sequence")) or str(r.get("id"))
                    keys.add(f"{cn}|{ident}")
                    return keys, "single mapped light"
                if map_n and normalize_seq(r.get("map_number")) == normalize_seq(map_n):
                    ident = normalize_seq(r.get("sequence")) or str(r.get("id"))
                    keys.add(f"{cn}|{ident}")
                    return keys, "single mapped light #"
        # Wires cut without FUD: if parallel pedestal, unknown stretch — count 1
        keys.add(f"{cn}|{normalize_seq(target) or ticket.get('id')}")
        return keys, "single call (1)"

    return keys, "unknown"


def estimate_lights_out(conn, active_df: pd.DataFrame) -> tuple[int, list[dict], set[str]]:
    """Total unique estimated dark lights across active tickets."""
    cache: dict = {}
    all_keys: set[str] = set()
    rows = []
    if active_df is None or active_df.empty:
        return 0, [], set()
    for _, t in active_df.iterrows():
        td = t.to_dict() if hasattr(t, "to_dict") else dict(t)
        keys, method = dark_keys_for_ticket(conn, td, cache)
        all_keys |= keys
        rows.append(
            {
                "Ticket": td.get("id"),
                "Type": display_ticket_type(td.get("ticket_type")),
                "Circuit": td.get("circuit_number"),
                "Location": td.get("light_number") or "",
                "LUB": td.get("lub") or "",
                "FUD": td.get("fud") or "",
                "Est. dark (this call)": len(keys),
                "How counted": method,
            }
        )
    return len(all_keys), rows, all_keys


def page_reports(conn: sqlite3.Connection) -> None:
    st.header("Reports")
    df = tickets_df(conn)
    if df.empty:
        st.info("No data yet.")
        return

    active = df[df["status"] == "active"].copy()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Active calls", len(active))
    c2.metric("All-time tickets", len(df))
    c3.metric("Flagged (all time)", int(df["is_flagged"].sum()) if "is_flagged" in df.columns else 0)
    c4.metric("Circuits used", df["circuit_number"].nunique())

    st.subheader("Estimated lights out (active)")
    st.caption(
        "Counts unique heads across **all active call types** (Trouble, UGT, theft, knockdown, "
        "tag outs, etc.). LUB/FUD uses the circuit map: **FUD and everything downstream on that leg**. "
        "Tag outs count every mapped light on the circuit. Unmapped calls count as 1 each. "
        "Same light on two tickets is only counted once."
    )
    type_opts = ["All"] + list(TICKET_TYPES)
    # also include any odd stored types
    if not active.empty and "ticket_type" in active.columns:
        for t in sorted(active["ticket_type"].dropna().unique()):
            dt = display_ticket_type(t)
            if dt not in type_opts:
                type_opts.append(dt)
    picked_types = st.multiselect(
        "Filter by call type",
        options=[x for x in type_opts if x != "All"],
        default=[],
        help="Leave empty to include every active call type.",
    )
    filtered_active = active
    if picked_types and not active.empty:
        def _type_ok(val) -> bool:
            return display_ticket_type(val) in picked_types or str(val) in picked_types

        filtered_active = active[active["ticket_type"].map(_type_ok)]

    total_out, detail_rows, _keys = estimate_lights_out(conn, filtered_active)
    m1, m2, m3 = st.columns(3)
    m1.metric("Est. lights out", total_out)
    m2.metric("Active calls in filter", len(filtered_active))
    m3.metric(
        "Types selected",
        "All" if not picked_types else len(picked_types),
    )

    if detail_rows:
        detail_df = pd.DataFrame(detail_rows)
        st.dataframe(detail_df, use_container_width=True, hide_index=True)
        if not filtered_active.empty and "ticket_type" in filtered_active.columns:
            st.caption("By call type (this filter)")
            by_type = (
                detail_df.groupby("Type")["Est. dark (this call)"]
                .sum()
                .sort_values(ascending=False)
            )
            st.bar_chart(by_type)
        st.download_button(
            "Download lights-out estimate (CSV)",
            detail_df.to_csv(index=False).encode("utf-8"),
            file_name=f"lights_out_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
        )
    else:
        st.info("No active calls match this filter.")

    st.subheader("Active by type")
    if not active.empty:
        shown = active["ticket_type"].map(display_ticket_type).value_counts()
        st.bar_chart(shown)
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
        st.dataframe(
            repeats.rename(columns={"circuit_number": "Circuit", "light_number": "Location"}),
            hide_index=True,
            use_container_width=True,
        )


def main() -> None:
    st.set_page_config(page_title="Street Light Tracker", page_icon="💡", layout="wide")

    if not require_login():
        return

    init_db()
    conn = get_conn()

    st.title("Street Light Tracker")
    st.caption("Troubles · damage · cable theft — LUB / FUD, active log, history")
    if st.session_state.get("flash_ok"):
        st.success(st.session_state.pop("flash_ok"))
    if st.session_state.get("flash_err"):
        st.error(st.session_state.pop("flash_err"))
    render_undo_banner()

    truck = st.sidebar.checkbox("Truck mode (simple)", value=st.session_state.get("truck_mode", False))
    st.session_state["truck_mode"] = truck

    if truck:
        nav = ["Active calls", "New call", "History"]
    elif is_supervisor():
        nav = [
            "Active calls",
            "New call",
            "Address search",
            "Light history",
            "History",
            "Circuits & maps",
            "Backup",
            "Reports",
        ]
    else:
        # Crew: view circuits, no Backup
        nav = [
            "Active calls",
            "New call",
            "Address search",
            "Light history",
            "History",
            "Circuits & maps",
            "Reports",
        ]

    page = st.sidebar.radio("Go to", nav)

    backend = "Turso (cloud)" if using_turso() else "Local SQLite"
    st.sidebar.caption(f"Database: **{backend}**")
    if not is_supervisor():
        st.sidebar.caption("Crew view — circuits are view-only; Backup is supervisor only")

    row = q_one(conn, "SELECT COUNT(*) AS n FROM tickets WHERE status = 'active'")
    active_n = (row or {}).get("n", 0)
    st.sidebar.metric("Active now", active_n)
    tag_n = q_one(
        conn,
        "SELECT COUNT(*) AS n FROM tickets WHERE status = 'active' AND (is_tag_out = 1 OR ticket_type IN ('Tag out', 'Tag Out'))",
    )
    st.sidebar.metric("Tag outs", (tag_n or {}).get("n", 0))

    if page == "Active calls":
        page_active(conn)
    elif page == "New call":
        page_new_call(conn)
    elif page == "Address search":
        page_address_search(conn)
    elif page == "Light history":
        page_light_history(conn)
    elif page == "Backup":
        page_backup(conn)
    elif page == "History":
        page_history(conn)
    elif page == "Circuits & maps":
        page_circuits(conn)
    else:
        page_reports(conn)

    conn.close()


if __name__ == "__main__":
    main()
