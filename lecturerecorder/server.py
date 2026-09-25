"""Local HTTP API and static UI. Only ever bound to 127.0.0.1."""

from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, db
from .config import AUDIO_DIR, load_settings, public_settings, save_settings
from .transcriber import transcriber

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Lecture Recorder", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Guard against DNS rebinding and cross-site requests from pages open in other tabs:
    # the Host must be local and state-changing calls must carry a custom header,
    # which browsers will not send cross-origin without a CORS preflight we never allow.
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if host not in ALLOWED_HOSTS:
        return PlainTextResponse("Forbidden", status_code=403)
    if request.method not in ("GET", "HEAD") and request.headers.get("x-lecture-recorder") != "1":
        return PlainTextResponse("Forbidden", status_code=403)
    return await call_next(request)


def _lecture_or_404(lecture_id: str) -> dict:
    lec = db.get_lecture(lecture_id)
    if not lec:
        raise HTTPException(404, "Lecture not found")
    return lec


def _audio_dir(lecture_id: str) -> Path:
    path = AUDIO_DIR / lecture_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sse(gen: Iterator[str], on_done=None) -> StreamingResponse:
    """Stream text chunks as server-sent events and hand the full text to on_done."""

    def events():
        parts: list[str] = []
        try:
            for chunk in gen:
                parts.append(chunk)
                yield f"data: {json.dumps({'t': chunk})}\n\n"
            full = "".join(parts)
            extra = on_done(full) if on_done else None
            yield f"data: {json.dumps({'done': True, **(extra or {})})}\n\n"
        except ai.AIError as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        except Exception as exc:
            log.exception("stream failed")
            yield f"data: {json.dumps({'error': f'Unexpected error: {exc}'})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# Settings and status -------------------------------------------------------

class SettingsIn(BaseModel):
    api_key: str | None = None
    model: str | None = None
    whisper_model: str | None = None
    whisper_device: str | None = None
    language: str | None = None
    segment_seconds: int | None = None
    auto_notes: bool | None = None


@app.get("/api/settings")
def get_settings():
    return public_settings()


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if "api_key" in changes:
        changes["api_key"] = changes["api_key"].strip()
    if "segment_seconds" in changes:
        changes["segment_seconds"] = max(10, min(120, changes["segment_seconds"]))
    save_settings(changes)
    return public_settings()


@app.get("/api/status")
def status():
    return {"transcriber": transcriber.status(), "ai_ready": public_settings()["has_api_key"]}


# Lectures ------------------------------------------------------------------

class LectureIn(BaseModel):
    title: str = ""
    course: str = ""


class LecturePatch(BaseModel):
    title: str | None = None
    course: str | None = None
    notes: str | None = None


@app.get("/api/lectures")
def list_lectures(q: str = ""):
    return db.list_lectures(q.strip())


@app.post("/api/lectures")
def create_lecture(body: LectureIn):
    return db.create_lecture(body.title.strip() or "Untitled lecture", body.course.strip())


@app.get("/api/lectures/{lecture_id}")
def lecture_detail(lecture_id: str):
    lec = _lecture_or_404(lecture_id)
    lec["segments"] = db.segments(lecture_id)
    lec["messages"] = db.chat_history(lecture_id)
    lec["expansions"] = db.expansions(lecture_id)
    lec["quizzes"] = db.quizzes(lecture_id)
    lec["flashcards"] = db.flashcards(lecture_id)
    return lec


