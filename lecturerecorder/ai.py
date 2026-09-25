"""Claude-powered study features: notes, chat, deep dives, quizzes, graded
written answers and flashcards, for one lecture or for a whole course.

Every request opens with the same study material (a lecture transcript, or a
course's transcripts and uploaded files) followed by a cache breakpoint, so
repeated questions about the same material only pay full price for it once.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Iterator

import anthropic

from . import db
from .config import load_settings
from .materials import IMAGE_EXT


class AIError(Exception):
    pass


BASE_INSTRUCTIONS = """You are a study assistant inside a lecture recording app. Lecture transcripts \
were produced automatically by a speech recognizer, so they may contain misheard words, missing \
punctuation and no speaker labels. Silently correct obvious transcription errors when the intended \
word is clear from context; if something important is ambiguous, say so rather than guessing.

Ground your answers in the student's material. When you add knowledge the material does not cover, \
make that clear (for example "Beyond what was covered, ..."). Write in Markdown. Use headings, short \
paragraphs, bullet lists and tables where they aid understanding. Use LaTeX between $...$ or $$...$$ \
only for real math. Do not use emoji."""

LECTURE_SCOPE = """The material is one lecture. Reference moments in it with [mm:ss] timestamps \
(square brackets, exactly that format) when that helps the student find them."""

COURSE_SCOPE = """The material is a whole course: several lecture transcripts and files the student \
uploaded (slides, readings, handouts). Draw on all of it and connect ideas across lectures. When you \
point to where something is covered, name the source in parentheses, for example (Lecture: Week 3 \
Entropy, 12:03) or (Chapter 4.pdf, page 12)."""


def _client() -> anthropic.Anthropic:
    key = load_settings()["api_key"] or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AIError("Add your Anthropic API key in Settings to use AI features.")
    return anthropic.Anthropic(api_key=key)


def _model_options(effort: str) -> dict:
    model = load_settings()["model"]
    opts: dict = {"model": model}
    if not model.startswith("claude-haiku"):
        opts["thinking"] = {"type": "adaptive"}
        opts["output_config"] = {"effort": effort}
    if model == "claude-opus-5":
        # If a safety classifier declines, the API re-runs on a suitable fallback model.
        opts["betas"] = ["server-side-fallback-2026-07-01"]
        opts["fallbacks"] = "default"
    return opts


# Study material ------------------------------------------------------------

class Context:
    """The material a request is about, as content blocks for the first user turn."""

    def __init__(self, scope: str, blocks: list[dict]):
        self.scope = scope
        self.blocks = blocks

    def system(self) -> str:
        return BASE_INSTRUCTIONS + "\n\n" + (LECTURE_SCOPE if self.scope == "lecture" else COURSE_SCOPE)

    def opening(self, task: str) -> list[dict]:
        blocks = [dict(b) for b in self.blocks]
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return blocks + [{"type": "text", "text": task}]


def lecture_context(lecture: dict) -> Context:
    transcript = db.transcript_text(lecture["id"])
    if not transcript.strip():
        raise AIError("This lecture has no transcript yet.")
    header = f"Lecture title: {lecture['title']}"
    if lecture.get("course"):
        header += f"\nCourse: {lecture['course']}"
    return Context("lecture", [{"type": "text", "text": f"{header}\n\n<transcript>\n{transcript}\n</transcript>"}])


def course_context(course: dict) -> Context:
    excluded = set(course["excluded"])
    blocks: list[dict] = [{"type": "text", "text": f"Course: {course['name']}"}]
    for lec in db.course_lectures(course["name"]):
        if f"lecture:{lec['id']}" in excluded:
            continue
        transcript = db.transcript_text(lec["id"])
        if transcript.strip():
            blocks.append({"type": "text", "text": f'<lecture title="{lec["title"]}">\n{transcript}\n</lecture>'})
    for mat in db.materials(course["id"], with_text=True):
        if f"material:{mat['id']}" in excluded:
            continue
        label = f'<material name="{mat["filename"]}">'
        if mat["kind"] == "pdf_scan":
            data = base64.standard_b64encode(Path(mat["path"]).read_bytes()).decode()
            blocks.append({"type": "text", "text": f"{label} (scanned PDF follows)"})
            blocks.append({"type": "document", "title": mat["filename"],
                           "source": {"type": "base64", "media_type": "application/pdf", "data": data}})
        elif mat["kind"] == "image":
            media = IMAGE_EXT[Path(mat["path"]).suffix.lower()]
            data = base64.standard_b64encode(Path(mat["path"]).read_bytes()).decode()
            blocks.append({"type": "text", "text": f"{label} (image follows)"})
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
        elif mat["text"].strip():
            blocks.append({"type": "text", "text": f"{label}\n{mat['text']}\n</material>"})
    if len(blocks) == 1:
        raise AIError("This course has no lecture transcripts or readable materials selected yet.")
    return Context("course", blocks)


