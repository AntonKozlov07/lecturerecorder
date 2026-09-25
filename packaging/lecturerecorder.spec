# PyInstaller build for the desktop app. Run from the repo root:
#   pyinstaller packaging/lecturerecorder.spec --noconfirm
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [(os.path.join(ROOT, "lecturerecorder", "static"), os.path.join("lecturerecorder", "static"))]
binaries = []
hiddenimports = collect_submodules("uvicorn") + collect_submodules("lecturerecorder")

# Speech-to-text stack: native libraries, the bundled VAD model and tokenizer data.
for package in ("faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LectureRecorder",
    icon=os.path.join(SPECPATH, "icon.ico"),
    console=False,  # a desktop app: no black console window; logs go to app.log
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="LectureRecorder", upx=False)
