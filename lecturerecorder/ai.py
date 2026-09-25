"""Claude-powered study features: notes, chat, topic deep dives, quizzes, flashcards.

Every request shares the same system prompt (general instructions + the lecture
transcript) with a cache breakpoint on it, so asking several questions about the
same lecture only pays full price for the transcript once.
"""

from __future__ import annotations

import json
import os
from typing import Iterator

import anthropic

from . import db
from .config import load_settings


class AIError(Exception):
    pass


BASE_INSTRUCTIONS = """You are a study assistant inside a lecture recording app. The student recorded \
a lecture and it was transcribed automatically by a speech recognizer, so the transcript may contain \
misheard words, missing punctuation and no speaker labels. Silently correct obvious transcription \
errors when the intended word is clear from context; if something important is ambiguous, say so \
rather than guessing.

Ground your answers in the lecture. When you add knowledge the lecture did not cover, make that \
clear (for example "Beyond what was covered in the lecture, ..."). Reference moments in the lecture \
with their [mm:ss] timestamps when that helps the student find them.

Write in Markdown. Use headings, short paragraphs, bullet lists and tables where they aid \
understanding. Use LaTeX between $...$ or $$...$$ only for real math. Do not use emoji."""


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


def _system(lecture: dict) -> list[dict]:
    transcript = db.transcript_text(lecture["id"])
    if not transcript.strip():
        raise AIError("This lecture has no transcript yet.")
    header = f"Lecture title: {lecture['title']}"
    if lecture.get("course"):
        header += f"\nCourse: {lecture['course']}"
    return [
        {"type": "text", "text": BASE_INSTRUCTIONS},
        {
            "type": "text",
            "text": f"{header}\n\n<transcript>\n{transcript}\n</transcript>",
            "cache_control": {"type": "ephemeral"},
        },
    ]


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
        return AIError(f"API error ({exc.status_code}): {exc.message}")
    if isinstance(exc, anthropic.APIConnectionError):
        return AIError("Could not reach the Anthropic API. Check your internet connection.")
    return AIError(str(exc))


def _check_stop(message) -> None:
    if message.stop_reason == "refusal":
        raise AIError("The model declined this request.")


def _stream_text(lecture: dict, messages: list[dict], effort: str) -> Iterator[str]:
    """Yield text as it streams in; raises AIError on failure."""
    try:
        client = _client()
        with client.beta.messages.stream(
            max_tokens=64000,
            system=_system(lecture),
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


def _structured(lecture: dict, prompt: str, schema: dict, effort: str = "high") -> dict:
    opts = _model_options(effort)
    opts["output_config"] = {**opts.get("output_config", {}),
                             "format": {"type": "json_schema", "schema": schema}}
    try:
        client = _client()
        with client.beta.messages.stream(
            max_tokens=32000,
            system=_system(lecture),
            messages=[{"role": "user", "content": prompt}],
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


def stream_notes(lecture: dict) -> Iterator[str]:
    return _stream_text(lecture, [{"role": "user", "content": NOTES_PROMPT}], effort="high")


def generate_notes(lecture: dict) -> str:
    return "".join(stream_notes(lecture))


# Chat ----------------------------------------------------------------------

CHAT_PREFIX = ("(You are now chatting with the student about this lecture. Be direct and "
               "conversational; answer at the length the question deserves.)\n\n")


def stream_chat(lecture: dict, history: list[dict], question: str) -> Iterator[str]:
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    if messages:
        messages[0] = {"role": messages[0]["role"], "content": CHAT_PREFIX + messages[0]["content"]}
        messages.append({"role": "user", "content": question})
    else:
        messages = [{"role": "user", "content": CHAT_PREFIX + question}]
    return _stream_text(lecture, messages, effort="medium")


# Topics --------------------------------------------------------------------

TOPICS_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "timestamp": {"type": "string"},
                },
                "required": ["title", "summary", "timestamp"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}


def find_topics(lecture: dict) -> list[dict]:
    data = _structured(
        lecture,
        "List the distinct topics this lecture covered, in order, as 4 to 12 items. For each give "
        "a short title, a one-sentence summary of what was said about it, and the [mm:ss] timestamp "
        "where it starts (without brackets).",
        TOPICS_SCHEMA,
        effort="medium",
    )
    return data["topics"]


def stream_expand(lecture: dict, topic: str) -> Iterator[str]:
    prompt = f"""Expand on this topic from the lecture: "{topic}"

Go deeper than the lecture did:
1. Start with a short recap of what the lecture said about it (with timestamps).
2. Explain it from first principles, building intuition before formalism.
3. Work through one or two concrete examples.
4. Cover common misconceptions and how to avoid them.
5. Connect it to related ideas the student should know next.
6. Suggest what to search for or read to learn more. Do not invent URLs or citations."""
    return _stream_text(lecture, [{"role": "user", "content": prompt}], effort="medium")


# Quiz ----------------------------------------------------------------------

QUIZ_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "answer_index": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "topic": {"type": "string"},
                },
                "required": ["question", "options", "answer_index", "explanation", "topic"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

DIFFICULTY = {
    "easy": "recall of key facts and definitions",
    "medium": "understanding and applying the concepts",
    "hard": "analysis, edge cases, multi-step reasoning and distinguishing closely related ideas",
}


def make_quiz(lecture: dict, count: int, difficulty: str, focus: str = "") -> list[dict]:
    focus_line = f"\nFocus on: {focus}" if focus.strip() else ""
    data = _structured(
        lecture,
        f"Write a {count}-question multiple-choice quiz on this lecture testing "
        f"{DIFFICULTY.get(difficulty, DIFFICULTY['medium'])}.{focus_line}\n\n"
        "Each question has exactly 4 options with one correct answer (answer_index is 0-3). "
        "Make distractors plausible, vary the position of the correct answer, and do not use "
        "'all of the above' or 'none of the above'. The explanation should say why the answer is "
        "right and, briefly, why the tempting wrong option is wrong, citing the lecture timestamp "
        "where relevant. Questions must be answerable from the lecture.",
        QUIZ_SCHEMA,
    )
    questions = [
        q for q in data["questions"]
        if len(q["options"]) >= 2 and 0 <= q["answer_index"] < len(q["options"])
    ]
    if not questions:
        raise AIError("The quiz came back empty. Try again.")
    return questions


# Flashcards ----------------------------------------------------------------

CARDS_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"front": {"type": "string"}, "back": {"type": "string"}},
                "required": ["front", "back"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}


def make_flashcards(lecture: dict, count: int) -> list[dict]:
    data = _structured(
        lecture,
        f"Create {count} flashcards covering the most important material in this lecture. "
        "The front is a specific question or term; the back is a concise answer (1-3 sentences, "
        "Markdown allowed). Prefer cards that test understanding over trivia. Order them as the "
        "material was taught.",
        CARDS_SCHEMA,
        effort="medium",
    )
    return [c for c in data["cards"] if c["front"].strip() and c["back"].strip()]
