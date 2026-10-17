"""
app_index.py
============
Universal application indexer, local fuzzy resolver, and cache manager.

Discovers installed Windows applications from:
1. Current user Start Menu (.lnk shortcuts)
2. All users / ProgramData Start Menu (.lnk shortcuts)
3. Windows Store / UWP apps via Get-StartApps

Security & Privacy Rules:
- Indexing is 100% read-only and local.
- Never sends full app lists to cloud models.
- Never dumps the full index or paths into logs (summary counts only).
- At most 3 candidate names may reach the model or user on ambiguity/miss.
- Ambiguity asks one candidate at a time with yes/no. Never guesses.
- On miss: refreshes once and retries before declaring not found.
- On refresh failure: retains existing cache.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from backend.app_security import (
    ShortcutInfo,
    check_denylist,
    check_script_or_interpreter,
    resolve_shortcut,
)

logger = logging.getLogger(__name__)

# Aliases for common applications
DEFAULT_ALIASES: dict[str, list[str]] = {
    "visual studio code": ["code", "vscode", "vs code"],
    "google chrome": ["chrome", "google-chrome"],
    "mozilla firefox": ["firefox"],
    "microsoft edge": ["edge", "msedge"],
    "spotify": ["spotify music"],
    "whatsapp": ["whatsapp desktop"],
    "notepad": ["editor"],
    "calculator": ["calc"],
    "file explorer": ["explorer", "files"],
}


@dataclass(frozen=True)
class IndexedApp:
    name: str
    clean_name: str
    target_path: str
    shortcut_path: str
    arguments: str
    is_store_app: bool
    app_id: str
    run_as_admin: bool
    aliases: tuple[str, ...]

    def display_label(self) -> str:
        return self.name


@dataclass(frozen=True)
class ResolveResult:
    status: str  # "found", "ambiguous", "weak_match", "not_found"
    match: Optional[IndexedApp]
    candidates: list[IndexedApp]  # at most 3
    message: str


def _clean_str(s: str) -> str:
    """Normalize string for fuzzy comparison: lowercased alphanumeric."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