# Calling the API -----------------------------------------------------------

def _friendly(exc: Exception) -> AIError:
    if isinstance(exc, anthropic.AuthenticationError):
        return AIError("The API key was rejected. Check it in Settings.")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return AIError("This API key does not have access to the selected model.")
    if isinstance(exc, anthropic.NotFoundError):
        return AIError("The selected model was not found. Pick another one in Settings.")
    if isinstance(exc, anthropic.RateLimitError):
        return AIError("Rate limited by the API. Wait a moment and try again.")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 413 or "too long" in str(exc.message).lower():
            return AIError("There is too much material for one request. Leave some sources out and try again.")
        return AIError(f"API error ({exc.status_code}): {exc.message}")
    if isinstance(exc, anthropic.APIConnectionError):
        return AIError("Could not reach the Anthropic API. Check your internet connection.")
    return AIError(str(exc))


def _check_stop(message) -> None:
    if message.stop_reason == "refusal":
        raise AIError("The model declined this request.")


def _stream_text(ctx: Context, messages: list[dict], effort: str) -> Iterator[str]:
    """Yield text as it streams in; raises AIError on failure."""
    try:
        client = _client()
        with client.beta.messages.stream(
            max_tokens=64000,
            system=ctx.system(),
            messages=messages,
            **_model_options(effort),
        ) as stream:
            for text in stream.text_stream:
                yield text
            final = stream.get_final_message()
        _check_stop(final)
        if final.stop_reason == "max_tokens":
            yield "\n\n*(Response was cut off because it reached the length limit.)*"
    except AIError:
        raise
    except anthropic.AnthropicError as exc:
        raise _friendly(exc) from exc


def _structured(ctx: Context, task: str, schema: dict, effort: str = "high") -> dict:
    opts = _model_options(effort)
    opts["output_config"] = {**opts.get("output_config", {}),
                             "format": {"type": "json_schema", "schema": schema}}
    try:
        client = _client()
        with client.beta.messages.stream(
            max_tokens=32000,
            system=ctx.system(),
            messages=[{"role": "user", "content": ctx.opening(task)}],
            **opts,
        ) as stream:
            final = stream.get_final_message()
    except anthropic.AnthropicError as exc:
        raise _friendly(exc) from exc
    _check_stop(final)
    if final.stop_reason == "max_tokens":
        raise AIError("The response was too long. Try asking for fewer items.")
    text = "".join(b.text for b in final.content if b.type == "text")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise AIError("The model returned malformed data. Try again.") from exc


def _obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _list_of(key: str, item: dict) -> dict:
    return _obj({key: {"type": "array", "items": item}})


# Notes ---------------------------------------------------------------------

NOTES_PROMPT = """Write study notes for this lecture that a student could revise from without \
re-listening. Structure:

# <A clear title for the lecture>
A 2-4 sentence overview of what the lecture covered and why it matters.

## Key concepts
One subsection per major concept, in the order they were taught: explain it clearly, include \
definitions, formulas, worked examples and the lecturer's own examples or analogies.

## Definitions
A compact table of important terms.

## Things the lecturer emphasized
Anything flagged as important, likely on an exam, a common mistake, or repeated. Include timestamps.

## Announcements and tasks
Deadlines, readings, assignments or logistics mentioned. Omit this section if there were none.

## Open questions
Points that were unclear in the recording or worth asking about.

Be thorough but tight: no filler, no restating the same point twice."""


def stream_notes(ctx: Context) -> Iterator[str]:
    return _stream_text(ctx, [{"role": "user", "content": ctx.opening(NOTES_PROMPT)}], effort="high")


def generate_notes(ctx: Context) -> str:
    return "".join(stream_notes(ctx))


# Chat ----------------------------------------------------------------------

