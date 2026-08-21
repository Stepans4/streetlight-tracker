#!/usr/bin/env python3
"""Street Light Outage Tracker — Streamlit + SQLite / Turso."""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from pypdf import PdfReader

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PDF_DIR = DATA_DIR / "pdfs"
DB_PATH = DATA_DIR / "tracker.db"

TICKET_TYPES = ["Outage", "Damage", "Cable Theft", "Pedestal cut", "Other"]
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
        sequence INTEGER,
        location_note TEXT,
        UNIQUE(circuit_id, light_number),
        FOREIGN KEY (circuit_id) REFERENCES circuits(id) ON DELETE CASCADE
    )
    """,
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


def init_db() -> None:
    conn = get_conn()
    for stmt in SCHEMA_STATEMENTS:
        _run_sql(conn, stmt)
    conn.commit()
    _migrate_tickets(conn)
    conn.close()


def _migrate_tickets(conn) -> None:
    try:
        rows = conn.execute("PRAGMA table_info(tickets)").fetchall()
        cols = set()
        for row in rows:
            # Row may be tuple or sqlite3.Row
            if isinstance(row, sqlite3.Row):
                cols.add(row[1])
            else:
                cols.add(row[1])
        if "lub" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN lub TEXT")
        if "fud" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN fud TEXT")
        if "pedestal_cut" not in cols:
            conn.execute(
                "ALTER TABLE tickets ADD COLUMN pedestal_cut INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()
    except Exception:
        # Fresh Turso DB already has columns from CREATE TABLE
        pass


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


def get_circuit(conn: sqlite3.Connection, circuit_number: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM circuits WHERE circuit_number = ?",
        (circuit_number.strip(),),
    ).fetchone()


def get_or_create_circuit(
    conn: sqlite3.Connection, circuit_number: str, circuit_type: str = "unknown"
) -> sqlite3.Row:
    row = get_circuit(conn, circuit_number)
    if row:
        return row
    conn.execute(
        "INSERT INTO circuits (circuit_number, circuit_type, created_at) VALUES (?, ?, ?)",
        (circuit_number.strip(), circuit_type, now_iso()),
    )
    conn.commit()
    return get_circuit(conn, circuit_number)


def get_light_sequence(conn: sqlite3.Connection, circuit_number: str, light_number: str) -> int | None:
    row = conn.execute(
        """
        SELECT cl.sequence
        FROM circuit_lights cl
        JOIN circuits c ON c.id = cl.circuit_id
        WHERE c.circuit_number = ? AND cl.light_number = ?
        """,
        (circuit_number.strip(), str(light_number).strip()),
    ).fetchone()
    if row and row["sequence"] is not None:
        return int(row["sequence"])
    return None


def _norm(value: str | None) -> str:
    return (value or "").strip()


def ticket_units(row) -> list[str]:
    units = []
    for key in ("light_number", "lub", "fud"):
        try:
            val = _norm(row[key] if not isinstance(row, dict) else row.get(key))
        except (KeyError, IndexError):
            val = ""
        if val:
            units.append(val)
    return units


def check_duplicate(
    conn: sqlite3.Connection,
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

    active = conn.execute(
        """
        SELECT id, light_number, lub, fud, ticket_type, created_at, pedestal_cut
        FROM tickets
        WHERE status = 'active' AND circuit_number = ?
        ORDER BY created_at
        """,
        (circuit_number,),
    ).fetchall()

    if not active:
        return False, "", None

    circuit = get_circuit(conn, circuit_number)
    ctype = (circuit["circuit_type"] if circuit else "unknown") or "unknown"

    for t in active:
        existing = ticket_units(t)
        if new_units and set(new_units) & set(existing):
            reason = (
                f"Same unit already on ticket #{t['id']} "
                f"({t['ticket_type']}, LUB {t['lub'] or '—'} / FUD {t['fud'] or '—'}, {t['created_at']})"
            )
            return True, reason, t["id"]

        new_seqs = [get_light_sequence(conn, circuit_number, u) for u in new_units]
        new_seqs = [s for s in new_seqs if s is not None]
        t_fud_seq = get_light_sequence(conn, circuit_number, t["fud"] or "")
        t_lub_seq = get_light_sequence(conn, circuit_number, t["lub"] or "")
        t_light_seq = get_light_sequence(conn, circuit_number, t["light_number"] or "")
        break_after = t_lub_seq
        dark_from = t_fud_seq if t_fud_seq is not None else t_light_seq

        if new_seqs and (dark_from is not None or break_after is not None):
            for ns in new_seqs:
                after_lub = break_after is not None and ns > break_after
                at_or_after_fud = dark_from is not None and ns >= dark_from
                if after_lub or at_or_after_fud:
                    reason = (
                        f"Same break as ticket #{t['id']} — new unit is at/after "
                        f"LUB {t['lub'] or '—'} / FUD {t['fud'] or t['light_number'] or '—'}. "
                        f"Break is between last unit burning and first unit dark "
                        f"(series run or wires cut in a pedestal)."
                    )
                    return True, reason, t["id"]

        new_fud_seq = get_light_sequence(conn, circuit_number, fud) if fud else None
        new_lub_seq = get_light_sequence(conn, circuit_number, lub) if lub else None
        if (
            new_lub_seq is not None
            and new_fud_seq is not None
            and t_lub_seq is not None
            and t_fud_seq is not None
        ):
            overlap = not (new_fud_seq < t_lub_seq or new_lub_seq > t_fud_seq)
            if overlap:
                reason = (
                    f"LUB/FUD range overlaps ticket #{t['id']} "
                    f"(existing LUB {t['lub']} / FUD {t['fud']})."
                )
                return True, reason, t["id"]

    ids = ", ".join(f"#{t['id']}" for t in active)
    summary = "; ".join(
        f"#{t['id']} LUB {t['lub'] or '—'} FUD {t['fud'] or t['light_number'] or '—'}"
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
    return True, reason, active[0]["id"]


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
) -> tuple[int, bool, str]:
    flagged, reason, parent = check_duplicate(conn, circuit_number, light_number, lub, fud)
    cur = conn.execute(
        """
        INSERT INTO tickets (
            ticket_type, circuit_number, light_number, lub, fud, pedestal_cut,
            location, description,
            status, is_flagged, flag_reason, parent_ticket_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
        """,
        (
            ticket_type,
            circuit_number.strip(),
            _norm(light_number) or None,
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
    return cur.lastrowid, flagged, reason


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


def tickets_df(conn: sqlite3.Connection, status: str | None = None) -> pd.DataFrame:
    if status:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tickets ORDER BY created_at DESC").fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    return df


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

    circuits = [r["circuit_number"] for r in conn.execute("SELECT circuit_number FROM circuits ORDER BY circuit_number").fetchall()]

    with st.form("new_call", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        ticket_type = c1.selectbox("Type", TICKET_TYPES)
        circuit_number = c2.text_input("Circuit number *", placeholder="e.g. 1427")
        light_number = c3.text_input("Reported light #", placeholder="from the call")
        l1, l2, l3 = st.columns(3)
        lub = l1.text_input("LUB — last unit burning", placeholder="last light still on")
        fud = l2.text_input("FUD — first unit dark", placeholder="first light out")
        pedestal_cut = l3.checkbox("Wires cut in pedestal")
        location = st.text_input("Location / intersection", placeholder="N 27th & W Capitol")
        description = st.text_area("Notes", placeholder="LUB 12 / FUD 13. Pedestal between them cut.")
        submitted = st.form_submit_button("Log call", type="primary")

    if submitted:
        if not circuit_number.strip():
            st.error("Circuit number is required.")
            return
        get_or_create_circuit(conn, circuit_number)
        tid, flagged, reason = insert_ticket(
            conn,
            ticket_type,
            circuit_number,
            light_number,
            location,
            description,
            lub=lub,
            fud=fud,
            pedestal_cut=pedestal_cut or ticket_type == "Pedestal cut",
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
            "light_number": "Reported",
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
            "light_number": "Reported",
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
        "Upload circuit PDFs and (optionally) enter the light order from the source. "
        "Light order is used with **LUB / FUD** to find the same break on series runs "
        "and on parallel LED circuits where wires are cut in a pedestal."
    )

    tab_list, tab_add, tab_import, tab_pdf = st.tabs(
        ["Circuit list", "Add / edit circuit", "Import light order", "Upload PDF"]
    )

    with tab_list:
        circuits = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM circuit_lights WHERE circuit_id = c.id) AS light_count,
                   (SELECT COUNT(*) FROM circuit_pdfs WHERE circuit_id = c.id) AS pdf_count
            FROM circuits c
            ORDER BY c.circuit_number
            """
        ).fetchall()
        if not circuits:
            st.info("No circuits defined yet. Add one, or log a call — the circuit will be created automatically.")
        else:
            rows = []
            for c in circuits:
                rows.append(
                    {
                        "Circuit": c["circuit_number"],
                        "Type": circuit_label(c["circuit_type"]),
                        "Lights mapped": c["light_count"],
                        "PDFs": c["pdf_count"],
                        "Notes": c["description"] or "",
                    }
                )
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            pick = st.selectbox(
                "View lights on circuit",
                [c["circuit_number"] for c in circuits],
            )
            lights = conn.execute(
                """
                SELECT cl.sequence, cl.light_number, cl.location_note
                FROM circuit_lights cl
                JOIN circuits c ON c.id = cl.circuit_id
                WHERE c.circuit_number = ?
                ORDER BY COALESCE(cl.sequence, 999999), cl.light_number
                """,
                (pick,),
            ).fetchall()
            if lights:
                st.dataframe(
                    pd.DataFrame([dict(r) for r in lights]).rename(
                        columns={
                            "sequence": "Seq (from source)",
                            "light_number": "Light #",
                            "location_note": "Location",
                        }
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.caption("No light order stored for this circuit yet.")

            pdfs = conn.execute(
                """
                SELECT p.id, p.filename, p.uploaded_at, length(p.extracted_text) AS chars
                FROM circuit_pdfs p
                JOIN circuits c ON c.id = p.circuit_id
                WHERE c.circuit_number = ?
                ORDER BY p.uploaded_at DESC
                """,
                (pick,),
            ).fetchall()
            if pdfs:
                st.write("PDFs")
                for p in pdfs:
                    with st.expander(f"{p['filename']}  ·  {p['uploaded_at']}"):
                        text = conn.execute(
                            "SELECT extracted_text FROM circuit_pdfs WHERE id = ?",
                            (p["id"],),
                        ).fetchone()["extracted_text"]
                        st.text_area("Extracted text", text or "(no text)", height=200, key=f"pdftext_{p['id']}")

            search_pdf = st.text_input("Search extracted PDF text across all circuits")
            if search_pdf.strip():
                hits = conn.execute(
                    """
                    SELECT c.circuit_number, p.filename, p.extracted_text
                    FROM circuit_pdfs p
                    JOIN circuits c ON c.id = p.circuit_id
                    WHERE p.extracted_text LIKE ?
                    """,
                    (f"%{search_pdf.strip()}%",),
                ).fetchall()
                if not hits:
                    st.write("No matches.")
                else:
                    for h in hits:
                        st.markdown(f"**Circuit {h['circuit_number']}** — {h['filename']}")
                        st.caption((h["extracted_text"] or "")[:500])

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
        with st.form("add_light"):
            cn2 = st.text_input("Circuit number", key="al_cn")
            ln = st.text_input("Light number")
            seq = st.number_input("Sequence from source (1 = first / upstream)", min_value=1, step=1)
            loc = st.text_input("Location note")
            add_l = st.form_submit_button("Add light")
        if add_l and cn2.strip() and ln.strip():
            circ = get_or_create_circuit(conn, cn2)
            try:
                conn.execute(
                    """
                    INSERT INTO circuit_lights (circuit_id, light_number, sequence, location_note)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(circuit_id, light_number) DO UPDATE SET
                        sequence = excluded.sequence,
                        location_note = excluded.location_note
                    """,
                    (circ["id"], ln.strip(), int(seq), loc.strip() or None),
                )
                conn.commit()
                st.success(f"Light {ln.strip()} on circuit {cn2.strip()} saved at sequence {int(seq)}.")
            except sqlite3.Error as e:
                st.error(str(e))

    with tab_import:
        st.write("CSV columns: `circuit_number,light_number,sequence,location`")
        st.code("1427,1,1,N 27th & Capitol\n1427,2,2,N 27th mid-block\n1427,3,3,N 27th & Keefe", language="csv")
        up = st.file_uploader("CSV file", type=["csv"])
        if up and st.button("Import CSV"):
            text = up.getvalue().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            needed = {"circuit_number", "light_number"}
            if not reader.fieldnames or not needed.issubset({f.strip() for f in reader.fieldnames}):
                st.error("CSV must include circuit_number and light_number columns.")
            else:
                n = 0
                for row in reader:
                    cn = (row.get("circuit_number") or "").strip()
                    ln = (row.get("light_number") or "").strip()
                    if not cn or not ln:
                        continue
                    circ = get_or_create_circuit(conn, cn)
                    seq_raw = (row.get("sequence") or "").strip()
                    seq = int(seq_raw) if seq_raw.isdigit() else None
                    loc = (row.get("location") or "").strip() or None
                    conn.execute(
                        """
                        INSERT INTO circuit_lights (circuit_id, light_number, sequence, location_note)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(circuit_id, light_number) DO UPDATE SET
                            sequence = COALESCE(excluded.sequence, circuit_lights.sequence),
                            location_note = COALESCE(excluded.location_note, circuit_lights.location_note)
                        """,
                        (circ["id"], ln, seq, loc),
                    )
                    n += 1
                conn.commit()
                st.success(f"Imported {n} light row(s).")
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
                (circ["id"], pdf.name, str(dest), extracted, now_iso()),
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
        ["Active calls", "New call", "History", "Circuits & maps", "Reports"],
    )

    backend = "Turso (cloud)" if using_turso() else "Local SQLite"
    st.sidebar.caption(f"Database: **{backend}**")

    row = conn.execute(
        "SELECT COUNT(*) AS n FROM tickets WHERE status = 'active'"
    ).fetchone()
    active_n = row["n"] if isinstance(row, sqlite3.Row) or hasattr(row, "keys") else row[0]
    st.sidebar.metric("Active now", active_n)

    if page == "Active calls":
        page_active(conn)
    elif page == "New call":
        page_new_call(conn)
    elif page == "History":
        page_history(conn)
    elif page == "Circuits & maps":
        page_circuits(conn)
    else:
        page_reports(conn)

    conn.close()


if __name__ == "__main__":
    main()
