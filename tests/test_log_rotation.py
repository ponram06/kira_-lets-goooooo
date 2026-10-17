"""
test_log_rotation.py
====================
Unit tests for size-capped log rotation in paths.py, audit.py, and events.py.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from backend import audit, events, paths


def test_rotate_log_file_rotates_and_keeps_backups(tmp_path: Path):
    log_file = tmp_path / "test.log"
    # Create file with 200 bytes
    log_file.write_bytes(b"A" * 200)

    # First rotation at max_bytes=100
    paths.rotate_log_file(log_file, max_bytes=100, backup_count=3)
    assert not log_file.exists()
    assert (tmp_path / "test.1.log").exists()
    assert (tmp_path / "test.1.log").stat().st_size == 200

    # Write new file with 250 bytes
    log_file.write_bytes(b"B" * 250)
    # Second rotation
    paths.rotate_log_file(log_file, max_bytes=100, backup_count=3)
    assert not log_file.exists()
    assert (tmp_path / "test.1.log").read_bytes() == b"B" * 250
    assert (tmp_path / "test.2.log").read_bytes() == b"A" * 200

    # Write third file with 300 bytes
    log_file.write_bytes(b"C" * 300)
    # Third rotation
    paths.rotate_log_file(log_file, max_bytes=100, backup_count=3)
    assert (tmp_path / "test.1.log").read_bytes() == b"C" * 300
    assert (tmp_path / "test.2.log").read_bytes() == b"B" * 250
    assert (tmp_path / "test.3.log").read_bytes() == b"A" * 200

    # Write fourth file with 400 bytes - backup_count=3 should drop the oldest (A)
    log_file.write_bytes(b"D" * 400)
    paths.rotate_log_file(log_file, max_bytes=100, backup_count=3)
    assert (tmp_path / "test.1.log").read_bytes() == b"D" * 400
    assert (tmp_path / "test.2.log").read_bytes() == b"C" * 300
    assert (tmp_path / "test.3.log").read_bytes() == b"B" * 250
    assert not (tmp_path / "test.4.log").exists()


def test_audit_log_rotation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_log = tmp_path / "actions.jsonl"
    monkeypatch.setattr(audit, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(audit, "ACTIONS_LOG", test_log)
    monkeypatch.setattr(audit, "MAX_LOG_BYTES", 150)  # low threshold
    monkeypatch.setattr(audit, "BACKUP_COUNT", 2)

    # First write
    audit.log_action("tool1", {"arg": "val1"}, "allow", "ok")
    assert test_log.exists()
    assert test_log.stat().st_size > 0

    # Force size over threshold
    test_log.write_text("X" * 200, encoding="utf-8")

    # Second write triggers rotation
    audit.log_action("tool2", {"arg": "val2"}, "allow", "ok")
    assert (tmp_path / "actions.1.jsonl").exists()
    assert (tmp_path / "actions.1.jsonl").read_text(encoding="utf-8") == "X" * 200

    new_content = test_log.read_text(encoding="utf-8").strip()
    data = json.loads(new_content)
    assert data["tool"] == "tool2"


def test_events_log_rotation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_log = tmp_path / "events.jsonl"
    monkeypatch.setattr(events, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(events, "EVENTS_LOG", test_log)
    monkeypatch.setattr(events, "MAX_LOG_BYTES", 150)
    monkeypatch.setattr(events, "BACKUP_COUNT", 2)

    events.publish("test.event1", "first event")
    assert test_log.exists()

    # Force size over threshold
    test_log.write_text("Y" * 200, encoding="utf-8")

    events.publish("test.event2", "second event")
    assert (tmp_path / "events.1.jsonl").exists()
    assert (tmp_path / "events.1.jsonl").read_text(encoding="utf-8") == "Y" * 200

    new_content = test_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(new_content) == 1
    data = json.loads(new_content[0])
    assert data["type"] == "test.event2"
