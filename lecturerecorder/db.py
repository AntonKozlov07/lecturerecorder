"""SQLite storage for lectures, transcript segments, chats, quizzes and cards."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS lectures (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    course TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    duration REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ready',          -- recording | processing | ready
    notes TEXT NOT NULL DEFAULT '',
    notes_status TEXT NOT NULL DEFAULT 'none',     -- none | generating | done | error
    notes_error TEXT NOT NULL DEFAULT '',
    topics TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lecture_id TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start REAL NOT NULL DEFAULT 0,
    duration REAL NOT NULL DEFAULT 0,
    audio_path TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    parts TEXT NOT NULL DEFAULT '[]',              -- [[start, end, text], ...] relative to segment start
    status TEXT NOT NULL DEFAULT 'queued',         -- queued | done | error
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS segments_lecture ON segments(lecture_id, idx);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lecture_id TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS expansions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lecture_id TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quizzes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lecture_id TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    difficulty TEXT NOT NULL,
    questions TEXT NOT NULL,
    answers TEXT,
    score INTEGER
);
CREATE TABLE IF NOT EXISTS flashcards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lecture_id TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    front TEXT NOT NULL,
    back TEXT NOT NULL
);
"""

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")
_conn.execute("PRAGMA journal_mode = WAL")
_conn.executescript(SCHEMA)
_lock = threading.RLock()


@contextmanager
def tx():
    with _lock:
        _conn.execute("BEGIN")
        try:
            yield _conn
            _conn.execute("COMMIT")
        except BaseException:
            _conn.execute("ROLLBACK")
            raise