CHAT_PREFIX = ("(You are now chatting with the student about this material. Be direct and "
               "conversational; answer at the length the question deserves.)\n\n")


def stream_chat(ctx: Context, history: list[dict], question: str) -> Iterator[str]:
    turns = [{"role": m["role"], "content": m["content"]} for m in history] + [{"role": "user", "content": question}]
    turns[0] = {"role": "user", "content": ctx.opening(CHAT_PREFIX + turns[0]["content"])}
    return _stream_text(ctx, turns, effort="medium")


# Topics --------------------------------------------------------------------

TOPICS_SCHEMA = _list_of("topics", _obj({
    "title": {"type": "string"}, "summary": {"type": "string"}, "timestamp": {"type": "string"},
}))


def find_topics(ctx: Context) -> list[dict]:
    data = _structured(
        ctx,
        "List the distinct topics this lecture covered, in order, as 4 to 12 items. For each give "
        "a short title, a one-sentence summary of what was said about it, and the mm:ss timestamp "
        "where it starts (without brackets).",
        TOPICS_SCHEMA,
        effort="medium",
    )
    return data["topics"]


def stream_expand(ctx: Context, topic: str) -> Iterator[str]:
    task = f"""Expand on this topic from the lecture: "{topic}"

Go deeper than the lecture did:
1. Start with a short recap of what the lecture said about it (with timestamps).
2. Explain it from first principles, building intuition before formalism.
3. Work through one or two concrete examples.
4. Cover common misconceptions and how to avoid them.
5. Connect it to related ideas the student should know next.
6. Suggest what to search for or read to learn more. Do not invent URLs or citations."""
    return _stream_text(ctx, [{"role": "user", "content": ctx.opening(task)}], effort="medium")


# Quizzes -------------------------------------------------------------------

CHOICE_SCHEMA = _list_of("questions", _obj({
    "question": {"type": "string"},
    "options": {"type": "array", "items": {"type": "string"}},
    "answer_index": {"type": "integer"},
    "explanation": {"type": "string"},
    "source": {"type": "string"},
}))

WRITTEN_SCHEMA = _list_of("questions", _obj({
    "question": {"type": "string"},
    "model_answer": {"type": "string"},
    "key_points": {"type": "array", "items": {"type": "string"}},
    "source": {"type": "string"},
}))

GRADING_SCHEMA = _list_of("results", _obj({
    "verdict": {"type": "string", "enum": ["correct", "partial", "incorrect"]},
    "feedback": {"type": "string"},
}))

DIFFICULTY = {
    "easy": "recall of key facts and definitions",
    "medium": "understanding and applying the concepts",
    "hard": "analysis, edge cases, multi-step reasoning and distinguishing closely related ideas",
}


def _source_hint(ctx: Context) -> str:
    return ("the [mm:ss] timestamp where it was covered" if ctx.scope == "lecture"
            else "which lecture or file covers it, e.g. 'Lecture: Week 3, 12:03' or 'Chapter 4.pdf, page 12'")


def make_quiz(ctx: Context, count: int, difficulty: str, focus: str = "", kind: str = "choice") -> list[dict]:
    focus_line = f"\nFocus on: {focus}" if focus.strip() else ""
    level = DIFFICULTY.get(difficulty, DIFFICULTY["medium"])
    spread = "" if ctx.scope == "lecture" else " Spread questions across the different lectures and materials."
    if kind == "written":
        data = _structured(
            ctx,
            f"Write {count} short-answer exam questions on this material testing {level}.{focus_line}{spread}\n\n"
            "Each should be answerable in 1-5 sentences (or a short calculation). Give a model answer, the "
            f"key points a full-marks answer must contain, and as source, {_source_hint(ctx)}.",
            WRITTEN_SCHEMA,
        )
        questions = [q for q in data["questions"] if q["question"].strip()]
    else:
        data = _structured(
            ctx,
            f"Write a {count}-question multiple-choice quiz on this material testing {level}.{focus_line}{spread}\n\n"
            "Each question has exactly 4 options with one correct answer (answer_index is 0-3). "
            "Make distractors plausible, vary the position of the correct answer, and do not use "
            "'all of the above' or 'none of the above'. The explanation should say why the answer is "
            "right and, briefly, why the tempting wrong option is wrong. As source, give "
            f"{_source_hint(ctx)}. Questions must be answerable from the material.",
            CHOICE_SCHEMA,
        )
        questions = [q for q in data["questions"]
                     if len(q["options"]) >= 2 and 0 <= q["answer_index"] < len(q["options"])]
    if not questions:
        raise AIError("The quiz came back empty. Try again.")
    return questions


