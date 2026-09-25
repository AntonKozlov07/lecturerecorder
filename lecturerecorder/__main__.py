"""Start the app: run the local server and open it in its own window.

The window is Chrome, Edge, Chromium or Brave in "app mode" (no tabs or address
bar) with a dedicated profile, so it behaves like a desktop app and remembers
the microphone permission. Closing the window quits the app. If none of those
browsers is installed it falls back to the default browser.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from .config import DATA_DIR, load_settings


def find_app_browser() -> str | None:
    candidates: list[str] = []
    if sys.platform == "win32":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates += [
                    rf"{base}\Google\Chrome\Application\chrome.exe",
                    rf"{base}\Microsoft\Edge\Application\msedge.exe",
                    rf"{base}\BraveSoftware\Brave-Browser\Application\brave.exe",
                ]
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                     "microsoft-edge", "brave-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    return next((c for c in candidates if Path(c).exists()), None)


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free port")


def wait_until_up(url: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url + "api/status", timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("Server did not start")


def selftest() -> None:
    """Used by the build to prove the packaged app has everything it needs."""
    import anthropic
    import av
    import ctranslate2
    import faster_whisper
    import docx
    import onnxruntime
    import pptx
    import pypdf
    import qrcode
    from faster_whisper.vad import get_vad_model

    from .phone import _ensure_certs, _qr_svg

    from .server import STATIC, app  # noqa: F401

    assert (STATIC / "index.html").exists(), "UI files missing"
    assert (STATIC / "vendor" / "katex" / "katex.min.js").exists(), "vendor files missing"
    get_vad_model()  # loads the bundled voice-activity model through onnxruntime
    docx.Document()          # needs python-docx's bundled template
    pptx.Presentation()      # needs python-pptx's bundled template
    assert "<svg" in _qr_svg("https://example.com")
    _ensure_certs(["192.168.0.10"])
    print(f"documents ok: pypdf {pypdf.__version__}, qrcode, certificates")
    print(f"selftest ok: anthropic {anthropic.__version__}, faster-whisper {faster_whisper.__version__}, "
          f"ctranslate2 {ctranslate2.__version__}, av {av.__version__}, onnxruntime {onnxruntime.__version__}, "
          f"cuda devices {ctranslate2.get_cuda_device_count()}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="lecturerecorder", description="Record lectures and study them with AI.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-window", action="store_true", help="Only run the server; open the URL yourself.")
    parser.add_argument("--browser", action="store_true", help="Open in your default browser instead of an app window.")
    parser.add_argument("--selftest", action="store_true", help="Check that all components load, then exit.")
    args = parser.parse_args()

    if sys.stdout is None or sys.stderr is None:
        # The packaged Windows app has no console; keep a log file instead.
        log_file = open(DATA_DIR / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log_file

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.selftest:
        selftest()
        return

    from . import db
    from .server import app
    from .transcriber import transcriber

    db.recover_after_restart()
    transcriber.warm_up()
    transcriber.requeue_pending()

    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until_up(url)
    if load_settings()["phone_enabled"]:
        from . import phone
        threading.Thread(target=phone.start, name="phone-start", daemon=True).start()
    print(f"Lecture Recorder is running at {url}")
    print(f"Data folder: {DATA_DIR}")

    browser = None if (args.no_window or args.browser) else find_app_browser()
    if browser:
        profile = DATA_DIR / "window-profile"
        proc = subprocess.Popen([
            browser, f"--app={url}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--window-size=1320,860",
        ])
        print("Close the app window (or press Ctrl+C here) to quit.")
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
    else:
        if not args.no_window:
            webbrowser.open(url)
        print("Press Ctrl+C to quit.")
        try:
            while thread.is_alive():
                thread.join(1)
        except KeyboardInterrupt:
            pass

    server.should_exit = True
    thread.join(5)


if __name__ == "__main__":
    main()
