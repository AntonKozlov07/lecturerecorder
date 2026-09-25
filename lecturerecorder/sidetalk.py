"""Live side-talk detection.

As chunks of a recording are transcribed, new transcript lines are sent in
small batches to Claude Haiku, which flags lines that are not lecture content
(students joking with each other, personal conversations, phone calls). Those
lines stay visible in the transcript but are left out of everything sent to
the AI afterwards, so detection usually pays for itself.

While recording, a lecture is checked at most once a minute, so each call
covers a couple of chunks. When a recording finishes, whatever is left is
checked straight away, before notes are written.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import ai, db
from .config import load_settings, public_settings

log = logging.getLogger(__name__)

LIVE_INTERVAL = 60      # seconds between checks of a lecture that is still being recorded
BATCH_LINES = 150       # most lines sent in one request
CONTEXT_LINES = 6       # earlier lines included so a new batch is judged in context


def enabled() -> bool:
    return bool(load_settings()["filter_side_talk"]) and public_settings()["has_api_key"]


class SideTalk:
    def __init__(self) -> None:
        self._lock = threading.Condition()
        self._pending: dict[str, bool] = {}        # lecture id -> finished (check everything now)
        self._last: dict[str, float] = {}
        self._callbacks: dict[str, Callable[[], None]] = {}
        threading.Thread(target=self._run, name="sidetalk", daemon=True).start()

    def request(self, lecture_id: str, final: bool = False, then: Callable[[], None] | None = None) -> None:
        """Queue a check. `final` checks right away; `then` runs once the lecture is fully checked."""
        with self._lock:
            self._pending[lecture_id] = self._pending.get(lecture_id, False) or final
            if then:
                self._callbacks[lecture_id] = then
            self._lock.notify()

    def _due(self) -> str | None:
        now = time.time()
        for lecture_id, final in self._pending.items():
            if final or now - self._last.get(lecture_id, 0) >= LIVE_INTERVAL:
                return lecture_id
        return None

    def _run(self) -> None:
        while True:
            with self._lock:
                lecture_id = self._due()
                while lecture_id is None:
                    self._lock.wait(timeout=5)
                    lecture_id = self._due()
                final = self._pending.pop(lecture_id)
                callback = self._callbacks.pop(lecture_id, None) if final else None
                self._last[lecture_id] = time.time()
            try:
                self._check(lecture_id, final)
            except Exception:
                log.exception("Side-talk detection failed for %s", lecture_id)
                db.update_lecture(lecture_id, offtopic_status="error")
            if callback:
                try:
                    callback()
                except Exception:
                    log.exception("Callback after side-talk detection failed")

    def _check(self, lecture_id: str, final: bool) -> None:
        lecture = db.get_lecture(lecture_id)
        if not lecture or not enabled():
            return
        segments = [s for s in db.segments(lecture_id) if s["status"] == "done"]
        unchecked = [s for s in segments if not s["offtopic_checked"] and s["parts"]]
        if not unchecked:
            if final and lecture["offtopic_status"] == "running":
                db.update_lecture(lecture_id, offtopic_status="done")
            return
        db.update_lecture(lecture_id, offtopic_status="running")
        flagged: dict[int, set[int]] = {s["id"]: set() for s in unchecked}
        all_lines = db.transcript_lines(lecture_id)
        # Only chunks seen above: new ones may have arrived since and are picked up next time.
        todo = [(i, seg, part, text) for i, (seg, part, text) in enumerate(all_lines) if seg in flagged]
        if not todo:
            return
        first = todo[0][0]
        context = [text for _, _, text in all_lines[max(0, first - CONTEXT_LINES):first]]
        for start in range(0, len(todo), BATCH_LINES):
            batch = todo[start:start + BATCH_LINES]
            hits = ai.detect_side_talk(lecture["title"], context, [text for _, _, _, text in batch])
            for n in hits:
                _, seg, part, _ = batch[n]
                flagged[seg].add(part)
            context = [text for _, _, _, text in batch[-CONTEXT_LINES:]]
        for seg_id, parts in flagged.items():
            db.set_offtopic(seg_id, list(parts))
        still_recording = lecture["status"] == "recording"
        db.update_lecture(lecture_id, offtopic_status="running" if still_recording and not final else "done")


detector = SideTalk()
