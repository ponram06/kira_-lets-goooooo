"""
doctor.py
=========
Environment health check:  python -m backend.doctor  [--json]

Checks Python, packages, the `code` command, Ollama and its model, the
microphone, provider keys and the Free-mode PIN (presence only — values are
never printed), that .env and logs/ are git-ignored, that no key or PIN has
leaked into logs/, and internet access. Every problem comes with a fix.

Exit code 1 if a required check fails. All system access goes through the
``Probe`` class so tests can replace it with a fake.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from backend import envfile, secrets_guard
from backend.config_loader import load_config
from backend import paths

ROOT = Path(__file__).parent.parent
_LOG_SCAN_BYTES = 5 * 1024 * 1024      # scan at most the last 5 MB of each log


@dataclass
class Check:
    name: str
    status: str            # "ok" | "warn" | "fail"
    detail: str
    fix: str = ""
    required: bool = True


class Probe:
    """Real system access. Tests pass a subclass with canned answers."""

    def python_version(self) -> tuple[int, int, int]:
        return sys.version_info[:3]

    def package_version(self, dist: str) -> Optional[str]:
        try:
            return importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            return None

    def which(self, cmd: str) -> Optional[str]:
        return shutil.which(cmd)

    def http_get_json(self, url: str, timeout: float) -> Any:
        import httpx
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def http_head(self, url: str, timeout: float) -> int:
        import httpx
        return httpx.head(url, timeout=timeout, follow_redirects=False).status_code

    def default_input_device(self) -> Optional[str]:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
        return str(dev.get("name")) if dev else None

    def env_value(self, name: str) -> str:
        return envfile.get_value(name)

    def git_ignored(self, rel_path: str) -> Optional[bool]:
        """True/False from `git check-ignore`; None if git is unavailable."""
        try:
            res = subprocess.run(
                ["git", "check-ignore", "-q", rel_path],
                cwd=str(ROOT), capture_output=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode in (0, 1):
            return res.returncode == 0
        return None

    def log_files(self) -> list[Path]:
        logs = paths.logs_dir()
        return [p for p in logs.rglob("*") if p.is_file()] if logs.is_dir() else []

    def read_tail(self, path: Path) -> str:
        with open(path, "rb") as f:
            try:
                f.seek(-_LOG_SCAN_BYTES, 2)
            except OSError:
                f.seek(0)
            return f.read().decode("utf-8", errors="ignore")

    def secret_values(self) -> list[str]:
        secrets_guard.refresh()
        return secrets_guard.known_secrets()


# ── requirement parsing ─────────────────────────────────────────────────────

def required_packages(req_file: Path = ROOT / "requirements.txt") -> list[str]:
    """Distribution names from requirements.txt (comments and markers ignored)."""
    names: list[str] = []
    try:
        lines = req_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return names
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m:
            names.append(m.group(1))
    return names


# ── individual checks ───────────────────────────────────────────────────────

def check_python(p: Probe) -> Check:
    v = p.python_version()
    text = ".".join(map(str, v))
    if tuple(v[:2]) >= (3, 11):
        return Check("Python", "ok", f"Python {text}")
    return Check("Python", "fail", f"Python {text} is too old",
                 "Install Python 3.11 or newer and recreate .venv.")


def check_packages(p: Probe, packages: list[str]) -> Check:
    missing = [d for d in packages if p.package_version(d) is None]
    if not missing:
        return Check("Packages", "ok", f"{len(packages)} required packages installed")
    return Check("Packages", "fail", "Missing: " + ", ".join(missing),
                 r"Run: .venv\Scripts\python.exe -m pip install -r requirements.txt")


def check_code_command(p: Probe, cmd: str) -> Check:
    if p.which(cmd):
        return Check("VS Code command", "ok", f"'{cmd}' found on PATH")
    return Check("VS Code command", "fail", f"'{cmd}' is not on PATH",
                 "In VS Code: Ctrl+Shift+P → 'Shell Command: Install code command in PATH', "
                 "or reinstall VS Code with 'Add to PATH' ticked, then open a new terminal.")


def _ollama_provider(cfg: dict) -> Optional[dict]:
    for prov in cfg.get("providers", []):
        if prov.get("name") == "ollama":
            return prov
    return None


def check_ollama(p: Probe, cfg: dict, timeout: float) -> Check:
    prov = _ollama_provider(cfg)
    if not prov:
        return Check("Ollama", "ok", "not configured (skipped)", required=False)
    base = str(prov.get("base_url", "")).rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    model = str(prov.get("model", ""))
    try:
        data = p.http_get_json(f"{base}/api/tags", timeout)
    except Exception:
        return Check("Ollama", "warn", "not running (local fallback unavailable)",
                     "Install Ollama from ollama.com and start it, or ignore if you only use cloud providers.",
                     required=False)
    names = {str(m.get("name", "")) for m in (data or {}).get("models", [])}
    if model in names or f"{model}:latest" in names:
        return Check("Ollama", "ok", f"running, model '{model}' pulled", required=False)
    return Check("Ollama", "warn", f"running, but model '{model}' is not pulled",
                 f"Run: ollama pull {model}", required=False)


def check_microphone(p: Probe) -> Check:
    try:
        name = p.default_input_device()
    except Exception:
        name = None
    if name:
        return Check("Microphone", "ok", f"default input: {name[:60]}")
    return Check("Microphone", "warn", "no input device found",
                 "Plug in or enable a microphone (Windows Settings → Sound → Input). "
                 "You can still type in the terminal.", required=False)


def check_keys(p: Probe, cfg: dict, ollama_ok: bool) -> list[Check]:
    results: list[Check] = []
    any_key = False
    for prov in cfg.get("providers", []):
        env = prov.get("api_key_env")
        if not env:
            continue
        if p.env_value(env):
            any_key = True
            results.append(Check(f"Key {env}", "ok", "set (hidden)", required=False))
        else:
            results.append(Check(f"Key {env}", "warn", "not set",
                                 f"Add {env} in Settings (or .env) to use {prov.get('name')}.",
                                 required=False))
    if not any_key and not ollama_ok:
        results.append(Check("LLM provider", "fail", "no cloud key set and Ollama not available",
                             "Add at least one API key in Settings, or start Ollama."))
    return results


def check_pin(p: Probe) -> Check:
    pin = p.env_value(secrets_guard.PIN_ENV)
    if not pin:
        return Check("Free-mode PIN", "warn", f"{secrets_guard.PIN_ENV} not set — Free mode disabled",
                     "Set a PIN in Settings (or add KIRA_FREE_MODE_PIN to .env) if you want Free mode.",
                     required=False)
    if not (pin.isdigit() and 4 <= len(pin) <= 12):
        return Check("Free-mode PIN", "warn", "set (hidden), but not 4-12 digits",
                     "Choose a PIN of 4 to 12 digits in Settings.", required=False)
    return Check("Free-mode PIN", "ok", "set (hidden)", required=False)


def check_gitignore(p: Probe) -> Check:
    bad = []
    for rel in (".env", "logs/backend.log"):
        ignored = p.git_ignored(rel)
        if ignored is None:
            return Check("Git ignore", "warn", "git not available — could not verify",
                         "Make sure .gitignore lists .env and logs/.", required=False)
        if not ignored:
            bad.append(rel.split("/")[0] + ("/" if "/" in rel else ""))
    if not bad:
        return Check("Git ignore", "ok", ".env and logs/ are git-ignored")
    return Check("Git ignore", "fail", "not ignored: " + ", ".join(bad),
                 "Add these lines to .gitignore: " + "  ".join(bad))


def check_log_leaks(p: Probe) -> Check:
    secrets = p.secret_values()
    if not secrets:
        return Check("Logs", "ok", "no secrets configured to look for")
    leaked: list[str] = []
    for path in p.log_files():
        try:
            text = p.read_tail(path)
        except OSError:
            continue
        if any(secrets_guard.secret_in_text(s, text) for s in secrets):
            leaked.append(path.name)
    if not leaked:
        return Check("Logs", "ok", "no key or PIN found in logs/")
    return Check("Logs", "fail", f"a key or PIN appears in {len(leaked)} log file(s): " + ", ".join(leaked[:5]),
                 "Delete those files in logs/ and change the affected key or PIN.")


def check_internet(p: Probe, url: str, timeout: float) -> Check:
    try:
        p.http_head(url, timeout)
        return Check("Internet", "ok", "reachable", required=False)
    except Exception:
        return Check("Internet", "warn", "not reachable — cloud providers and Edge TTS will fail",
                     "Check your connection or proxy. Ollama and pyttsx3 still work offline.",
                     required=False)


# ── runner ──────────────────────────────────────────────────────────────────

def run_checks(probe: Optional[Probe] = None, cfg: Optional[dict] = None) -> list[Check]:
    p = probe or Probe()
    cfg = cfg if cfg is not None else load_config()
    dcfg = cfg.get("doctor", {}) or {}
    timeout = float(dcfg.get("timeout", 4))

    ollama = check_ollama(p, cfg, timeout)
    checks = [
        check_python(p),
        check_packages(p, required_packages()),
        check_code_command(p, str(dcfg.get("code_command", "code"))),
        ollama,
        check_microphone(p),
        *check_keys(p, cfg, ollama_ok=(ollama.status == "ok" and "skipped" not in ollama.detail)),
        check_pin(p),
        check_gitignore(p),
        check_log_leaks(p),
        check_internet(p, str(dcfg.get("internet_check_url", "https://www.google.com/generate_204")), timeout),
    ]
    return checks


def has_required_failure(checks: list[Check]) -> bool:
    return any(c.status == "fail" and c.required for c in checks)


def format_report(checks: list[Check]) -> str:
    tags = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    lines = ["Kira doctor"]
    for c in checks:
        lines.append(f"  {tags.get(c.status, '[ ?? ]')} {c.name}: {c.detail}")
        if c.status != "ok" and c.fix:
            lines.append(f"         Fix: {c.fix}")
    lines.append("")
    lines.append("Result: " + ("problems found (see FAIL above)" if has_required_failure(checks)
                                else "ready" + (" (with warnings)" if any(c.status == "warn" for c in checks) else "")))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.doctor")
    parser.add_argument("--json", action="store_true", help="machine-readable output (Settings window)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    checks = run_checks()
    if args.json:
        print(json.dumps({"checks": [asdict(c) for c in checks], "ok": not has_required_failure(checks)}))
    else:
        print(format_report(checks))
    return 1 if has_required_failure(checks) else 0


if __name__ == "__main__":
    sys.exit(main())