def q(sql: str, args=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def one(sql: str, args=()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def run(sql: str, args=()) -> int:
    with _lock:
        cur = _conn.execute(sql, args)
        return cur.lastrowid


# Lectures ------------------------------------------------------------------

def create_lecture(title: str, course: str = "", status: str = "recording") -> dict:
    lecture_id = uuid.uuid4().hex[:12]
    run(
        "INSERT INTO lectures (id, title, course, created_at, status) VALUES (?, ?, ?, ?, ?)",
        (lecture_id, title, course, time.time(), status),
    )
    return get_lecture(lecture_id)


def get_lecture(lecture_id: str) -> dict | None:
    lec = one("SELECT * FROM lectures WHERE id = ?", (lecture_id,))
    if lec:
        lec["topics"] = json.loads(lec["topics"] or "[]")
        counts = one(
            "SELECT COUNT(*) AS n, SUM(status = 'queued') AS pending, SUM(status = 'error') AS failed "
            "FROM segments WHERE lecture_id = ?",
            (lecture_id,),
        )
        lec["segment_count"] = counts["n"] or 0
        lec["pending_segments"] = counts["pending"] or 0
        lec["failed_segments"] = counts["failed"] or 0
    return lec


def list_lectures(search: str = "") -> list[dict]:
    if search:
        like = f"%{search}%"
        return q(
            "SELECT DISTINCT l.id, l.title, l.course, l.created_at, l.duration, l.status FROM lectures l "
            "LEFT JOIN segments s ON s.lecture_id = l.id "
            "WHERE l.title LIKE ? OR l.course LIKE ? OR l.notes LIKE ? OR s.text LIKE ? "
            "ORDER BY l.created_at DESC",
            (like, like, like, like),
        )
    return q("SELECT id, title, course, created_at, duration, status FROM lectures ORDER BY created_at DESC")


def update_lecture(lecture_id: str, **fields) -> None:
    allowed = {"title", "course", "duration", "status", "notes", "notes_status", "notes_error", "topics"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "topics" in fields and not isinstance(fields["topics"], str):
        fields["topics"] = json.dumps(fields["topics"])
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    run(f"UPDATE lectures SET {cols} WHERE id = ?", (*fields.values(), lecture_id))


def delete_lecture(lecture_id: str) -> None:
    run("DELETE FROM lectures WHERE id = ?", (lecture_id,))


# Segments ------------------------------------------------------------------

def add_segment(lecture_id: str, idx: int, start: float, duration: float, audio_path: str,
                text: str = "", status: str = "queued", parts: list | None = None) -> int:
    return run(
        "INSERT INTO segments (lecture_id, idx, start, duration, audio_path, text, parts, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (lecture_id, idx, start, duration, audio_path, text, json.dumps(parts or []), status),
    )


def get_segment(segment_id: int) -> dict | None:
    return one("SELECT * FROM segments WHERE id = ?", (segment_id,))


def segments(lecture_id: str) -> list[dict]:
    rows = q("SELECT * FROM segments WHERE lecture_id = ? ORDER BY idx, id", (lecture_id,))
    for r in rows:
        r["parts"] = json.loads(r["parts"] or "[]")
        r["has_audio"] = bool(r.pop("audio_path"))
    return rows


def next_segment_idx(lecture_id: str) -> int:
    row = one("SELECT MAX(idx) AS m FROM segments WHERE lecture_id = ?", (lecture_id,))
    return (row["m"] + 1) if row and row["m"] is not None else 0


def finish_segment(segment_id: int, text: str, parts: list, duration: float | None = None) -> None:
    if duration is not None:
        run("UPDATE segments SET text = ?, parts = ?, status = 'done', error = '', duration = ? WHERE id = ?",
            (text, json.dumps(parts), duration, segment_id))
    else:
        run("UPDATE segments SET text = ?, parts = ?, status = 'done', error = '' WHERE id = ?",
            (text, json.dumps(parts), segment_id))


def fail_segment(segment_id: int, error: str) -> None:
    run("UPDATE segments SET status = 'error', error = ? WHERE id = ?", (error[:500], segment_id))


def requeue_segment(segment_id: int) -> None:
    run("UPDATE segments SET status = 'queued', error = '' WHERE id = ?", (segment_id,))


def queued_segment_ids() -> list[int]:
    return [r["id"] for r in q("SELECT id FROM segments WHERE status = 'queued' ORDER BY id")]


def previous_text(lecture_id: str, idx: int, chars: int = 240) -> str:
    row = one(
        "SELECT text FROM segments WHERE lecture_id = ? AND idx < ? AND status = 'done' AND text != '' "
        "ORDER BY idx DESC LIMIT 1",
        (lecture_id, idx),
    )
    return row["text"][-chars:] if row else ""


def transcript_text(lecture_id: str, timestamps: bool = True) -> str:
    lines = []
    for seg in segments(lecture_id):
        if seg["status"] != "done":
            continue
        if seg["parts"]:
            for start, _end, text in seg["parts"]:
                lines.append(f"[{fmt_ts(seg['start'] + start)}] {text}" if timestamps else text)
        elif seg["text"]:
            lines.append(f"[{fmt_ts(seg['start'])}] {seg['text']}" if timestamps else seg["text"])
    return "\n".join(lines)


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# Chat ----------------------------------------------------------------------

def chat_history(lecture_id: str) -> list[dict]:
    return q("SELECT id, role, content, created_at FROM messages WHERE lecture_id = ? ORDER BY id", (lecture_id,))


def add_message(lecture_id: str, role: str, content: str) -> int:
    return run("INSERT INTO messages (lecture_id, role, content, created_at) VALUES (?, ?, ?, ?)",
               (lecture_id, role, content, time.time()))


def clear_chat(lecture_id: str) -> None:
    run("DELETE FROM messages WHERE lecture_id = ?", (lecture_id,))


# Expansions ----------------------------------------------------------------

def expansions(lecture_id: str) -> list[dict]:
    return q("SELECT * FROM expansions WHERE lecture_id = ? ORDER BY id DESC", (lecture_id,))


def add_expansion(lecture_id: str, topic: str, content: str) -> int:
    return run("INSERT INTO expansions (lecture_id, topic, content, created_at) VALUES (?, ?, ?, ?)",
               (lecture_id, topic, content, time.time()))


def delete_expansion(expansion_id: int) -> None:
    run("DELETE FROM expansions WHERE id = ?", (expansion_id,))


# Quizzes -------------------------------------------------------------------

def quizzes(lecture_id: str) -> list[dict]:
    rows = q("SELECT * FROM quizzes WHERE lecture_id = ? ORDER BY id DESC", (lecture_id,))
    for r in rows:
        r["questions"] = json.loads(r["questions"])
        r["answers"] = json.loads(r["answers"]) if r["answers"] else None
    return rows


def add_quiz(lecture_id: str, difficulty: str, questions: list) -> int:
    return run("INSERT INTO quizzes (lecture_id, created_at, difficulty, questions) VALUES (?, ?, ?, ?)",
               (lecture_id, time.time(), difficulty, json.dumps(questions)))


def get_quiz(quiz_id: int) -> dict | None:
    row = one("SELECT * FROM quizzes WHERE id = ?", (quiz_id,))
    if row:
        row["questions"] = json.loads(row["questions"])
        row["answers"] = json.loads(row["answers"]) if row["answers"] else None
    return row


def submit_quiz(quiz_id: int, answers: list, score: int) -> None:
    run("UPDATE quizzes SET answers = ?, score = ? WHERE id = ?", (json.dumps(answers), score, quiz_id))


def delete_quiz(quiz_id: int) -> None:
    run("DELETE FROM quizzes WHERE id = ?", (quiz_id,))


# Flashcards ----------------------------------------------------------------

def flashcards(lecture_id: str) -> list[dict]:
    return q("SELECT id, front, back FROM flashcards WHERE lecture_id = ? ORDER BY id", (lecture_id,))


def replace_flashcards(lecture_id: str, cards: list[dict]) -> None:
    with tx() as c:
        c.execute("DELETE FROM flashcards WHERE lecture_id = ?", (lecture_id,))
        c.executemany("INSERT INTO flashcards (lecture_id, front, back) VALUES (?, ?, ?)",
                      [(lecture_id, card["front"], card["back"]) for card in cards])


def recover_after_restart() -> None:
    """A crash or closed window can leave a lecture marked as recording."""
    run("UPDATE lectures SET status = 'processing' WHERE status = 'recording'")
    run("UPDATE lectures SET status = 'ready' WHERE status = 'processing' AND id NOT IN "
        "(SELECT lecture_id FROM segments WHERE status = 'queued')")
    run("UPDATE lectures SET notes_status = 'none' WHERE notes_status = 'generating' AND notes = ''")
    run("UPDATE lectures SET notes_status = 'done' WHERE notes_status = 'generating' AND notes != ''")
