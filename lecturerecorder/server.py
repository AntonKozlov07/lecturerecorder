"""HTTP API and static UI.

The main server listens on 127.0.0.1 only. When phone access is switched on,
phone.py also serves the same app on the local network, where every request
must come from a paired device.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Callable, Iterator

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, db, phone
from .config import AUDIO_DIR, MATERIALS_DIR, load_settings, public_settings, save_settings
from .materials import ACCEPTED, MaterialError, estimate_tokens, extract
from .transcriber import transcriber

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Lecture Recorder", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def guard(request: Request, call_next):
    if phone.is_phone_request(request):
        denied = phone.authorize(request)
        if denied is not None:
            return denied
    else:
        # Guard against DNS rebinding: the Host must be this computer.
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ALLOWED_HOSTS:
            return PlainTextResponse("Forbidden", status_code=403)
    # State-changing calls must carry a custom header, which browsers will not send
    # cross-origin without a CORS preflight that this server never allows.
    if (request.method not in ("GET", "HEAD") and request.headers.get("x-lecture-recorder") != "1"
            and not request.url.path.startswith("/phone/")):
        return PlainTextResponse("Forbidden", status_code=403)
    return await call_next(request)


def _lecture_or_404(lecture_id: str) -> dict:
    lec = db.get_lecture(lecture_id)
    if not lec:
        raise HTTPException(404, "Lecture not found")
    return lec


def _course_or_404(course_id: str) -> dict:
    course = db.get_course(course_id)
    if not course:
        raise HTTPException(404, "Course not found")
    return course


def _audio_dir(lecture_id: str) -> Path:
    path = AUDIO_DIR / lecture_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sse(make_gen: Callable[[], Iterator[str]], on_done=None) -> StreamingResponse:
    """Stream text chunks as server-sent events and hand the full text to on_done."""

    def events():
        parts: list[str] = []
        try:
            for chunk in make_gen():
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


def _ai_json(fn, *args):
    try:
        return fn(*args)
    except ai.AIError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


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
    if {"whisper_model", "whisper_device"} & changes.keys():
        transcriber.warm_up()
    return public_settings()


@app.get("/api/status")
def status(request: Request):
    return {"transcriber": transcriber.status(), "ai_ready": public_settings()["has_api_key"],
            "on_phone": phone.is_phone_request(request)}


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
    scope = ("lecture", lecture_id)
    lec["segments"] = db.segments(lecture_id)
    lec["messages"] = db.chat_history(scope)
    lec["expansions"] = db.expansions(lecture_id)
    lec["quizzes"] = db.quizzes(scope)
    lec["flashcards"] = db.flashcards(scope)
    course = db.one("SELECT id FROM courses WHERE name = ?", (lec["course"],)) if lec["course"] else None
    lec["course_id"] = course["id"] if course else None
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
    kind = audio.content_type or ""
    ext = ".ogg" if "ogg" in kind else ".mp4" if ("mp4" in kind or "aac" in kind) else ".webm"
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
            notes = ai.generate_notes(ai.lecture_context(db.get_lecture(lecture_id)))
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
            yield from ai.stream_notes(ai.lecture_context(lec))
        except BaseException as exc:
            db.update_lecture(lecture_id, notes_status="done" if lec["notes"] else "error",
                              notes_error=str(exc) if isinstance(exc, ai.AIError) else "")
            raise

    return _sse(gen, done)


# Topics and expansions (single lectures) -----------------------------------

@app.post("/api/lectures/{lecture_id}/topics")
def topics(lecture_id: str):
    lec = _lecture_or_404(lecture_id)
    result = _ai_json(lambda: ai.find_topics(ai.lecture_context(lec)))
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

    return _sse(lambda: ai.stream_expand(ai.lecture_context(lec), topic), done)


@app.delete("/api/expansions/{expansion_id}")
def delete_expansion(expansion_id: int):
    db.delete_expansion(expansion_id)
    return {"ok": True}


# Courses and materials -----------------------------------------------------

class CourseIn(BaseModel):
    name: str


class CoursePatch(BaseModel):
    name: str | None = None
    excluded: list[str] | None = None


def _material_view(m: dict) -> dict:
    m = {k: v for k, v in m.items() if k not in ("text", "path")}
    m["tokens"] = estimate_tokens(m)
    return m


@app.get("/api/courses")
def list_courses():
    return db.list_courses()


@app.post("/api/courses")
def create_course(body: CourseIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Course name is empty")
    return db.ensure_course(name)


@app.get("/api/courses/{course_id}")
def course_detail(course_id: str):
    course = _course_or_404(course_id)
    scope = ("course", course_id)
    lectures = db.course_lectures(course["name"])
    for lec in lectures:
        text = db.transcript_text(lec["id"])
        lec["tokens"] = len(text) // 4
    course["lectures"] = lectures
    course["materials"] = [_material_view(m) for m in db.materials(course_id)]
    course["messages"] = db.chat_history(scope)
    course["quizzes"] = db.quizzes(scope)
    course["flashcards"] = db.flashcards(scope)
    course["accepted"] = ACCEPTED
    return course


@app.patch("/api/courses/{course_id}")
def patch_course(course_id: str, body: CoursePatch):
    _course_or_404(course_id)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "Course name is empty")
        clash = db.one("SELECT id FROM courses WHERE name = ? AND id != ?", (name, course_id))
        if clash:
            raise HTTPException(400, "Another course already has that name")
        db.rename_course(course_id, name)
    if body.excluded is not None:
        db.set_course_excluded(course_id, body.excluded)
    return db.get_course(course_id)


@app.delete("/api/courses/{course_id}")
def delete_course(course_id: str):
    _course_or_404(course_id)
    db.delete_course(course_id)
    shutil.rmtree(MATERIALS_DIR / course_id, ignore_errors=True)
    return {"ok": True}


@app.post("/api/courses/{course_id}/materials")
async def upload_material(course_id: str, file: UploadFile = File(...)):
    _course_or_404(course_id)
    name = Path(file.filename or "file").name
    folder = MATERIALS_DIR / course_id
    folder.mkdir(parents=True, exist_ok=True)
    stem, ext, n = Path(name).stem, Path(name).suffix.lower(), 1
    path = folder / f"{stem}{ext}"
    while path.exists():
        n += 1
        path = folder / f"{stem} ({n}){ext}"
    size = 0
    with path.open("wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            out.write(chunk)
    try:
        kind, text, pages = extract(path)
    except MaterialError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc))
    material_id = db.add_material(course_id, name, kind, str(path), text, pages, size)
    return _material_view(next(m for m in db.materials(course_id) if m["id"] == material_id))


@app.get("/api/materials/{material_id}/file")
def material_file(material_id: int):
    mat = db.get_material(material_id)
    if not mat or not Path(mat["path"]).exists():
        raise HTTPException(404, "File not found")
    return FileResponse(mat["path"], filename=mat["filename"], content_disposition_type="inline")


@app.get("/api/materials/{material_id}/text")
def material_text(material_id: int):
    mat = db.get_material(material_id)
    if not mat:
        raise HTTPException(404, "File not found")
    return {"text": mat["text"]}


@app.delete("/api/materials/{material_id}")
def delete_material(material_id: int):
    mat = db.get_material(material_id)
    if mat:
        Path(mat["path"]).unlink(missing_ok=True)
        db.delete_material(material_id)
    return {"ok": True}


# Chat, quizzes and flashcards for a lecture or a course ---------------------

def _scope(kind: str, item_id: str) -> tuple[tuple[str, str], Callable[[], ai.Context]]:
    if kind == "lectures":
        lec = _lecture_or_404(item_id)
        return ("lecture", item_id), lambda: ai.lecture_context(lec)
    if kind == "courses":
        course = _course_or_404(item_id)
        return ("course", item_id), lambda: ai.course_context(course)
    raise HTTPException(404, "Not found")


class ChatIn(BaseModel):
    message: str


@app.post("/api/{kind}/{item_id}/chat/stream")
def chat_stream(kind: str, item_id: str, body: ChatIn):
    scope, context = _scope(kind, item_id)
    question = body.message.strip()
    if not question:
        raise HTTPException(400, "Message is empty")
    history = db.chat_history(scope)

    def done(text: str):
        db.add_message(scope, "user", question)
        db.add_message(scope, "assistant", text)

    return _sse(lambda: ai.stream_chat(context(), history, question), done)


@app.delete("/api/{kind}/{item_id}/chat")
def clear_chat(kind: str, item_id: str):
    scope, _ = _scope(kind, item_id)
    db.clear_chat(scope)
    return {"ok": True}


class QuizIn(BaseModel):
    count: int = 10
    difficulty: str = "medium"
    focus: str = ""
    kind: str = "choice"


@app.post("/api/{kind}/{item_id}/quizzes")
def create_quiz(kind: str, item_id: str, body: QuizIn):
    scope, context = _scope(kind, item_id)
    quiz_kind = "written" if body.kind == "written" else "choice"
    count = max(3, min(25, body.count))
    result = _ai_json(lambda: ai.make_quiz(context(), count, body.difficulty, body.focus, quiz_kind))
    if not isinstance(result, list):
        return result
    return db.get_quiz(db.add_quiz(scope, quiz_kind, body.difficulty, result))


class AnswersIn(BaseModel):
    answers: list[int | str | None]


@app.post("/api/quizzes/{quiz_id}/submit")
def submit_quiz(quiz_id: int, body: AnswersIn):
    quiz = db.get_quiz(quiz_id)
    if not quiz:
        raise HTTPException(404, "Quiz not found")
    if quiz["kind"] == "written":
        answers = [a if isinstance(a, str) else "" for a in body.answers]
        answers += [""] * (len(quiz["questions"]) - len(answers))
        kind, item_id = ("lectures", quiz["lecture_id"]) if quiz["lecture_id"] else ("courses", quiz["course_id"])
        _, context = _scope(kind, item_id)
        grading = _ai_json(lambda: ai.grade_written(context(), quiz["questions"], answers))
        if not isinstance(grading, list):
            return grading
        score = sum({"correct": 1, "partial": 0.5}.get(g["verdict"], 0) for g in grading)
        db.submit_quiz(quiz_id, answers, score, grading)
    else:
        score = sum(1 for q, a in zip(quiz["questions"], body.answers) if a == q["answer_index"])
        db.submit_quiz(quiz_id, body.answers, score)
    return db.get_quiz(quiz_id)


@app.delete("/api/quizzes/{quiz_id}")
def delete_quiz(quiz_id: int):
    db.delete_quiz(quiz_id)
    return {"ok": True}


class CardsIn(BaseModel):
    count: int = 20


@app.post("/api/{kind}/{item_id}/flashcards")
def create_flashcards(kind: str, item_id: str, body: CardsIn):
    scope, context = _scope(kind, item_id)
    result = _ai_json(lambda: ai.make_flashcards(context(), max(5, min(60, body.count))))
    if not isinstance(result, list):
        return result
    db.replace_flashcards(scope, result)
    return db.flashcards(scope)


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
    cards = db.flashcards(("lecture", lecture_id))
    if cards:
        out += ["", "## Flashcards", ""] + [f"- **{c['front']}**: {c['back']}" for c in cards]
    out += ["", "## Transcript", "", db.transcript_text(lecture_id)]
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in lec["title"]).strip() or "lecture"
    return PlainTextResponse("\n".join(out) + "\n", media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="{safe}.md"'})


# Phone access --------------------------------------------------------------

app.include_router(phone.router)
