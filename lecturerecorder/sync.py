"""Sync with the iPhone app through a private GitHub repository.

See docs/sync-format.md for the repository layout and the merge rules; the
iPhone app implements the same contract. In short: every lecture and course is
one JSON file, each device remembers what every file looked like at its last
sync, and only files that changed are downloaded, merged or uploaded. All
uploads from one sync go into a single commit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import db
from .config import AUDIO_DIR, MATERIALS_DIR, load_settings

log = logging.getLogger(__name__)

FORMAT = 1
MARKER = "lecture-recorder.json"
TOMBSTONES = "deleted.json"
MAX_FILE = 25 * 1024 * 1024
INTERVAL = 120                      # seconds between automatic syncs
API = os.environ.get("LECTURERECORDER_GITHUB_API", "https://api.github.com")


class SyncError(Exception):
    pass


# GitHub ----------------------------------------------------------------------

class GitHub:
    def __init__(self, repo: str, token: str):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
            raise SyncError('Enter the repository as "your-username/repo-name".')
        self.repo = repo
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None, ok404: bool = False):
        url = f"{API}/repos/{self.repo}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "LectureRecorder",
            **({"Content-Type": "application/json"} if data else {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            if ok404 and exc.code in (404, 409):
                return None
            if exc.code == 401:
                raise SyncError("GitHub rejected the token. Create a new one and paste it in Settings.") from exc
            if exc.code == 403 and "rate limit" in detail.lower():
                raise SyncError("GitHub's rate limit was reached. Sync will try again later.") from exc
            if exc.code in (403, 404):
                raise SyncError("Can't access that repository. Check its name, and that the token has "
                                "Contents: Read and write access to it.") from exc
            raise GitHubConflict(detail) if exc.code == 422 else SyncError(f"GitHub error {exc.code}: {detail}")
        except urllib.error.URLError as exc:
            raise SyncError("Can't reach GitHub. Check your internet connection.") from exc

    def blob(self, sha: str) -> bytes:
        data = self.call("GET", f"/git/blobs/{sha}")
        return base64.b64decode(data["content"])


class GitHubConflict(SyncError):
    pass


def git_blob_sha(data: bytes) -> str:
    """The SHA GitHub assigns to a file with this content."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def encode(doc: dict) -> bytes:
    return (json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode()


def doc_hash(doc: dict) -> str:
    body = {k: v for k, v in doc.items() if k != "modified_at"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# Export: database -> documents ------------------------------------------------

def _quiz_out(q: dict) -> dict:
    return {k: q[k] for k in ("created_at", "kind", "difficulty", "questions", "answers", "grading", "score")}


def lecture_ready(lec: dict) -> bool:
    return (lec["status"] == "ready" and not lec["pending_segments"] and lec["notes_status"] != "generating"
            and lec["offtopic_status"] != "running")


def export_lecture(lecture_id: str) -> dict | None:
    lec = db.get_lecture(lecture_id)
    if not lec or not lecture_ready(lec):
        return None
    scope = ("lecture", lecture_id)
    lines = []
    for seg in db.segments(lecture_id):
        if seg["status"] != "done":
            continue
        if seg["parts"]:
            for i, (s, e, text) in enumerate(seg["parts"]):
                lines.append({"t": round(seg["start"] + s, 2), "end": round(seg["start"] + e, 2), "text": text,
                              "side_talk": i in seg["offtopic"]})
        elif seg["text"]:
            lines.append({"t": round(seg["start"], 2), "end": round(seg["start"] + seg["duration"], 2),
                          "text": seg["text"], "side_talk": False})
    return {
        "format": FORMAT, "id": lec["id"], "title": lec["title"], "course": lec["course"],
        "created_at": lec["created_at"], "duration": lec["duration"],
        "transcript": lines,
        "side_talk_checked": lec["offtopic_status"] == "done",
        "notes": lec["notes"], "topics": lec["topics"],
        "deep_dives": [{"topic": x["topic"], "content": x["content"], "created_at": x["created_at"]}
                       for x in reversed(db.expansions(lecture_id))],
        "chat": [{"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
                 for m in db.chat_history(scope)],
        "quizzes": [_quiz_out(q) for q in reversed(db.quizzes(scope))],
        "flashcards": [{"front": c["front"], "back": c["back"], "source": c["source"]} for c in db.flashcards(scope)],
    }


def material_path(course_id: str, mat: dict) -> str | None:
    if mat["size"] > MAX_FILE or not mat.get("path") or not Path(mat["path"]).exists():
        return None
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", mat["filename"]).strip() or "file"
    return f"files/{course_id}/{mat['uid']}/{safe}"


def export_course(course_id: str) -> tuple[dict, dict[str, str]] | None:
    """The course document, and the original files to upload as {repo path: local path}."""
    course = db.get_course(course_id)
    if not course:
        return None
    scope = ("course", course_id)
    mats = db.materials(course_id, with_text=True)
    by_id = {str(m["id"]): m["uid"] for m in mats}
    excluded = []
    for key in course["excluded"]:
        kind, _, item = key.partition(":")
        excluded.append(f"material:{by_id[item]}" if kind == "material" and item in by_id else key)
    files, materials = {}, []
    for m in mats:
        path = material_path(course_id, m)
        if path:
            files[path] = m["path"]
        materials.append({"id": m["uid"], "filename": m["filename"], "kind": m["kind"], "pages": m["pages"],
                          "size": m["size"], "created_at": m["created_at"], "text": m["text"], "file": path})
    doc = {
        "format": FORMAT, "id": course_id, "name": course["name"], "created_at": course["created_at"],
        "excluded": sorted(excluded), "materials": materials,
        "chat": [{"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
                 for m in db.chat_history(scope)],
        "quizzes": [_quiz_out(q) for q in reversed(db.quizzes(scope))],
        "flashcards": [{"front": c["front"], "back": c["back"], "source": c["source"]} for c in db.flashcards(scope)],
    }
    return doc, files


# Merge ----------------------------------------------------------------------------

def _key(item: dict, extra: str = "") -> str:
    return f"{round(float(item.get('created_at', 0)), 3)}|{item.get(extra, '') if extra else ''}"


def _union(older: list, newer: list, extra: str = "", prefer=None) -> list:
    """Combine two lists of timestamped items. On a clash `prefer(older_item, newer_item)` decides,
    and without it the newer document's item wins."""
    out: dict[str, dict] = {}
    for item in older + newer:
        k = _key(item, extra)
        out[k] = prefer(out[k], item) if (k in out and prefer) else item
    return sorted(out.values(), key=lambda x: float(x.get("created_at", 0)))


def merge(local: dict, remote: dict) -> dict:
    """Combine two versions of the same lecture or course (see docs/sync-format.md)."""
    newer, older = (remote, local) if remote.get("modified_at", 0) >= local.get("modified_at", 0) else (local, remote)
    merged = dict(newer)

    def prefer_quiz(old: dict, new: dict) -> dict:
        if bool(old.get("answers")) != bool(new.get("answers")):
            return old if old.get("answers") else new  # an answered quiz beats an unanswered copy
        return new

    merged["chat"] = _union(older.get("chat", []), newer.get("chat", []), "role")
    merged["quizzes"] = _union(older.get("quizzes", []), newer.get("quizzes", []), "kind", prefer_quiz)
    if "deep_dives" in newer or "deep_dives" in older:
        merged["deep_dives"] = _union(older.get("deep_dives", []), newer.get("deep_dives", []), "topic")
    if "materials" in newer or "materials" in older:
        mats = {m["id"]: m for m in older.get("materials", [])}
        mats.update({m["id"]: m for m in newer.get("materials", [])})
        merged["materials"] = sorted(mats.values(), key=lambda m: m.get("created_at", 0))
    return merged


# Import: documents -> database --------------------------------------------------------

def _replace_scoped(scope: tuple[str, str], doc: dict) -> None:
    col = "lecture_id" if scope[0] == "lecture" else "course_id"
    with db.tx() as c:
        c.execute(f"DELETE FROM messages WHERE {col} = ?", (scope[1],))
        c.executemany(f"INSERT INTO messages ({col}, role, content, created_at) VALUES (?, ?, ?, ?)",
                      [(scope[1], m["role"], m["content"], m["created_at"]) for m in doc.get("chat", [])])
        c.execute(f"DELETE FROM quizzes WHERE {col} = ?", (scope[1],))
        c.executemany(
            f"INSERT INTO quizzes ({col}, created_at, kind, difficulty, questions, answers, grading, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(scope[1], q["created_at"], q.get("kind", "choice"), q.get("difficulty", "medium"),
              json.dumps(q.get("questions", [])),
              json.dumps(q["answers"]) if q.get("answers") is not None else None,
              json.dumps(q["grading"]) if q.get("grading") is not None else None,
              q.get("score")) for q in doc.get("quizzes", [])])
        c.execute(f"DELETE FROM flashcards WHERE {col} = ?", (scope[1],))
        c.executemany(f"INSERT INTO flashcards ({col}, front, back, source) VALUES (?, ?, ?, ?)",
                      [(scope[1], f["front"], f["back"], f.get("source", "")) for f in doc.get("flashcards", [])])


def import_lecture(doc: dict) -> None:
    lecture_id = doc["id"]
    existing = db.get_lecture(lecture_id)
    if existing and existing["status"] == "recording":
        return  # never overwrite a lecture that is being recorded right now
    db.ensure_course(doc.get("course", ""))
    fields = dict(title=doc.get("title") or "Untitled lecture", course=doc.get("course", ""),
                  duration=float(doc.get("duration") or 0), status="ready", notes=doc.get("notes", ""),
                  notes_status="done" if doc.get("notes", "").strip() else "none",
                  topics=doc.get("topics", []), offtopic_status="done" if doc.get("side_talk_checked") else "none")
    if not existing:
        db.run("INSERT INTO lectures (id, title, created_at) VALUES (?, ?, ?)",
               (lecture_id, fields["title"], float(doc.get("created_at") or time.time())))
    db.update_lecture(lecture_id, **fields)

    # Transcript: if the text is unchanged (the usual case) only update side-talk flags,
    # so a lecture recorded here keeps its audio. Otherwise store the lines as one chunk.
    lines = doc.get("transcript", [])
    local = []
    for seg in db.segments(lecture_id):
        if seg["status"] == "done":
            local += [(seg["id"], i, text) for i, (_s, _e, text) in enumerate(seg["parts"])]
    if local and [t for *_, t in local] == [line["text"] for line in lines]:
        flags: dict[int, list[int]] = {}
        for (seg_id, part, _), line in zip(local, lines):
            flags.setdefault(seg_id, [])
            if line.get("side_talk"):
                flags[seg_id].append(part)
        for seg_id, parts in flags.items():
            db.set_offtopic(seg_id, parts)
    else:
        db.run("DELETE FROM segments WHERE lecture_id = ?", (lecture_id,))
        if lines:
            parts = [[float(line["t"]), float(line.get("end", line["t"])), line["text"]] for line in lines]
            seg_id = db.add_segment(lecture_id, 0, 0, float(doc.get("duration") or 0), "", status="done",
                                    text=" ".join(p[2] for p in parts), parts=parts)
            db.set_offtopic(seg_id, [i for i, line in enumerate(lines) if line.get("side_talk")])

    with db.tx() as c:
        c.execute("DELETE FROM expansions WHERE lecture_id = ?", (lecture_id,))
        c.executemany("INSERT INTO expansions (lecture_id, topic, content, created_at) VALUES (?, ?, ?, ?)",
                      [(lecture_id, x["topic"], x["content"], x["created_at"]) for x in doc.get("deep_dives", [])])
    _replace_scoped(("lecture", lecture_id), doc)


def adopt_course_id(local_id: str, remote_id: str) -> None:
    """Give a never-synced local course the ID of the same-named course from the other device."""
    course = db.get_course(local_id)
    with db.tx() as c:
        c.execute("UPDATE courses SET name = name || ' (renaming)' WHERE id = ?", (local_id,))
        c.execute("INSERT INTO courses (id, name, created_at, excluded) VALUES (?, ?, ?, ?)",
                  (remote_id, course["name"], course["created_at"], json.dumps(course["excluded"])))
        for table in ("materials", "messages", "quizzes", "flashcards"):
            c.execute(f"UPDATE {table} SET course_id = ? WHERE course_id = ?", (remote_id, local_id))
        c.execute("DELETE FROM courses WHERE id = ?", (local_id,))


def import_course(doc: dict, fetch) -> None:
    """`fetch(repo_path)` returns a file's bytes from GitHub, or None if it isn't there."""
    course_id = doc["id"]
    name = (doc.get("name") or "Untitled course").strip()
    course = db.get_course(course_id)
    if not course:
        clash = db.one("SELECT id FROM courses WHERE name = ?", (name,))
        if clash and not db.one("SELECT path FROM sync_state WHERE path = ?", (f"courses/{clash['id']}.json",)):
            adopt_course_id(clash["id"], course_id)
        else:
            if clash:
                name = f"{name} (2)"
            db.run("INSERT INTO courses (id, name, created_at) VALUES (?, ?, ?)",
                   (course_id, name, float(doc.get("created_at") or time.time())))
        course = db.get_course(course_id)
    if course["name"] != name and not db.one("SELECT id FROM courses WHERE name = ? AND id != ?", (name, course_id)):
        db.rename_course(course_id, name)

    # Materials: add new ones (downloading their files), update text, drop removed ones.
    local = {m["uid"]: m for m in db.materials(course_id, with_text=True)}
    remote_ids = set()
    for m in doc.get("materials", []):
        remote_ids.add(m["id"])
        if m["id"] in local:
            continue
        path = ""
        if m.get("file"):
            data = fetch(m["file"])
            if data is not None:
                folder = MATERIALS_DIR / course_id
                folder.mkdir(parents=True, exist_ok=True)
                dest = folder / f"{m['id']}-{Path(m['file']).name}"
                dest.write_bytes(data)
                path = str(dest)
        db.add_material(course_id, m["filename"], m["kind"], path, m.get("text", ""), int(m.get("pages") or 0),
                        int(m.get("size") or 0), uid=m["id"], created_at=m.get("created_at"))
    for uid, m in local.items():
        if uid not in remote_ids:
            db.run("DELETE FROM materials WHERE id = ?", (m["id"],))
            if m["path"]:
                Path(m["path"]).unlink(missing_ok=True)

    by_uid = {m["uid"]: str(m["id"]) for m in db.materials(course_id)}
    excluded = []
    for key in doc.get("excluded", []):
        kind, _, item = key.partition(":")
        excluded.append(f"material:{by_uid[item]}" if kind == "material" and item in by_uid else key)
    db.set_course_excluded(course_id, excluded)
    _replace_scoped(("course", course_id), doc)


def apply_tombstones(tomb: dict) -> None:
    for lecture_id in tomb.get("lectures", {}):
        if db.get_lecture(lecture_id):
            db.run("DELETE FROM lectures WHERE id = ?", (lecture_id,))
            shutil.rmtree(AUDIO_DIR / lecture_id, ignore_errors=True)
    for course_id in tomb.get("courses", {}):
        course = db.get_course(course_id)
        if course:
            with db.tx() as c:
                c.execute("UPDATE lectures SET course = '' WHERE course = ?", (course["name"],))
                c.execute("DELETE FROM courses WHERE id = ?", (course_id,))
            shutil.rmtree(MATERIALS_DIR / course_id, ignore_errors=True)
    for uid in tomb.get("materials", {}):
        mat = db.one("SELECT id, path FROM materials WHERE uid = ?", (uid,))
        if mat:
            db.run("DELETE FROM materials WHERE id = ?", (mat["id"],))
            if mat["path"]:
                Path(mat["path"]).unlink(missing_ok=True)
    with db.tx() as c:
        for kind, items in tomb.items():
            for item_id, ts in items.items():
                c.execute("INSERT OR IGNORE INTO sync_tombstones (kind, id, deleted_at) VALUES (?, ?, ?)",
                          (kind, item_id, ts))


def local_tombstones() -> dict:
    out: dict[str, dict] = {"lectures": {}, "courses": {}, "materials": {}}
    for row in db.q("SELECT kind, id, deleted_at FROM sync_tombstones"):
        out.setdefault(row["kind"], {})[row["id"]] = row["deleted_at"]
    return out


# The sync run -----------------------------------------------------------------------

def _state(path: str) -> dict:
    return db.one("SELECT * FROM sync_state WHERE path = ?", (path,)) or {"remote_sha": "", "local_hash": "", "modified_at": 0}


def _save_state(path: str, remote_sha: str, local_hash: str, modified_at: float) -> None:
    db.run("INSERT OR REPLACE INTO sync_state (path, remote_sha, local_hash, modified_at) VALUES (?, ?, ?, ?)",
           (path, remote_sha, local_hash, modified_at))


def sync_once(gh: GitHub) -> dict:
    """One full sync. Returns counts of what happened."""
    repo = gh.call("GET", "")
    branch = repo["default_branch"]
    ref = gh.call("GET", f"/git/ref/heads/{branch}", ok404=True)
    if ref is None:
        # An empty repository: create the first commit with the Contents API.
        gh.call("PUT", f"/contents/{MARKER}", {"message": "Set up Lecture Recorder sync",
                                                "content": base64.b64encode(encode({"format": FORMAT})).decode()})
        ref = gh.call("GET", f"/git/ref/heads/{branch}")
    head = ref["object"]["sha"]
    commit = gh.call("GET", f"/git/commits/{head}")
    tree = gh.call("GET", f"/git/trees/{commit['tree']['sha']}?recursive=1")
    if tree.get("truncated"):
        raise SyncError("The sync repository has too many files for one listing.")
    remote = {e["path"]: e["sha"] for e in tree["tree"] if e["type"] == "blob"}
    allowed = (MARKER, TOMBSTONES, "README.md", ".gitignore", "LICENSE")
    foreign = [p for p in remote if not (p.startswith(("lectures/", "courses/", "files/")) or p in allowed)]
    if foreign and MARKER not in remote:
        raise SyncError("That repository already has other files in it. Use a new, empty private repository.")

    fetched: dict[str, bytes] = {}

    def fetch(path: str) -> bytes | None:
        if path not in remote:
            return None
        if path not in fetched:
            fetched[path] = gh.blob(remote[path])
        return fetched[path]

    # Tombstones first, so nothing deleted elsewhere gets uploaded again.
    tomb = local_tombstones()
    if TOMBSTONES in remote:
        theirs = json.loads(fetch(TOMBSTONES))
        apply_tombstones(theirs)
        tomb = local_tombstones()
    uploads: dict[str, bytes] = {}
    deletes: set[str] = set()
    tomb_doc = {k: dict(sorted(v.items())) for k, v in sorted(tomb.items())}
    if git_blob_sha(encode(tomb_doc)) != remote.get(TOMBSTONES) and any(tomb_doc.values()):
        uploads[TOMBSTONES] = encode(tomb_doc)
    for kind, items in tomb.items():
        for item_id in items:
            if kind in ("lectures", "courses") and f"{kind}/{item_id}.json" in remote:
                deletes.add(f"{kind}/{item_id}.json")
    for path in remote:
        parts = path.split("/")
        if path.startswith("files/") and len(parts) >= 3 and (parts[1] in tomb["courses"] or parts[2] in tomb["materials"]):
            deletes.add(path)

    if MARKER not in remote:
        uploads[MARKER] = encode({"format": FORMAT})
    stats = {"uploaded": 0, "downloaded": 0, "merged": 0}
    new_states: list[tuple[str, str, str, float]] = []

    def reconcile(path: str, local_doc: dict | None, importer) -> None:
        st = _state(path)
        local_hash = doc_hash(local_doc) if local_doc else ""
        local_changed = bool(local_doc) and local_hash != st["local_hash"]
        remote_sha = remote.get(path, "")
        remote_changed = bool(remote_sha) and remote_sha != st["remote_sha"]
        if local_doc and not remote_sha:
            local_changed = True  # never uploaded, or removed from GitHub by hand
        if not local_changed and not remote_changed:
            return
        if local_changed and not remote_changed:
            doc = {**local_doc, "modified_at": time.time()}
            uploads[path] = encode(doc)
            new_states.append((path, git_blob_sha(uploads[path]), local_hash, doc["modified_at"]))
            stats["uploaded"] += 1
            return
        theirs = json.loads(fetch(path))
        if theirs.get("format", 1) > FORMAT:
            raise SyncError("The iPhone app is newer than this app. Update Lecture Recorder on this computer.")
        if local_changed:
            mine = {**local_doc, "modified_at": time.time()}
            doc = merge(mine, theirs)
            importer(doc)
            uploads[path] = encode(doc)
            stats["merged"] += 1
            new_states.append((path, git_blob_sha(uploads[path]), "", doc.get("modified_at", 0)))
        else:
            importer(theirs)
            stats["downloaded"] += 1
            new_states.append((path, remote_sha, "", theirs.get("modified_at", 0)))

    # Courses before lectures, so imported lectures find their course.
    local_courses = {c["id"] for c in db.list_courses()}
    remote_courses = {p[8:-5] for p in remote if p.startswith("courses/") and p.endswith(".json")}
    for course_id in sorted((local_courses | remote_courses) - set(tomb["courses"])):
        exported = export_course(course_id) if course_id in local_courses else None
        doc, files = exported if exported else (None, {})
        reconcile(f"courses/{course_id}.json", doc, lambda d: import_course(d, fetch))
        for repo_path, local_path in files.items():
            if repo_path not in remote:  # a material's file never changes, so presence is enough
                uploads[repo_path] = Path(local_path).read_bytes()

    local_lectures = {l["id"] for l in db.list_lectures()}
    remote_lectures = {p[9:-5] for p in remote if p.startswith("lectures/") and p.endswith(".json")}
    for lecture_id in sorted((local_lectures | remote_lectures) - set(tomb["lectures"])):
        doc = export_lecture(lecture_id) if lecture_id in local_lectures else None
        if lecture_id in local_lectures and doc is None:
            continue  # still recording or transcribing: sync it later
        reconcile(f"lectures/{lecture_id}.json", doc, import_lecture)

    if uploads or deletes:
        entries = []
        for path, data in uploads.items():
            blob = gh.call("POST", "/git/blobs", {"content": base64.b64encode(data).decode(), "encoding": "base64"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        entries += [{"path": p, "mode": "100644", "type": "blob", "sha": None} for p in deletes if p not in uploads]
        new_tree = gh.call("POST", "/git/trees", {"base_tree": commit["tree"]["sha"], "tree": entries})
        new_commit = gh.call("POST", "/git/commits", {
            "message": f"Sync from {platform.node() or 'PC'}",
            "tree": new_tree["sha"], "parents": [head]})
        gh.call("PATCH", f"/git/refs/heads/{branch}", {"sha": new_commit["sha"], "force": False})

    # Record what every synced document looks like now, as exported from this device.
    for path, remote_sha, _, modified_at in new_states:
        kind, item_id = path.split("/")[0], path.split("/")[1][:-5]
        exported = export_course(item_id) if kind == "courses" else export_lecture(item_id)
        doc = exported[0] if (kind == "courses" and exported) else exported
        _save_state(path, remote_sha, doc_hash(doc) if doc else "", modified_at)
    for path in deletes:
        db.run("DELETE FROM sync_state WHERE path = ?", (path,))
    return stats


# Background service ------------------------------------------------------------------

class SyncService:
    def __init__(self) -> None:
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self.state = {"running": False, "last_sync": None, "last_error": "", "last_result": None}
        threading.Thread(target=self._loop, name="sync", daemon=True).start()

    @staticmethod
    def configured() -> bool:
        s = load_settings()
        return bool(s["github_repo"] and s["github_token"])

    def trigger(self) -> None:
        self._wake.set()

    def status(self) -> dict:
        return {**self.state, "configured": self.configured(), "repo": load_settings()["github_repo"]}

    def run_now(self) -> dict:
        with self._lock:
            s = load_settings()
            self.state["running"] = True
            try:
                gh = GitHub(s["github_repo"], s["github_token"])
                for attempt in range(3):
                    try:
                        result = sync_once(gh)
                        break
                    except GitHubConflict:
                        if attempt == 2:
                            raise
                        time.sleep(1)  # the iPhone pushed at the same moment: start over
                self.state.update(last_sync=time.time(), last_error="", last_result=result)
            except SyncError as exc:
                self.state["last_error"] = str(exc)
            except Exception as exc:
                log.exception("Sync failed")
                self.state["last_error"] = f"Sync failed: {exc}"
            finally:
                self.state["running"] = False
            return self.status()

    def _loop(self) -> None:
        time.sleep(5)
        while True:
            if self.configured():
                self.run_now()
            self._wake.wait(INTERVAL)
            self._wake.clear()


service: SyncService | None = None


def start() -> SyncService:
    global service
    if service is None:
        service = SyncService()
    return service


def new_id() -> str:
    return uuid.uuid4().hex[:12]
