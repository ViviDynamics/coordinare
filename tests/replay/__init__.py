"""Offline replays of recorded environment-bootstrap model replies (issue 376).

A fixture under ``tests/fixtures/replay/<run>/<case>.json`` holds redacted replies taken from an acceptance run by
``scripts/replay_extract.py``. ``ReplayTransport`` plays them to the native runtime in order and stops the replay
when the harness asks for something the recording never answered; other seams feed the recorded candidates to the
domain code directly.
"""
