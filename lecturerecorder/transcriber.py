"""Background speech-to-text using faster-whisper, running fully on this PC.

Audio arrives as short segments while recording (or one long file on import).
A single worker thread transcribes them in order so the transcript fills in
live without blocking the UI.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

from . import db
from .config import load_settings

log = logging.getLogger(__name__)


class Transcriber:
    def __init__(self) -> None:
        self._queue: queue.Queue[int] = queue.Queue()
        self._model = None
        self._model_key: tuple | None = None
        self._device = ""
        self._gpu_failed = False  # set when "auto" picked the GPU but its libraries are missing
        self.state = "idle"           # idle | loading | transcribing | error
        self.detail = ""
        self.on_lecture_complete: Callable[[str], None] | None = None
        threading.Thread(target=self._run, name="transcriber", daemon=True).start()

    def enqueue(self, segment_id: int) -> None:
        self._queue.put(segment_id)

    def requeue_pending(self) -> None:
        for seg_id in db.queued_segment_ids():
            self.enqueue(seg_id)

    def status(self) -> dict:
        return {"state": self.state, "detail": self.detail, "queued": self._queue.qsize()}

    # ------------------------------------------------------------------

    def _load_model(self):
        settings = load_settings()
        device = settings["whisper_device"]
        if device == "auto" and self._gpu_failed:
            device = "cpu"
        elif device == "auto":
            try:
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"
        self._device = device
        compute_type = "float16" if device == "cuda" else "int8"
        key = (settings["whisper_model"], device, compute_type)
        if self._model is not None and self._model_key == key:
            return self._model

        from faster_whisper import WhisperModel

        self.state = "loading"
        self.detail = (f"Loading speech model '{key[0]}' on {device}. "
                       "The first run downloads it, which can take a few minutes.")
        log.info(self.detail)
        self._model = WhisperModel(key[0], device=device, compute_type=compute_type)
        self._model_key = key
        return self._model

    def _run(self) -> None:
        while True:
            seg_id = self._queue.get()
            seg = db.get_segment(seg_id)
            if not seg or seg["status"] != "queued":
                continue
            try:
                try:
                    model = self._load_model()
                    self.state = "transcribing"
                    self.detail = ""
                    self._transcribe(model, seg)
                except Exception:
                    # An NVIDIA card without the CUDA libraries installed fails here;
                    # when the device was picked automatically, fall back to the CPU once.
                    if self._device != "cuda" or load_settings()["whisper_device"] != "auto":
                        raise
                    log.warning("GPU transcription failed, switching to CPU", exc_info=True)
                    self._gpu_failed = True
                    self._model = None
                    self._transcribe(self._load_model(), seg)
            except Exception as exc:  # keep the worker alive whatever happens
                log.exception("Transcription failed for segment %s", seg_id)
                db.fail_segment(seg_id, f"{type(exc).__name__}: {exc}")
                self.state = "error"
                self.detail = f"Transcription failed: {exc}"
            else:
                if self._queue.empty():
                    self.state = "idle"
                    self.detail = ""
            self._check_complete(seg["lecture_id"])

    def _transcribe(self, model, seg: dict) -> None:
        settings = load_settings()
        prompt = db.previous_text(seg["lecture_id"], seg["idx"]) or None
        pieces, info = model.transcribe(
            seg["audio_path"],
            language=settings["language"] or None,
            initial_prompt=prompt,
            vad_filter=True,
            beam_size=5,
        )
        parts = []
        for p in pieces:
            text = p.text.strip()
            if text:
                parts.append([round(p.start, 2), round(p.end, 2), text])
        text = " ".join(p[2] for p in parts)
        # Imported files report their real length only once decoded.
        duration = info.duration if seg["duration"] <= 0 else None
        db.finish_segment(seg["id"], text, parts, duration)
        if duration is not None:
            lec = db.get_lecture(seg["lecture_id"])
            if lec:
                db.update_lecture(lec["id"], duration=max(lec["duration"], seg["start"] + duration))

    def _check_complete(self, lecture_id: str) -> None:
        lec = db.get_lecture(lecture_id)
        if not lec or lec["status"] != "processing" or lec["pending_segments"] > 0:
            return
        db.update_lecture(lecture_id, status="ready")
        if self.on_lecture_complete:
            try:
                self.on_lecture_complete(lecture_id)
            except Exception:
                log.exception("on_lecture_complete failed")


transcriber = Transcriber()