def grade_written(ctx: Context, questions: list[dict], answers: list[str]) -> list[dict]:
    items = []
    for i, (q, a) in enumerate(zip(questions, answers), 1):
        items.append(
            f"<question n=\"{i}\">\n{q['question']}\n<model_answer>{q['model_answer']}</model_answer>\n"
            f"<key_points>{'; '.join(q['key_points'])}</key_points>\n"
            f"<student_answer>{a.strip() or '(no answer)'}</student_answer>\n</question>"
        )
    data = _structured(
        ctx,
        "Grade the student's answers to these questions, in order, one result per question. "
        "'correct' means it covers the key points (wording can differ); 'partial' means some key "
        "points are right but something important is missing or wrong; 'incorrect' otherwise, "
        "including blank answers. Feedback is 1-3 sentences addressed to the student: what they got "
        "right, what is missing or wrong, and the fix. Be fair and encouraging, not lenient.\n\n"
        + "\n\n".join(items),
        GRADING_SCHEMA,
        effort="medium",
    )
    results = data["results"][: len(questions)]
    while len(results) < len(questions):
        results.append({"verdict": "incorrect", "feedback": "Could not be graded."})
    return results


# Flashcards ----------------------------------------------------------------

CARDS_SCHEMA = _list_of("cards", _obj({
    "front": {"type": "string"}, "back": {"type": "string"}, "source": {"type": "string"},
}))


def make_flashcards(ctx: Context, count: int) -> list[dict]:
    spread = "" if ctx.scope == "lecture" else " Cover all the lectures and materials, not just one."
    data = _structured(
        ctx,
        f"Create {count} flashcards covering the most important material.{spread} "
        "The front is a specific question or term; the back is a concise answer (1-3 sentences, "
        "Markdown allowed). Prefer cards that test understanding over trivia. Order them as the "
        f"material was taught. As source, give {_source_hint(ctx)}.",
        CARDS_SCHEMA,
        effort="medium",
    )
    return [c for c in data["cards"] if c["front"].strip() and c["back"].strip()]


# Side talk -----------------------------------------------------------------

SIDE_TALK_MODEL = "claude-haiku-4-5"  # small, frequent classification calls: the cheapest model is plenty
SIDE_TALK_SCHEMA = _obj({"side_talk": {"type": "array", "items": {"type": "integer"}}})
SIDE_TALK_PROMPT = """These numbered lines come from an automatic transcript of a class recording. \
The student wants to separate the lecture from side talk.

Side talk: conversation that is not part of the class, such as students chatting or joking with each \
other, personal conversations, phone calls, and remarks unrelated to the course.

Not side talk: anything the instructor says about the subject (including jokes, stories and \
tangents the instructor uses while teaching), questions and answers about the material, and course \
logistics such as deadlines, readings and announcements. When unsure, treat a line as lecture content.

Lecture: {title}
{context}
<lines>
{lines}
</lines>

List the numbers of the side-talk lines. Return an empty list if there are none."""


def detect_side_talk(title: str, context: list[str], lines: list[str]) -> set[int]:
    """Return the 0-based indexes of lines that are side talk rather than lecture content."""
    ctx = ""
    if context:
        ctx = "\nThe lines just before these, for context only:\n<earlier>\n" + "\n".join(context) + "\n</earlier>"
    numbered = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))
    try:
        message = _client().messages.create(
            model=SIDE_TALK_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": SIDE_TALK_PROMPT.format(title=title, context=ctx, lines=numbered)}],
            output_config={"format": {"type": "json_schema", "schema": SIDE_TALK_SCHEMA}},
        )
    except anthropic.AnthropicError as exc:
        raise _friendly(exc) from exc
    _check_stop(message)
    text = "".join(b.text for b in message.content if b.type == "text")
    try:
        numbers = json.loads(text)["side_talk"]
    except (ValueError, KeyError) as exc:
        raise AIError("Side-talk detection returned malformed data.") from exc
    return {n - 1 for n in numbers if isinstance(n, int) and 1 <= n <= len(lines)}
