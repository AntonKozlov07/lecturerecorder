"""End-to-end sync tests: two independent app installs syncing through a fake GitHub.

Each "device" runs in its own process with its own data folder, exactly like two
computers. Run with:  python tests/test_sync.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from fake_github import TOKEN, serve  # noqa: E402

PRELUDE = """
import json, time
from lecturerecorder import config, db, sync
config.save_settings({"github_repo": "me/lectures", "github_token": %r})
def do_sync():
    return sync.sync_once(sync.GitHub("me/lectures", %r))
def out(value):
    print("RESULT " + json.dumps(value, default=str))
""" % (TOKEN, TOKEN)


class Device:
    def __init__(self, name: str, api: str, tmp: Path):
        self.name = name
        self.env = {**os.environ, "LECTURERECORDER_HOME": str(tmp / name), "LECTURERECORDER_GITHUB_API": api,
                    "PYTHONPATH": str(ROOT)}

    def run(self, code: str):
        proc = subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(code)], env=self.env,
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f"{self.name} failed:\n{proc.stderr}")
        results = [line[7:] for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
        return json.loads(results[-1]) if results else None


def check(cond: bool, message: str) -> None:
    print(("ok   " if cond else "FAIL ") + message)
    if not cond:
        raise SystemExit(1)


def main() -> None:
    server, store = serve()
    api = f"http://127.0.0.1:{server.server_address[1]}"
    tmp = Path(tempfile.mkdtemp())
    pc, other = Device("pc", api, tmp), Device("other", api, tmp)
    pdf = tmp / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake scanned pdf bytes")

    # 1. First device creates a course with a file and a lecture, then syncs into an empty repo.
    pc.run(f"""
        course = db.ensure_course("PHYS 201")
        db.add_material(course["id"], "scan.pdf", "pdf_scan", {str(pdf)!r}, "", 1, {pdf.stat().st_size})
        lec = db.create_lecture("Week 1", "PHYS 201", status="ready")
        seg = db.add_segment(lec["id"], 0, 0, 30, "", status="queued")
        db.finish_segment(seg, "x", [[0, 4, "Entropy counts microstates."], [4, 8, "lol pizza later?"]])
        db.set_offtopic(seg, [1])
        db.update_lecture(lec["id"], notes="# Week 1 notes", notes_status="done", offtopic_status="done")
        db.add_message(("lecture", lec["id"]), "user", "What is entropy?")
        db.add_message(("lecture", lec["id"]), "assistant", "A count of microstates.")
        out(do_sync())
    """)
    files = store.files()
    check("lecture-recorder.json" in files, "empty repository is initialised")
    check(any(p.startswith("lectures/") for p in files), "lecture uploaded")
    check(any(p.startswith("files/") and p.endswith("scan.pdf") for p in files), "course file uploaded")
    lecture_path = next(p for p in files if p.startswith("lectures/"))
    doc = json.loads(files[lecture_path])
    check(doc["transcript"][1]["side_talk"] is True and doc["notes"] == "# Week 1 notes", "lecture document contents")

    # 2. Second device downloads everything.
    got = other.run("""
        r = do_sync()
        lec = db.list_lectures()[0]
        c = db.list_courses()[0]
        mats = db.materials(c["id"], with_text=True)
        out({"r": r, "title": lec["title"], "course": c["name"], "chat": len(db.chat_history(("lecture", lec["id"]))),
             "ai_text": db.transcript_text(lec["id"]), "mat": mats[0]["filename"],
             "file_ok": bool(mats[0]["path"]) and open(mats[0]["path"], "rb").read().startswith(b"%PDF")})
    """)
    check(got["r"]["downloaded"] == 2, "second device downloads lecture and course")
    check(got["title"] == "Week 1" and got["course"] == "PHYS 201" and got["chat"] == 2, "imported lecture matches")
    check("pizza" not in got["ai_text"], "side talk stays excluded from AI text after import")
    check(got["mat"] == "scan.pdf" and got["file_ok"], "course file downloaded")

    # 3. Nothing changed: a sync uploads nothing.
    r = other.run("out(do_sync())")
    check(r == {"uploaded": 0, "downloaded": 0, "merged": 0}, "idle sync does nothing")
    before = store.requests
    pc.run("out(do_sync())")
    check(store.requests - before < 12, "idle sync makes only a few API calls")

    # 4. Both devices change the same lecture before syncing: chat messages are combined.
    other.run("""
        lec = db.list_lectures()[0]
        db.add_message(("lecture", lec["id"]), "user", "Question from the other device")
        db.update_lecture(lec["id"], title="Week 1: Entropy")
        out(do_sync())
    """)
    merged = pc.run("""
        lec = db.list_lectures()[0]
        db.add_message(("lecture", lec["id"]), "user", "Question asked on the PC")
        r = do_sync()
        lec = db.get_lecture(lec["id"])
        out({"r": r, "title": lec["title"], "chat": [m["content"] for m in db.chat_history(("lecture", lec["id"]))]})
    """)
    check(merged["r"]["merged"] == 1, "conflicting edits are merged")
    check("Question from the other device" in merged["chat"] and "Question asked on the PC" in merged["chat"],
          "chat from both devices kept")
    back = other.run("""
        r = do_sync(); lec = db.list_lectures()[0]
        out({"r": r, "chat": len(db.chat_history(("lecture", lec["id"])))})
    """)
    check(back["chat"] == 4, "merged result reaches the other device")

    # 5. Same course name created on both devices before they ever synced: they become one course.
    pc.run('db.ensure_course("BIO 110"); out(do_sync())')
    third = Device("third", api, tmp)
    names = third.run("""
        db.ensure_course("BIO 110")
        do_sync()
        out(sorted(c["name"] for c in db.list_courses()))
    """)
    check(names.count("BIO 110") == 1 and "(2)" not in "".join(names), "same-named courses are joined, not duplicated")

    # 6. Deleting on one device deletes everywhere.
    pc.run("""
        lec = db.list_lectures()[0]
        db.delete_lecture(lec["id"])
        out(do_sync())
    """)
    left = other.run("do_sync(); out(len(db.list_lectures()))")
    check(left == 0, "deleted lecture removed on the other device")
    check(not any(p.startswith("lectures/") for p in store.files()), "deleted lecture removed from GitHub")

    # 7. A push rejected because the other device pushed first is retried automatically.
    store.reject_next_push = True
    status = pc.run("""
        db.create_lecture("Week 2", "", status="ready")
        out(sync.SyncService().run_now())
    """)
    check(not status["last_error"] and status["last_result"]["uploaded"] >= 1, "rejected push is retried")

    print("all sync scenarios passed")
    server.shutdown()


if __name__ == "__main__":
    main()
