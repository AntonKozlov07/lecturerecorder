#!/usr/bin/env sh
# Lecture Recorder launcher for macOS and Linux. First run sets everything up.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo "Setting up Lecture Recorder for the first time..."
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -e .
fi
exec .venv/bin/python -m lecturerecorder "$@"
