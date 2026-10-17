You are the QA and security lead for this project. Read AGENTS.md, docs/PROGRESS.md and the whole codebase. Your job is to try to break it, prove what works, fix what does not, and produce an evidence-based report. Do not push to any remote. Never use real API keys, real messages, real accounts or real sites in automated tests.

PART A. AUTOMATED TEST SUITE (extend tests/ and the Electron tests)
1. Inventory every module and tool and list which are untested or weakly tested. Add tests until each tool and each policy branch has a normal case, an edge case, a failure case and a kill-switch case. Target 85% line coverage for policy.py, tools/, agent.py, providers.py and events.py (pytest-cov) and report the numbers.
2. Policy matrix test: generate every combination of tool x risk x mode x outside_workspace x affects_people x stop_flag x provider (cloud or ollama) and assert the exact decision (allow, ask, countdown, block) against a written table in docs/POLICY_TABLE.md. Any change in a decision is a failure.
3. Provider chaos tests: simulate 429, quota exhausted, 401, 403, 5xx, timeouts, connection loss, malformed JSON, tool-call arguments that are invalid JSON, a provider without tool support, and all providers failing. Assert cooldowns, fallback order, no key leakage in logs, and a graceful spoken message.
4. Agent loop tests with a scripted fake LLM: step cap (15), retry cap (2), loop detection (same failing call repeated), verification after each step, ask and countdown flows, cancel during countdown, kill switch mid-run, recovery after an error.
5. Voice tests with fakes: push-to-talk pause and resume, interruption, STT failure fallback, TTS failure fallback, silence, very long input, spoken stop word.
6. WebSocket tests: localhost-only binding, token auth required, bad tokens and bad origins rejected, message size limits, reconnection, event ordering, and no secrets in any event payload.
7. Electron tests: state-to-animation mapping, lip-sync mapping, expression blending, character switching, 3D-to-sprite fallback, config validation, position restore with a missing monitor, click-through hit-test logic, tray and menu command wiring.
8. Soak and leak tests: 1,000 simulated agent tasks through the event bus, 5,000 avatar state transitions and 20 character switches. Assert that memory growth stays flat within a small tolerance and that no timers, listeners or file handles leak.

PART B. SECURITY AND ABUSE TESTING (assume the LLM can be fooled or hostile)
Build a hostile fake LLM that obeys any injected instruction and emits dangerous tool calls. Prove that policy.py and the tools stop them even when the model misbehaves:
1. Path attacks: .. paths, absolute paths on other drives, UNC paths, mixed slashes, directory junctions, symlinks, 8.3 short names, trailing dots and spaces, alternate data streams, reserved device names (CON, NUL), very long paths, and unicode lookalikes.
2. Command attacks: chaining and redirection characters (& | > < ^ % ; and newlines), PowerShell and cmd tricks, environment variable expansion, encoded commands, quoting tricks, non-allow-listed executables, and arguments that smuggle a second command.
3. Prompt injection: instructions hidden in a web page, a file, the clipboard, a screenshot, an incoming-looking message, tool output and a contact name, telling the assistant to ignore its rules, switch to Free mode, send messages, delete files or reveal keys. Assert that nothing changes the mode, permissions or behavior.
4. Mode attacks: try to enter Free mode by a spoken phrase, a model tool call, an event message and the WebSocket. Each must need the deliberate confirmation. Also assert the auto-revert, the restart behavior and forced Restricted mode on the Ollama provider.
5. Messaging abuse: unknown or ambiguous contacts, multiple recipients, rapid repeated sends, very long messages, message text containing instructions, and any bulk-sending attempt. Assert the read-back, ask or countdown, the rate limit and the single-recipient rule.
6. Script-runner abuse: scripts that import os, subprocess, socket, ctypes or shutil, use eval or exec, open outside paths, loop forever, use huge memory or print huge output. Assert the static checks, timeout, output limit and working-directory limit. State clearly in the report that static checks are not a full sandbox.
7. Browser and screen-control abuse: password fields, payment pages, CAPTCHAs, login pages, downloads, form submissions, sensitive window titles (password managers, banking), and clicks outside the visible screen. Assert the pause-and-ask behavior and that credentials are never typed.
8. Secrets hygiene: scan the whole repo and its git history for keys and personal data (common key formats, phone numbers, usernames, absolute user paths). Confirm .gitignore coverage, redaction in logs and events, and that the settings window never shows a full key.
9. Dependencies: run pip-audit, npm audit, bandit and ruff and list findings by severity. Do not upgrade major versions without telling me.

