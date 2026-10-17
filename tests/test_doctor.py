"""
Doctor checks with a fake Probe (no network, no audio, no git calls).
"""

import json
from pathlib import Path

from backend import doctor
from tests.conftest import TEST_PIN

FAKE_KEY = "gsk_FAKE_doctor_key_abcdef123456"

CFG = {
    "providers": [
        {"name": "groq", "base_url": "https://x/v1", "api_key_env": "GROQ_API_KEY", "model": "m"},
        {"name": "ollama", "base_url": "http://localhost:11434/v1", "api_key_env": None, "model": "qwen2.5:7b"},
    ],
    "doctor": {"internet_check_url": "https://example.invalid/", "timeout": 1, "code_command": "code"},
}


class FakeProbe(doctor.Probe):
    def __init__(self, **kw):
        self.version = kw.get("version", (3, 11, 9))
        self.missing = kw.get("missing", set())
        self.code = kw.get("code", True)
        self.ollama = kw.get("ollama", {"models": [{"name": "qwen2.5:7b"}]})
        self.mic = kw.get("mic", "Microphone Array")
        self.env = kw.get("env", {"GROQ_API_KEY": FAKE_KEY, "KIRA_FREE_MODE_PIN": TEST_PIN})
        self.ignored = kw.get("ignored", {".env": True, "logs/backend.log": True})
        self.logs = kw.get("logs", {})
        self.online = kw.get("online", True)

    def python_version(self): return self.version
    def package_version(self, dist): return None if dist in self.missing else "1.0"
    def which(self, cmd): return "C:/code.cmd" if self.code else None

    def http_get_json(self, url, timeout):
        if self.ollama is None:
            raise ConnectionError("down")
        return self.ollama

    def http_head(self, url, timeout):
        if not self.online:
            raise ConnectionError("offline")
        return 204

    def default_input_device(self): return self.mic
    def env_value(self, name): return self.env.get(name, "")
    def git_ignored(self, rel): return self.ignored.get(rel)
    def log_files(self): return [Path(n) for n in self.logs]
    def read_tail(self, path): return self.logs[path.name]
    def secret_values(self): return [v for v in self.env.values() if len(v) >= 4]


def _by_name(checks):
    return {c.name: c for c in checks}


def test_all_good():
    checks = doctor.run_checks(FakeProbe(), CFG)
    assert not doctor.has_required_failure(checks)
    assert all(c.status == "ok" for c in checks), [c for c in checks if c.status != "ok"]


def test_failures_have_fixes():
    probe = FakeProbe(version=(3, 10, 0), missing={"pyyaml"}, code=False, ollama=None, mic=None,
                      env={}, ignored={".env": False, "logs/backend.log": True}, online=False)
    checks = _by_name(doctor.run_checks(probe, CFG))
    assert checks["Python"].status == "fail"
    assert checks["Packages"].status == "fail" and "pyyaml" in checks["Packages"].detail
    assert checks["VS Code command"].status == "fail"
    assert checks["Ollama"].status == "warn"
    assert checks["Microphone"].status == "warn"
    assert checks["LLM provider"].status == "fail"
    assert checks["Git ignore"].status == "fail" and ".env" in checks["Git ignore"].detail
    assert checks["Internet"].status == "warn"
    for c in checks.values():
        if c.status != "ok":
            assert c.fix, c.name


def test_ollama_model_not_pulled():
    checks = _by_name(doctor.run_checks(FakeProbe(ollama={"models": [{"name": "llama3"}]}), CFG))
    assert checks["Ollama"].status == "warn" and "ollama pull qwen2.5:7b" in checks["Ollama"].fix


def test_pin_missing_says_free_mode_disabled():
    checks = _by_name(doctor.run_checks(FakeProbe(env={"GROQ_API_KEY": FAKE_KEY}), CFG))
    pin = checks["Free-mode PIN"]
    assert pin.status == "warn" and "Free mode disabled" in pin.detail


def test_secret_in_logs_detected_without_printing_it():
    probe = FakeProbe(logs={"backend.log": f"oops {FAKE_KEY} here", "events.jsonl": "clean"})
    checks = doctor.run_checks(probe, CFG)
    logs = _by_name(checks)["Logs"]
    assert logs.status == "fail" and "backend.log" in logs.detail
    report = doctor.format_report(checks)
    assert FAKE_KEY not in report and TEST_PIN not in report


def test_report_never_contains_secret_values():
    checks = doctor.run_checks(FakeProbe(), CFG)
    blob = doctor.format_report(checks) + json.dumps([c.__dict__ for c in checks])
    assert FAKE_KEY not in blob and TEST_PIN not in blob


def test_required_packages_parsing(tmp_path):
    req = tmp_path / "r.txt"
    req.write_text("# c\nopenai>=1.0\npython-dotenv==1.0.0  # note\n\n-e .\nfaster-whisper>=1\n")
    assert doctor.required_packages(req) == ["openai", "python-dotenv", "faster-whisper"]
