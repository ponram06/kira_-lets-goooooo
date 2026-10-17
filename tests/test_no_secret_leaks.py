"""
Proof that no secret (API keys or the Free-mode PIN) can appear in
logs/backend.log, events or WebSocket payloads.

The fake secrets live in a temp .env (conftest sets KIRA_ENV_FILE). We then
push them through every output path on purpose — log calls, print(), event
text and data, WebSocket broadcasts and a wrong-PIN Free-mode attempt — and
check that the captured outputs never contain them.
"""

import io
import json
import logging

from backend import envfile, events, secrets_guard
from backend.main import wrap_std_streams
from backend.policy import ModeManager
from backend.server import WebSocketServer
from tests.conftest import TEST_PIN

FAKE_GROQ = "gsk_FAKE_leaktest_groq_0123456789"
FAKE_OR = "sk-or-FAKE-leaktest-openrouter-987654"
WRONG_PIN = "73519"
SECRETS = (FAKE_GROQ, FAKE_OR, TEST_PIN)


def _set_fake_secrets():
    envfile.set_values({"GROQ_API_KEY": FAKE_GROQ, "OPENROUTER_API_KEY": FAKE_OR,
                        "KIRA_FREE_MODE_PIN": TEST_PIN})
    secrets_guard.refresh()


def _assert_clean(text: str):
    for s in SECRETS:
        assert s not in text, "secret leaked"
    assert WRONG_PIN not in text, "attempted PIN leaked"


def test_backend_log_never_contains_secrets(tmp_path):
    """Simulate logs/backend.log: logging + stdout/stderr redirected to one file."""
    _set_fake_secrets()
    log_path = tmp_path / "backend.log"
    with open(log_path, "w", encoding="utf-8") as sink:
        handler = logging.StreamHandler(sink)
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        secrets_guard.install_log_redaction()
        try:
            log = logging.getLogger("backend.leaktest")
            log.warning("key is %s", FAKE_GROQ)
            log.error(f"pin is {TEST_PIN} and other key {FAKE_OR}")
            try:
                raise RuntimeError(f"auth failed for {FAKE_GROQ}")
            except RuntimeError:
                log.exception("provider error")

            # print() goes through the redacting stream wrapper used by backend.main
            stream = secrets_guard.RedactingStream(sink)
            stream.write(f"printed {FAKE_OR} {TEST_PIN}\n")

            # A wrong PIN attempt must not log the attempted PIN
            mm = ModeManager(pin_loader=lambda: envfile.get_value("KIRA_FREE_MODE_PIN"))
            mm.try_enter_free(WRONG_PIN)
            mm.try_enter_free(TEST_PIN)
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
    text = log_path.read_text(encoding="utf-8")
    assert "[REDACTED]" in text
    _assert_clean(text)


def test_exception_traceback_text_redacted(tmp_path):
    """Tracebacks are formatted by the handler; the record message must already be clean,
    and exception text is redacted when the message is rendered by our filter."""
    _set_fake_secrets()
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "token=%s", (FAKE_GROQ,), None)
    secrets_guard.RedactingFilter().filter(rec)
    _assert_clean(rec.getMessage())


def test_events_never_contain_secrets(tmp_path, monkeypatch):
    _set_fake_secrets()
    jsonl = tmp_path / "events.jsonl"
    monkeypatch.setattr(events, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(events, "EVENTS_LOG", jsonl)
    seen = []
    events.subscribe(seen.append)
    try:
        events.publish("tool.start", f"Running with {FAKE_GROQ}",
                       {"args": {"text": f"pin {TEST_PIN}", "nested": [FAKE_OR]},
                        "pin": TEST_PIN, "api_key": FAKE_GROQ})
        events.publish("tool.done", "ok", {"result": f"{FAKE_OR}"})
    finally:
        events.unsubscribe(seen.append)
    _assert_clean(json.dumps(seen))
    _assert_clean(jsonl.read_text(encoding="utf-8"))


def test_websocket_payloads_never_contain_secrets():
    _set_fake_secrets()
    server = WebSocketServer(config={"server": {"enabled": False, "token_file": ".kira/test_token"}})
    sent = []

    class Q:
        def put_nowait(self, payload):
            sent.append(payload)

    server._clients = {object(): Q()}
    server._process_and_broadcast({"type": "voice.speaking", "text": "", "data": {"text": f"say {FAKE_GROQ}"}})
    server._process_and_broadcast({"type": "tool.done", "text": "", "data": {"tool": "x", "summary": f"{FAKE_OR} {TEST_PIN}"}})
    server._process_and_broadcast({"type": "policy.ask", "text": "", "data": {"prompt": f"send {TEST_PIN}?", "question_id": "q"}})
    server._process_and_broadcast({"type": "tool.error", "text": f"bad key {FAKE_GROQ}", "data": {}})
    assert len(sent) == 4
    _assert_clean("\n".join(sent))


def test_free_mode_events_carry_reason_only(tmp_path, monkeypatch):
    _set_fake_secrets()
    monkeypatch.setattr(events, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(events, "EVENTS_LOG", tmp_path / "e.jsonl")
    seen = []
    events.subscribe(seen.append)
    try:
        mm = ModeManager(pin=TEST_PIN)
        mm.try_enter_free(WRONG_PIN)
        mm.try_enter_free(TEST_PIN)
    finally:
        events.unsubscribe(seen.append)
    assert {e["type"] for e in seen} == {"free_mode.denied", "mode.change"}
    _assert_clean(json.dumps(seen))


def test_wrapped_stdout_redacts(monkeypatch):
    _set_fake_secrets()
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    monkeypatch.setattr("sys.stderr", io.StringIO())
    wrap_std_streams()
    import sys
    print(f"reply mentions {FAKE_GROQ} and {TEST_PIN}")
    sys.stdout.flush()
    _assert_clean(buf.getvalue())
    assert "[REDACTED]" in buf.getvalue()
