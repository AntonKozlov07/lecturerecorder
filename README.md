# Lecture Recorder

A desktop app for recording lectures and studying them afterwards.

- **Record** from any microphone. The transcript fills in while you record, and speech-to-text runs on your own computer with [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Your audio never leaves the machine.
- **Notes** are written automatically when you press Stop: overview, key concepts, definitions, what the lecturer emphasized, deadlines and open questions. You can edit them.
- **Chat** with the lecture. Answers are based on the transcript, cite timestamps, and say when they go beyond what was covered.
- **Topics**: get a list of what the lecture covered, then open a deep dive on any topic, or on anything you type in.
- **Quiz**: multiple choice at easy, medium or hard difficulty, with an optional focus. Each answer is explained. Past scores are kept.
- **Flashcards**: a generated deck with flip, "again" and "got it", plus keyboard shortcuts.
- **Import** an existing audio or video file, or paste a transcript.
- Timestamps anywhere in the app (notes, chat, quiz explanations) play the recording from that moment.
- Search across all lectures and transcripts, group by course, export everything to Markdown. Math renders properly. Light and dark themes follow your system.

![Notes view](docs/notes.png)
![Quiz view, dark theme](docs/quiz-dark.png)

## Requirements

- Python 3.10 or newer ([python.org](https://www.python.org/downloads/); on Windows, tick "Add Python to PATH")
- An [Anthropic API key](https://console.anthropic.com/) for notes, chat, topics, quizzes and flashcards. Recording and transcription work without one.
- Google Chrome, Microsoft Edge, Chromium or Brave. The app opens in one of these as its own window. Without one it opens in your default browser.

## Run it

**Windows:** double-click `run.bat`.

**macOS / Linux:** run `./run.sh`.

The first launch creates a virtual environment and installs dependencies, which takes a few minutes. The first recording also downloads the speech model (about 460 MB for the default "small" model). After that, startup takes a few seconds.

The app asks for your API key the first time it opens. You can change it, the Claude model, the speech model and more under **Settings**.

To run it by hand:

```sh
python -m venv .venv
.venv/bin/pip install -e .            # Windows: .venv\Scripts\pip install -e .
.venv/bin/python -m lecturerecorder   # options: --port 8765, --browser, --no-window
```

## Tips

- **Speech model:** "small" suits most laptops. "medium" or "large-v3" are more accurate and much faster with an NVIDIA GPU. "base" or "tiny" help on slow machines.
- **Language:** auto-detect works well. Set a language code (for example `en`) if a lecture mixes languages or is detected wrongly.
- **Placement:** sit close to the speaker, or use an external microphone. Transcription quality drives everything else.
- **Closing the window** quits the app. If you close it mid-recording, everything captured up to the last chunk (30 seconds by default) is kept.

## Where your data lives

Everything is stored in one folder: the database, audio, settings (including your API key) and the app window's browser profile.

| OS | Folder |
|---|---|
| Windows | `%APPDATA%\LectureRecorder` |
| macOS | `~/Library/Application Support/LectureRecorder` |
| Linux | `~/.local/share/lecturerecorder` |

Set `LECTURERECORDER_HOME` to use a different folder. Transcripts are sent to the Anthropic API only when you use an AI feature. The local server accepts connections only from this computer.

## How it works

- `lecturerecorder/__main__.py` starts a local FastAPI server on `127.0.0.1` and opens the app window.
- The browser records audio in chunks (30 s by default) and uploads each one. `transcriber.py` transcribes them in order on a background thread, passing the end of the previous chunk as context so sentences carry across chunks.
- `ai.py` calls Claude through the official `anthropic` SDK. Every feature shares one system prompt containing the transcript with a cache breakpoint, so follow-up questions on the same lecture reuse the cached transcript. Quizzes, topics and flashcards use structured JSON output. With Claude Opus 5, requests that a safety classifier declines are automatically retried on a fallback model (`fallbacks: "default"`).
- The UI is plain HTML, CSS and JavaScript in `lecturerecorder/static/` with no build step. Markdown, sanitizing and math come from vendored copies of marked, DOMPurify and KaTeX.
