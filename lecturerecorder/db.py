"""SQLite storage: lectures, transcripts, courses, course materials and study data."""

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
    offtopic_status TEXT NOT NULL DEFAULT 'none',  -- none | running | done | error: side-talk detection
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
    offtopic TEXT NOT NULL DEFAULT '[]',           -- indexes into parts judged not to be lecture content
    offtopic_checked INTEGER NOT NULL DEFAULT 0,   -- 1 once side-talk detection has looked at this chunk
    status TEXT NOT NULL DEFAULT 'queued',         -- queued | done | error
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS segments_lecture ON segments(lecture_id, idx);
CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    excluded TEXT NOT NULL DEFAULT '[]'            -- source keys left out of course-wide study tools
);
CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL,                            -- pdf | pdf_scan | slides | document | text | image
    path TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    pages INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    token_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
);
-- Study tables belong to either one lecture or one course.
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lecture_id TEXT REFERENCES lectures(id) ON DELETE CASCADE,
    course_id TEXT REFERENCES courses(id) ON DELETE CASCADE,
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
    lecture_id TEXT REFERENCES lectures(id) ON DELETE CASCADE,
    course_id TEXT REFERENCES courses(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'choice',           -- choice | written
    difficulty TEXT NOT NULL,
    questions TEXT NOT NULL,
    answers TEXT,
    grading TEXT,
    score REAL
);
CREATE TABLE IF NOT EXISTS flashcards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lecture_id TEXT REFERENCES lectures(id) ON DELETE CASCADE,
    course_id TEXT REFERENCES courses(id) ON DELETE CASCADE,
    front TEXT NOT NULL,
    back TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT ''
);
"""

_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")
_conn.execute("PRAGMA journal_mode = WAL")


def _migrate() -> None:
    version = _conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        _migrate_v1()
    if version < 2:
        # Side-talk detection columns.
        for table, column, decl in (("lectures", "offtopic_status", "TEXT NOT NULL DEFAULT 'none'"),
                                    ("segments", "offtopic", "TEXT NOT NULL DEFAULT '[]'"),
                                    ("segments", "offtopic_checked", "INTEGER NOT NULL DEFAULT 0")):
            existing = {r[1] for r in _conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        _conn.execute("PRAGMA user_version = 2")


def _migrate_v1() -> None:
    old_tables = {r[0] for r in _conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "messages" in old_tables:
        # Rebuild the study tables so rows can belong to a course instead of a lecture.
        _conn.execute("PRAGMA foreign_keys = OFF")
        _conn.executescript(
            "BEGIN;"
            "ALTER TABLE messages RENAME TO messages_v0;"
            "ALTER TABLE quizzes RENAME TO quizzes_v0;"
            "ALTER TABLE flashcards RENAME TO flashcards_v0;"
            + SCHEMA +
            """
            INSERT INTO messages (id, lecture_id, role, content, created_at)
                SELECT id, lecture_id, role, content, created_at FROM messages_v0;
            INSERT INTO quizzes (id, lecture_id, created_at, difficulty, questions, answers, score)
                SELECT id, lecture_id, created_at, difficulty, questions, answers, score FROM quizzes_v0;
            INSERT INTO flashcards (id, lecture_id, front, back)
                SELECT id, lecture_id, front, back FROM flashcards_v0;
            DROP TABLE messages_v0; DROP TABLE quizzes_v0; DROP TABLE flashcards_v0;
            COMMIT;
            """
        )
        _conn.execute("PRAGMA foreign_keys = ON")
    else:
        _conn.executescript(SCHEMA)
    for (name,) in _conn.execute("SELECT DISTINCT course FROM lectures WHERE course != ''").fetchall():
        _conn.execute("INSERT OR IGNORE INTO courses (id, name, created_at) VALUES (?, ?, ?)",
                      (uuid.uuid4().hex[:12], name, time.time()))
    _conn.execute("PRAGMA user_version = 1")


_migrate()
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
    ensure_course(course)
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
    allowed = {"title", "course", "duration", "status", "notes", "notes_status", "notes_error", "topics",
               "offtopic_status"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if fields.get("course"):
        ensure_course(fields["course"])
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
        r["offtopic"] = json.loads(r["offtopic"] or "[]")
        r["has_audio"] = bool(r.pop("audio_path"))
    return rows


def next_segment_idx(lecture_id: str) -> int:
    row = one("SELECT MAX(idx) AS m FROM segments WHERE lecture_id = ?", (lecture_id,))
    return (row["m"] + 1) if row and row["m"] is not None else 0


def finish_segment(segment_id: int, text: str, parts: list, duration: float | None = None) -> None:
    if duration is not None:
        run("UPDATE segments SET text = ?, parts = ?, offtopic = '[]', offtopic_checked = 0, status = 'done', "
            "error = '', duration = ? WHERE id = ?", (text, json.dumps(parts), duration, segment_id))
    else:
        run("UPDATE segments SET text = ?, parts = ?, offtopic = '[]', offtopic_checked = 0, status = 'done', "
            "error = '' WHERE id = ?", (text, json.dumps(parts), segment_id))


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


def transcript_text(lecture_id: str, timestamps: bool = True, include_offtopic: bool = False) -> str:
    """The transcript as text. Lines marked as side talk are left out unless asked for."""
    lines = []
    for seg in segments(lecture_id):
        if seg["status"] != "done":
            continue
        if seg["parts"]:
            skip = set() if include_offtopic else set(seg["offtopic"])
            for i, (start, _end, text) in enumerate(seg["parts"]):
                if i not in skip:
                    lines.append(f"[{fmt_ts(seg['start'] + start)}] {text}" if timestamps else text)
        elif seg["text"]:
            lines.append(f"[{fmt_ts(seg['start'])}] {seg['text']}" if timestamps else seg["text"])
    return "\n".join(lines)


def transcript_lines(lecture_id: str) -> list[tuple[int, int, str]]:
    """Every timed transcript line as (segment id, part index, "[mm:ss] text")."""
    out = []
    for seg in segments(lecture_id):
        if seg["status"] == "done":
            for i, (start, _end, text) in enumerate(seg["parts"]):
                out.append((seg["id"], i, f"[{fmt_ts(seg['start'] + start)}] {text}"))
    return out


def set_offtopic(segment_id: int, part_indexes: list[int], checked: bool = True) -> None:
    run("UPDATE segments SET offtopic = ?, offtopic_checked = ? WHERE id = ?",
        (json.dumps(sorted(set(part_indexes))), int(checked), segment_id))


def reset_offtopic_checks(lecture_id: str) -> None:
    run("UPDATE segments SET offtopic_checked = 0 WHERE lecture_id = ?", (lecture_id,))


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# Scopes: study data belongs to ("lecture", id) or ("course", id) --------------

def _col(scope: tuple[str, str]) -> str:
    return {"lecture": "lecture_id", "course": "course_id"}[scope[0]]


# Chat ----------------------------------------------------------------------

def chat_history(scope: tuple[str, str]) -> list[dict]:
    return q(f"SELECT id, role, content, created_at FROM messages WHERE {_col(scope)} = ? ORDER BY id", (scope[1],))


def add_message(scope: tuple[str, str], role: str, content: str) -> int:
    return run(f"INSERT INTO messages ({_col(scope)}, role, content, created_at) VALUES (?, ?, ?, ?)",
               (scope[1], role, content, time.time()))


def clear_chat(scope: tuple[str, str]) -> None:
    run(f"DELETE FROM messages WHERE {_col(scope)} = ?", (scope[1],))


# Expansions ----------------------------------------------------------------

def expansions(lecture_id: str) -> list[dict]:
    return q("SELECT * FROM expansions WHERE lecture_id = ? ORDER BY id DESC", (lecture_id,))


def add_expansion(lecture_id: str, topic: str, content: str) -> int:
    return run("INSERT INTO expansions (lecture_id, topic, content, created_at) VALUES (?, ?, ?, ?)",
               (lecture_id, topic, content, time.time()))


def delete_expansion(expansion_id: int) -> None:
    run("DELETE FROM expansions WHERE id = ?", (expansion_id,))


# Quizzes -------------------------------------------------------------------

def _quiz_row(r: dict) -> dict:
    for key in ("questions", "answers", "grading"):
        r[key] = json.loads(r[key]) if r[key] else None
    return r


def quizzes(scope: tuple[str, str]) -> list[dict]:
    return [_quiz_row(r) for r in q(f"SELECT * FROM quizzes WHERE {_col(scope)} = ? ORDER BY id DESC", (scope[1],))]


def add_quiz(scope: tuple[str, str], kind: str, difficulty: str, questions: list) -> int:
    return run(f"INSERT INTO quizzes ({_col(scope)}, created_at, kind, difficulty, questions) VALUES (?, ?, ?, ?, ?)",
               (scope[1], time.time(), kind, difficulty, json.dumps(questions)))


def get_quiz(quiz_id: int) -> dict | None:
    row = one("SELECT * FROM quizzes WHERE id = ?", (quiz_id,))
    return _quiz_row(row) if row else None


def submit_quiz(quiz_id: int, answers: list, score: float, grading: list | None = None) -> None:
    run("UPDATE quizzes SET answers = ?, score = ?, grading = ? WHERE id = ?",
        (json.dumps(answers), score, json.dumps(grading) if grading is not None else None, quiz_id))


def delete_quiz(quiz_id: int) -> None:
    run("DELETE FROM quizzes WHERE id = ?", (quiz_id,))


# Flashcards ----------------------------------------------------------------

def flashcards(scope: tuple[str, str]) -> list[dict]:
    return q(f"SELECT id, front, back, source FROM flashcards WHERE {_col(scope)} = ? ORDER BY id", (scope[1],))


def replace_flashcards(scope: tuple[str, str], cards: list[dict]) -> None:
    col = _col(scope)
    with tx() as c:
        c.execute(f"DELETE FROM flashcards WHERE {col} = ?", (scope[1],))
        c.executemany(f"INSERT INTO flashcards ({col}, front, back, source) VALUES (?, ?, ?, ?)",
                      [(scope[1], card["front"], card["back"], card.get("source", "")) for card in cards])


# Courses -------------------------------------------------------------------

def ensure_course(name: str) -> dict | None:
    name = name.strip()
    if not name:
        return None
    run("INSERT OR IGNORE INTO courses (id, name, created_at) VALUES (?, ?, ?)",
        (uuid.uuid4().hex[:12], name, time.time()))
    return one("SELECT * FROM courses WHERE name = ?", (name,))


def _course_row(c: dict | None) -> dict | None:
    if c:
        c["excluded"] = json.loads(c["excluded"] or "[]")
    return c


def get_course(course_id: str) -> dict | None:
    return _course_row(one("SELECT * FROM courses WHERE id = ?", (course_id,)))


def list_courses() -> list[dict]:
    return q(
        "SELECT c.id, c.name, "
        "(SELECT COUNT(*) FROM lectures l WHERE l.course = c.name) AS lecture_count, "
        "(SELECT COUNT(*) FROM materials m WHERE m.course_id = c.id) AS material_count "
        "FROM courses c ORDER BY c.name COLLATE NOCASE"
    )


def rename_course(course_id: str, name: str) -> None:
    course = get_course(course_id)
    with tx() as c:
        c.execute("UPDATE lectures SET course = ? WHERE course = ?", (name, course["name"]))
        c.execute("UPDATE courses SET name = ? WHERE id = ?", (name, course_id))


def set_course_excluded(course_id: str, excluded: list[str]) -> None:
    run("UPDATE courses SET excluded = ? WHERE id = ?", (json.dumps(sorted(set(excluded))), course_id))


def delete_course(course_id: str) -> None:
    course = get_course(course_id)
    with tx() as c:
        c.execute("UPDATE lectures SET course = '' WHERE course = ?", (course["name"],))
        c.execute("DELETE FROM courses WHERE id = ?", (course_id,))


def course_lectures(course_name: str) -> list[dict]:
    return q("SELECT id, title, created_at, duration, status FROM lectures WHERE course = ? ORDER BY created_at",
             (course_name,))


# Materials -----------------------------------------------------------------

def add_material(course_id: str, filename: str, kind: str, path: str, text: str, pages: int, size: int) -> int:
    return run(
        "INSERT INTO materials (course_id, filename, kind, path, text, pages, size, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (course_id, filename, kind, path, text, pages, size, time.time()),
    )


def materials(course_id: str, with_text: bool = False) -> list[dict]:
    cols = "*" if with_text else "id, course_id, filename, kind, pages, size, created_at, LENGTH(text) AS chars"
    return q(f"SELECT {cols} FROM materials WHERE course_id = ? ORDER BY created_at", (course_id,))


def get_material(material_id: int) -> dict | None:
    return one("SELECT * FROM materials WHERE id = ?", (material_id,))


def delete_material(material_id: int) -> None:
    run("DELETE FROM materials WHERE id = ?", (material_id,))


# Paired phones -------------------------------------------------------------

def add_device(token_hash: str, name: str) -> None:
    now = time.time()
    run("INSERT OR REPLACE INTO devices (token_hash, name, created_at, last_seen) VALUES (?, ?, ?, ?)",
        (token_hash, name, now, now))


def device(token_hash: str) -> dict | None:
    return one("SELECT * FROM devices WHERE token_hash = ?", (token_hash,))


def touch_device(token_hash: str) -> None:
    run("UPDATE devices SET last_seen = ? WHERE token_hash = ?", (time.time(), token_hash))


def list_devices() -> list[dict]:
    return q("SELECT token_hash, name, created_at, last_seen FROM devices ORDER BY created_at")


def delete_device(token_hash: str) -> None:
    run("DELETE FROM devices WHERE token_hash = ?", (token_hash,))


def recover_after_restart() -> None:
    """A crash or closed window can leave a lecture marked as recording."""
    run("UPDATE lectures SET status = 'processing' WHERE status = 'recording'")
    run("UPDATE lectures SET status = 'ready' WHERE status = 'processing' AND id NOT IN "
        "(SELECT lecture_id FROM segments WHERE status = 'queued')")
    run("UPDATE lectures SET notes_status = 'none' WHERE notes_status = 'generating' AND notes = ''")
    run("UPDATE lectures SET notes_status = 'done' WHERE notes_status = 'generating' AND notes != ''")
