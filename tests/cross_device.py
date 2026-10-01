"""Cross-device sync check used by the iOS CI job.

  python tests/cross_device.py seed    PC data goes up to the fake GitHub
  python tests/cross_device.py verify  a fresh PC install sees what the iPhone tests added

The fake GitHub (tests/fake_github.py) must be running on LECTURERECORDER_GITHUB_API.
"""

import os
import sys
import tempfile

os.environ.setdefault("LECTURERECORDER_GITHUB_API", "http://127.0.0.1:8790")
os.environ["LECTURERECORDER_HOME"] = tempfile.mkdtemp()

from lecturerecorder import config, db, sync  # noqa: E402

TOKEN = "test-token"
config.save_settings({"github_repo": "me/lectures", "github_token": TOKEN})
gh = sync.GitHub("me/lectures", TOKEN)

if sys.argv[1] == "seed":
    pdf = os.path.join(os.environ["LECTURERECORDER_HOME"], "scan.pdf")
    with open(pdf, "wb") as f:
        f.write(b"%PDF-1.4 fake scan")
    course = db.ensure_course("PHYS 201")
    db.add_material(course["id"], "scan.pdf", "pdf_scan", pdf, "", 1, os.path.getsize(pdf))
    lec = db.create_lecture("Week 1", "PHYS 201", status="ready")
    seg = db.add_segment(lec["id"], 0, 0, 30, "", status="queued")
    db.finish_segment(seg, "x", [[0, 4, "Entropy counts microstates."], [4, 8, "lol pizza later?"]])
    db.set_offtopic(seg, [1])
    db.update_lecture(lec["id"], notes="# Week 1", notes_status="done", offtopic_status="done")
    db.add_message(("lecture", lec["id"]), "user", "What is entropy?")
    print("seeded:", sync.sync_once(gh))
else:
    print("pulled:", sync.sync_once(gh))
    titles = sorted(l["title"] for l in db.list_lectures())
    week1 = next(l for l in db.list_lectures() if l["title"] == "Week 1")
    chat = [m["content"] for m in db.chat_history(("lecture", week1["id"]))]
    print("lectures:", titles, "chat:", chat)
    assert "Recorded on the iPhone" in titles, "lecture recorded on the iPhone did not reach the PC"
    assert "Asked on the iPhone" in chat, "chat message from the iPhone did not reach the PC"
    phone = next(l for l in db.list_lectures() if l["title"] == "Recorded on the iPhone")
    assert "Hello from the phone." in db.transcript_text(phone["id"])
    print("cross-device sync OK")