PART C. PERFORMANCE AND RELIABILITY (target: integrated graphics, 16 GB RAM)
Write scripts under scripts/bench/ that measure and print: startup time, idle CPU and memory of the backend and Electron together, memory after a long idle (with a shortened simulated version for automated runs), voice end-to-end latency (end of speech to first spoken word, using event timestamps), tool latency, 3D avatar frame rate and frame time while idle, speaking and switching, and the WebGL-failure fallback. Report a table and flag anything above these limits: voice latency 3 s, 3D average below 24 fps, total idle memory above 1.5 GB.

PART D. HUMAN CHECKLIST (generate docs/MANUAL_TEST.md)
A printable checklist with expected results and a pass or fail box for each item, which I will run by hand:
1. The live demo scenario 10 times (target: 9 clean runs), including: folder already exists, VS Code already open, another window clicked mid-typing (must abort and type nothing), and Ctrl+Alt+K mid-run.
2. 20 spoken commands and questions (target: at least 19 understood), in quiet and noisy rooms.
3. Wi-Fi off: fallback to Ollama with a spoken notice, forced Restricted mode and screen control disabled. Wi-Fi back on: return to the cloud model.
4. Modes: Restricted asks and blocks correctly, Free needs confirmation, Free reverts after the timeout, indicator colors are correct.
5. Messaging: send a test message only to my own number or a test contact. Check the read-back, ask, countdown and cancel, and confirm that nothing is sent on cancel.
6. A browser task, a script task and a screen-control task (each with one deliberate failure), plus saving and replaying a learned skill.
7. Avatar: both characters (3D and sprite), persona switch, lip sync, expressions, tray, the small control button menu, drag with saved position, click-through on empty areas, sleep and wake, and quit leaving no stray processes.
8. Packaging: install on a clean user account or a second laptop, run the first-run wizard and the doctor, and confirm a clean uninstall.

PART E. FIX LOOP AND REPORT
1. For every failing test or bug found: reproduce it with a failing test first, fix the root cause (never by weakening a check or deleting a test), re-run the full suite, and commit with a clear message.
2. Produce docs/TEST_REPORT.md with: a summary table of all suites (pass, fail, skip counts), coverage numbers, the policy matrix result, security findings with severity, status (fixed or open) and evidence, performance numbers against the limits, known limitations, and a release-readiness verdict with the exact list of blockers.
3. Update README.md and AGENTS.md with the test commands (one command, scripts/test_all, that runs everything) and the current status.
4. Finish by printing the 10 biggest remaining risks, ranked, and what I should test by hand first.

ACCEPTANCE CRITERIA
All automated suites pass. The policy matrix matches the written table. The hostile-LLM suite shows zero policy bypasses. No secrets or personal data in the repo or its history. No high-severity audit findings left open. Coverage and performance limits are met, or reported as exceptions with reasons.

ADD TO PART B
10. App launcher and adapter abuse: app names, song names and message text containing quotes, & | ; % and newlines, URL schemes (file:, javascript:, ms-settings:), UNC paths and very long strings; requests to open denied apps; fake apps with lookalike names. Assert correct encoding, the deny-list in both modes, and no command execution.

ADD TO PART D
9. Open Spotify and play three different songs (one with a nonexistent name, which must be reported honestly); open WhatsApp and send hi plus a long message to a test contact; open Notepad, write and save a note; try to open an app that is not installed.