@app.patch("/api/lectures/{lecture_id}")
def patch_lecture(lecture_id: str, body: LecturePatch):
    _lecture_or_404(lecture_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "notes" in fields:
        fields["notes_status"] = "done" if fields["notes"].strip() else "none"
    db.update_lecture(lecture_id, **fields)
    return db.get_lecture(lecture_id)


@app.delete("/api/lectures/{lecture_id}")
def delete_lecture(lecture_id: str):
    _lecture_or_404(lecture_id)
    db.delete_lecture(lecture_id)
    shutil.rmtree(AUDIO_DIR / lecture_id, ignore_errors=True)
    return {"ok": True}


# Audio in ------------------------------------------------------------------

@app.post("/api/lectures/{lecture_id}/segments")
async def upload_segment(lecture_id: str, audio: UploadFile = File(...), idx: int = Form(...),
                         start: float = Form(...), duration: float = Form(...)):
    lec = _lecture_or_404(lecture_id)
    ext = ".ogg" if "ogg" in (audio.content_type or "") else ".mp4" if "mp4" in (audio.content_type or "") else ".webm"
    path = _audio_dir(lecture_id) / f"seg{idx:05d}{ext}"
    path.write_bytes(await audio.read())
    seg_id = db.add_segment(lecture_id, idx, start, duration, str(path))
    db.update_lecture(lecture_id, duration=max(lec["duration"], start + duration))
    transcriber.enqueue(seg_id)
    return {"id": seg_id}


class FinishIn(BaseModel):
    duration: float


@app.post("/api/lectures/{lecture_id}/finish")
def finish_recording(lecture_id: str, body: FinishIn):
    lec = _lecture_or_404(lecture_id)
    db.update_lecture(lecture_id, status="processing", duration=max(lec["duration"], body.duration))
    transcriber._check_complete(lecture_id)
    return db.get_lecture(lecture_id)


@app.post("/api/import")
async def import_audio(file: UploadFile = File(...), title: str = Form(""), course: str = Form("")):
    name = Path(file.filename or "audio").name
    lec = db.create_lecture(title.strip() or Path(name).stem, course.strip(), status="processing")
    path = _audio_dir(lec["id"]) / f"import{Path(name).suffix.lower() or '.bin'}"
    with path.open("wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)
    seg_id = db.add_segment(lec["id"], 0, 0, 0, str(path))
    transcriber.enqueue(seg_id)
    return db.get_lecture(lec["id"])


class TranscriptIn(BaseModel):
    text: str


@app.post("/api/lectures/{lecture_id}/transcript")
def paste_transcript(lecture_id: str, body: TranscriptIn):
    _lecture_or_404(lecture_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Transcript is empty")
    idx = db.next_segment_idx(lecture_id)
    db.add_segment(lecture_id, idx, 0, 0, "", text=text, status="done")
    db.update_lecture(lecture_id, status="ready")
    _maybe_auto_notes(lecture_id)
    return {"ok": True}


@app.post("/api/segments/{segment_id}/retry")
def retry_segment(segment_id: int):
    seg = db.get_segment(segment_id)
    if not seg or not seg["audio_path"]:
        raise HTTPException(404, "Segment not found")
    db.requeue_segment(segment_id)
    lec = db.get_lecture(seg["lecture_id"])
    if lec and lec["status"] == "ready":
        db.update_lecture(lec["id"], status="processing")
    transcriber.enqueue(segment_id)
    return {"ok": True}


@app.get("/api/segments/{segment_id}/audio")
def segment_audio(segment_id: int):
    seg = db.get_segment(segment_id)
    if not seg or not seg["audio_path"] or not Path(seg["audio_path"]).exists():
        raise HTTPException(404, "Audio not found")
    return FileResponse(seg["audio_path"])


# Notes ---------------------------------------------------------------------

def _maybe_auto_notes(lecture_id: str) -> None:
    settings = load_settings()
    lec = db.get_lecture(lecture_id)
    if not (settings["auto_notes"] and public_settings()["has_api_key"] and lec):
        return
    if lec["notes_status"] != "none" or not db.transcript_text(lecture_id).strip():
        return

    def work():
        db.update_lecture(lecture_id, notes_status="generating", notes_error="")
        try:
            notes = ai.generate_notes(db.get_lecture(lecture_id))
            db.update_lecture(lecture_id, notes=notes, notes_status="done")
        except Exception as exc:
            db.update_lecture(lecture_id, notes_status="error", notes_error=str(exc))

    threading.Thread(target=work, name=f"notes-{lecture_id}", daemon=True).start()


transcriber.on_lecture_complete = _maybe_auto_notes


@app.post("/api/lectures/{lecture_id}/notes/stream")
def notes_stream(lecture_id: str):
    lec = _lecture_or_404(lecture_id)
    db.update_lecture(lecture_id, notes_status="generating", notes_error="")

    def done(text: str):
        db.update_lecture(lecture_id, notes=text, notes_status="done")

    def gen():
        try:
            yield from ai.stream_notes(lec)
        except BaseException as exc:
            prior = lec["notes"]
            db.update_lecture(lecture_id, notes_status="done" if prior else "error",
                              notes_error=str(exc) if isinstance(exc, ai.AIError) else "")
            raise

    return _sse(gen(), done)


# Chat ----------------------------------------------------------------------

class ChatIn(BaseModel):
    message: str


@app.post("/api/lectures/{lecture_id}/chat/stream")
def chat_stream(lecture_id: str, body: ChatIn):
    lec = _lecture_or_404(lecture_id)
    question = body.message.strip()
    if not question:
        raise HTTPException(400, "Message is empty")
    history = db.chat_history(lecture_id)

    def done(text: str):
        db.add_message(lecture_id, "user", question)
        db.add_message(lecture_id, "assistant", text)

    return _sse(ai.stream_chat(lec, history, question), done)


@app.delete("/api/lectures/{lecture_id}/chat")
def clear_chat(lecture_id: str):
    db.clear_chat(lecture_id)
    return {"ok": True}


# Topics and expansions -----------------------------------------------------

def _ai_json(fn, *args):
    try:
        return fn(*args)
    except ai.AIError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@app.post("/api/lectures/{lecture_id}/topics")
def topics(lecture_id: str):
    lec = _lecture_or_404(lecture_id)
    result = _ai_json(ai.find_topics, lec)
    if isinstance(result, list):
        db.update_lecture(lecture_id, topics=result)
    return result


class ExpandIn(BaseModel):
    topic: str


@app.post("/api/lectures/{lecture_id}/expand/stream")
def expand_stream(lecture_id: str, body: ExpandIn):
    lec = _lecture_or_404(lecture_id)
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(400, "Topic is empty")

    def done(text: str):
        return {"id": db.add_expansion(lecture_id, topic, text)}

    return _sse(ai.stream_expand(lec, topic), done)


@app.delete("/api/expansions/{expansion_id}")
def delete_expansion(expansion_id: int):
    db.delete_expansion(expansion_id)
    return {"ok": True}


# Quizzes -------------------------------------------------------------------

class QuizIn(BaseModel):
    count: int = 10
    difficulty: str = "medium"
    focus: str = ""


@app.post("/api/lectures/{lecture_id}/quizzes")
def create_quiz(lecture_id: str, body: QuizIn):
    lec = _lecture_or_404(lecture_id)
    count = max(3, min(25, body.count))
    result = _ai_json(ai.make_quiz, lec, count, body.difficulty, body.focus)
    if not isinstance(result, list):
        return result
    quiz_id = db.add_quiz(lecture_id, body.difficulty, result)
    return db.get_quiz(quiz_id)


class AnswersIn(BaseModel):
    answers: list[int | None]


@app.post("/api/quizzes/{quiz_id}/submit")
def submit_quiz(quiz_id: int, body: AnswersIn):
    quiz = db.get_quiz(quiz_id)
    if not quiz:
        raise HTTPException(404, "Quiz not found")
    score = sum(1 for q, a in zip(quiz["questions"], body.answers) if a == q["answer_index"])
    db.submit_quiz(quiz_id, body.answers, score)
    return db.get_quiz(quiz_id)


@app.delete("/api/quizzes/{quiz_id}")
def delete_quiz(quiz_id: int):
    db.delete_quiz(quiz_id)
    return {"ok": True}


# Flashcards ----------------------------------------------------------------

class CardsIn(BaseModel):
    count: int = 20


@app.post("/api/lectures/{lecture_id}/flashcards")
def create_flashcards(lecture_id: str, body: CardsIn):
    lec = _lecture_or_404(lecture_id)
    result = _ai_json(ai.make_flashcards, lec, max(5, min(60, body.count)))
    if not isinstance(result, list):
        return result
    db.replace_flashcards(lecture_id, result)
    return db.flashcards(lecture_id)


# Export --------------------------------------------------------------------

@app.get("/api/lectures/{lecture_id}/export")
def export_markdown(lecture_id: str):
    lec = _lecture_or_404(lecture_id)
    out = [f"# {lec['title']}"]
    if lec["course"]:
        out.append(f"*{lec['course']}*")
    if lec["notes"].strip():
        out += ["", "## Notes", "", lec["notes"].strip()]
    for exp in reversed(db.expansions(lecture_id)):
        out += ["", f"## Deep dive: {exp['topic']}", "", exp["content"].strip()]
    cards = db.flashcards(lecture_id)
    if cards:
        out += ["", "## Flashcards", ""] + [f"- **{c['front']}**: {c['back']}" for c in cards]
    out += ["", "## Transcript", "", db.transcript_text(lecture_id)]
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in lec["title"]).strip() or "lecture"
    return PlainTextResponse("\n".join(out) + "\n", media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="{safe}.md"'})