class AppIndex:
    """Thread-safe index of installed applications with local fuzzy resolution."""

    def __init__(self, ps_timeout: float = 15.0) -> None:
        self._ps_timeout = ps_timeout
        self._cache: list[IndexedApp] = []
        self._last_refresh: float = 0.0
        self._lock = threading.Lock()

    def get_indexed_count(self) -> int:
        with self._lock:
            return len(self._cache)

    def _get_start_menu_paths(self) -> list[Path]:
        """Locate user and system Start Menu Programs directories."""
        paths = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            user_sm = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            if user_sm.is_dir():
                paths.append(user_sm)

        programdata = os.environ.get("PROGRAMDATA")
        if programdata:
            sys_sm = Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            if sys_sm.is_dir():
                paths.append(sys_sm)

        return paths

    def _query_store_apps_powershell(self) -> list[dict]:
        """Execute fixed PowerShell command with a 15 s timeout to get Store apps."""
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-StartApps | ConvertTo-Json -Depth 2",
        ]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._ps_timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                if isinstance(data, list):
                    return [item for item in data if isinstance(item, dict)]
                elif isinstance(data, dict):
                    return [data]
        except subprocess.TimeoutExpired:
            logger.warning("Get-StartApps PowerShell query timed out after %s s", self._ps_timeout)
        except Exception as exc:
            logger.warning("Get-StartApps query failed (%s)", exc)
        return []

    def refresh(self) -> bool:
        """
        Scan shortcuts and Store apps synchronously.
        If scanning fails, retains existing cache.
        Deduplicates by normalized target path and AppID.
        """
        try:
            discovered: list[IndexedApp] = []
            seen_targets: set[str] = set()

            # 1. Start Menu Shortcuts (.lnk)
            for sm_root in self._get_start_menu_paths():
                try:
                    for lnk_file in sm_root.rglob("*.lnk"):
                        try:
                            info = resolve_shortcut(lnk_file)
                            if not info.is_valid or not info.target_path:
                                continue

                            # Normalize target for deduplication
                            norm_target = str(Path(info.target_path).resolve()).lower()
                            if norm_target in seen_targets:
                                continue
                            seen_targets.add(norm_target)

                            name = lnk_file.stem
                            clean_name = _clean_str(name)

                            # Build aliases
                            aliases = []
                            for default_name, alias_list in DEFAULT_ALIASES.items():
                                if _clean_str(default_name) in clean_name or clean_name in _clean_str(default_name):
                                    aliases.extend(alias_list)

                            discovered.append(
                                IndexedApp(
                                    name=name,
                                    clean_name=clean_name,
                                    target_path=info.target_path,
                                    shortcut_path=str(lnk_file),
                                    arguments=info.arguments,
                                    is_store_app=False,
                                    app_id="",
                                    run_as_admin=info.run_as_admin,
                                    aliases=tuple(set(aliases)),
                                )
                            )
                        except Exception as item_exc:
                            logger.debug("Error indexing shortcut %s: %s", lnk_file, item_exc)
                except Exception as dir_exc:
                    logger.debug("Error scanning directory %s: %s", sm_root, dir_exc)

            # 2. Windows Store Apps
            store_apps = self._query_store_apps_powershell()
            for sa in store_apps:
                raw_name = str(sa.get("Name", "")).strip()
                app_id = str(sa.get("AppID", "")).strip()
                if not raw_name or not app_id:
                    continue

                norm_id = app_id.lower()
                if norm_id in seen_targets:
                    continue
                seen_targets.add(norm_id)

                clean_name = _clean_str(raw_name)
                aliases = []
                for default_name, alias_list in DEFAULT_ALIASES.items():
                    if _clean_str(default_name) in clean_name or clean_name in _clean_str(default_name):
                        aliases.extend(alias_list)

                discovered.append(
                    IndexedApp(
                        name=raw_name,
                        clean_name=clean_name,
                        target_path="",
                        shortcut_path="",
                        arguments="",
                        is_store_app=True,
                        app_id=app_id,
                        run_as_admin=False,
                        aliases=tuple(set(aliases)),
                    )
                )

            with self._lock:
                self._cache = discovered
                self._last_refresh = time.time()
                logger.info("App index refreshed: %d applications indexed.", len(self._cache))
                return True

        except Exception as exc:
            with self._lock:
                logger.error("App index refresh failed (%s); keeping %d cached apps.", exc, len(self._cache))
            return False

    async def refresh_async(self) -> bool:
        """Run refresh off the event loop."""
        return await asyncio.to_thread(self.refresh)

    def resolve(self, query: str, auto_retry: bool = True) -> ResolveResult:
        """
        Locally resolve an app query using exact and fuzzy matching.
        Never guesses: weak matches or ambiguous pairs ask the user.
        At most 3 candidates are ever returned.
        """
        clean_q = _clean_str(query)
        if not clean_q:
            return ResolveResult("not_found", None, [], "Application name cannot be empty.")

        with self._lock:
            apps = list(self._cache)

        # If cache is empty, refresh first
        if not apps and auto_retry:
            self.refresh()
            with self._lock:
                apps = list(self._cache)

        # 1. Exact match pass
        for app in apps:
            if app.clean_name == clean_q:
                return ResolveResult("found", app, [app], f"Found exact match: {app.name}")
            for alias in app.aliases:
                if _clean_str(alias) == clean_q:
                    return ResolveResult("found", app, [app], f"Found alias match: {app.name}")

        # 2. Fuzzy scoring
        scored: list[tuple[float, IndexedApp]] = []
        for app in apps:
            best_score = 0.0

            # Score against clean name
            ratio = difflib.SequenceMatcher(None, clean_q, app.clean_name).ratio()
            if clean_q in app.clean_name or app.clean_name in clean_q:
                # Substring boost
                best_score = max(best_score, 0.85 + 0.15 * ratio)
            else:
                best_score = max(best_score, ratio)

            # Score against aliases
            for alias in app.aliases:
                clean_alias = _clean_str(alias)
                alias_ratio = difflib.SequenceMatcher(None, clean_q, clean_alias).ratio()
                if clean_q in clean_alias:
                    best_score = max(best_score, 0.90)
                else:
                    best_score = max(best_score, alias_ratio)

            if best_score > 0.40:
                scored.append((best_score, app))

        scored.sort(key=lambda x: x[0], reverse=True)

        # If no candidates found and auto_retry is requested, refresh once and retry
        if not scored and auto_retry:
            logger.info("App query '%s' not found; refreshing index once and retrying.", query)
            self.refresh()
            return self.resolve(query, auto_retry=False)

        if not scored:
            return ResolveResult(
                "not_found",
                None,
                [],
                f"Application '{query}' not found. No matching applications installed.",
            )

        top_score, top_app = scored[0]
        runner_up_score = scored[1][0] if len(scored) > 1 else 0.0

        # Strong confident match
        if top_score >= 0.85 and (top_score - runner_up_score) >= 0.15:
            return ResolveResult("found", top_app, [top_app], f"Resolved '{query}' to {top_app.name}")

        # Ambiguous match (two close top candidates)
        if top_score >= 0.60 and (top_score - runner_up_score) < 0.15 and len(scored) >= 2:
            candidates = [s[1] for s in scored[:3]]
            # Ask one candidate at a time with yes/no (never guess!)
            msg = f"Did you mean {top_app.name}? (yes/no)"
            return ResolveResult("ambiguous", None, candidates, msg)

        # Weak match (below threshold)
        candidates = [s[1] for s in scored[:3]]
        candidate_names = ", ".join(f"'{c.name}'" for c in candidates)
        return ResolveResult(
            "weak_match",
            None,
            candidates,
            f"Application '{query}' not found. Did you mean {candidate_names}?",
        )


# Global singleton index
APP_INDEX = AppIndex()
