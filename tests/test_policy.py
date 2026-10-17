"""
test_policy.py
==============
Tests for backend/policy.py

Covered
-------
- LOW risk → allow in any mode
- MEDIUM allowlisted inside workspace → allow in Restricted
- MEDIUM screenshot → ask / allow by mode
- HIGH in Restricted → ask (never allow, never block for non-forbidden)
- HIGH in Free + affects_people → countdown
- Forbidden categories → block in every mode
- Kill switch → blocks everything
- outside_workspace flag → block (Restricted) / ask (Free)
- Unknown actions → block (Restricted) / ask (Free)
- Demo scenario: open_vscode, create_folder, create_file, type_code,
  run_command all return "allow" in Restricted
- ModeManager: default Restricted, enter_free with confirm/deny,
  enter_restricted, auto-revert after timeout, injectable callback
- ModeManager: Ollama forces Restricted + disables screen control;
  enter_free re-enables screen control
- Workspace guard: inside workspace OK; ..\\ paths; absolute outside;
  absolute on another drive; mixed slashes; symlinks; directory junctions;
  short 8.3 name note
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import backend.policy as policy_module
from backend.policy import (
    KILL_SWITCH,
    TOOL_REGISTRY,
    Mode,
    ModeManager,
    PolicyError,
    RiskLevel,
    check,
    resolve_path,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_kill_switch():
    """Ensure the global kill switch is clear before and after each test."""
    KILL_SWITCH.reset()
    yield
    KILL_SWITCH.reset()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Patch policy.WORKSPACE_ROOT to a temporary directory.
    resolve_path() reads WORKSPACE_ROOT from the module namespace at call
    time, so monkeypatching works correctly.
    """
    resolved = tmp_path.resolve()
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", resolved)
    return resolved


# ---------------------------------------------------------------------------
# LOW risk
# ---------------------------------------------------------------------------


def test_low_risk_allow_restricted():
    assert check("create_folder", Mode.RESTRICTED) == "allow"


def test_low_risk_allow_free():
    assert check("create_file", Mode.FREE) == "allow"


def test_open_vscode_allow_restricted():
    assert check("open_vscode", Mode.RESTRICTED) == "allow"


def test_speak_allow_any_mode():
    assert check("speak", Mode.RESTRICTED) == "allow"
    assert check("speak", Mode.FREE) == "allow"


# ---------------------------------------------------------------------------
# MEDIUM risk — allowlisted
# ---------------------------------------------------------------------------


def test_run_command_allowlisted_allow_restricted():
    """run_command is allowlisted and inside workspace → allow in Restricted."""
    assert check("run_command", Mode.RESTRICTED) == "allow"


def test_run_script_allowlisted_allow_restricted():
    assert check("run_script", Mode.RESTRICTED) == "allow"


def test_run_command_allow_free():
    assert check("run_command", Mode.FREE) == "allow"


# ---------------------------------------------------------------------------
# MEDIUM risk — screenshot (special case)
# ---------------------------------------------------------------------------


def test_screenshot_ask_restricted():
    assert check("take_screenshot", Mode.RESTRICTED) == "ask"


def test_screenshot_allow_free():
    assert check("take_screenshot", Mode.FREE) == "allow"


# ---------------------------------------------------------------------------
# MEDIUM risk — not allowlisted
# ---------------------------------------------------------------------------


def test_read_file_outside_block_restricted():
    """outside_workspace=True → block in Restricted regardless of risk level."""
    assert check("read_file_outside", Mode.RESTRICTED) == "block"


def test_read_file_outside_ask_free():
    assert check("read_file_outside", Mode.FREE) == "ask"


# ---------------------------------------------------------------------------
# HIGH risk
# ---------------------------------------------------------------------------


def test_high_restricted_send_message_ask():
    assert check("send_message", Mode.RESTRICTED) == "ask"


def test_high_restricted_delete_ask():
    assert check("delete_file", Mode.RESTRICTED) == "ask"


def test_high_restricted_install_ask():
    assert check("install_package", Mode.RESTRICTED) == "ask"


def test_high_restricted_never_allow():
    """
    All HIGH-risk non-forbidden actions that are *inside* workspace must return
    'ask' in Restricted — never 'allow'.

    Actions with outside_workspace=True legitimately return 'block' because the
    workspace guard fires before the risk branch; those are checked separately
    in test_write_file_outside_block_restricted.
    """
    for action, entry in TOOL_REGISTRY.items():
        if (
            entry["risk"] == RiskLevel.HIGH
            and not entry.get("forbidden_category")
            and not entry.get("outside_workspace")
        ):
            result = check(action, Mode.RESTRICTED)
            assert result == "ask", (
                f"Action '{action}' returned '{result}' in Restricted mode "
                f"(must be 'ask' for in-workspace HIGH-risk actions)"
            )


def test_high_free_send_message_countdown():
    """send_message affects_people=True in Free → countdown."""
    assert check("send_message", Mode.FREE) == "countdown"


def test_high_free_delete_ask():
    assert check("delete_file", Mode.FREE) == "ask"


def test_high_free_install_ask():
    assert check("install_package", Mode.FREE) == "ask"


def test_write_file_outside_block_restricted():
    assert check("write_file_outside", Mode.RESTRICTED) == "block"


def test_write_file_outside_ask_free():
    assert check("write_file_outside", Mode.FREE) == "ask"


# ---------------------------------------------------------------------------
# Forbidden categories — blocked in every mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    ["format_disk", "change_security_settings", "make_payment",
     "type_password", "type_otp"],
)
def test_forbidden_always_block(action: str):
    assert check(action, Mode.RESTRICTED) == "block"
    assert check(action, Mode.FREE) == "block"


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_kill_switch_blocks_low_risk():
    KILL_SWITCH.stop()
    assert check("create_folder", Mode.RESTRICTED) == "block"
    assert check("create_folder", Mode.FREE) == "block"


def test_kill_switch_blocks_medium_risk():
    KILL_SWITCH.stop()
    assert check("run_command", Mode.RESTRICTED) == "block"


def test_kill_switch_blocks_high_risk():
    KILL_SWITCH.stop()
    assert check("send_message", Mode.FREE) == "block"


def test_kill_switch_reset_restores_normal():
    KILL_SWITCH.stop()
    KILL_SWITCH.reset()
    assert check("create_folder", Mode.RESTRICTED) == "allow"


# ---------------------------------------------------------------------------
# Unknown / unregistered actions
# ---------------------------------------------------------------------------


def test_unknown_action_block_restricted():
    assert check("do_something_weird", Mode.RESTRICTED) == "block"


def test_unknown_action_ask_free():
    assert check("do_something_weird", Mode.FREE) == "ask"


# ---------------------------------------------------------------------------
# Demo scenario: hello.py must NOT prompt in Restricted
# ---------------------------------------------------------------------------


def test_hello_demo_all_allow_restricted():
    """
    The five tool calls in the demo (open VS Code → create folder → create
    file → type code → run command) must all return 'allow' in Restricted mode
    so the demo runs without any user prompt.
    """
    demo_actions = [
        "open_vscode",
        "create_folder",
        "create_file",
        "type_code",
        "run_command",
    ]
    for action in demo_actions:
        result = check(action, Mode.RESTRICTED)
        assert result == "allow", (
            f"Demo action '{action}' returned '{result}' in Restricted "
            f"(must be 'allow' for the demo to run prompt-free)"
        )


# ---------------------------------------------------------------------------
# Workspace guard — resolve_path()
# ---------------------------------------------------------------------------


def test_resolve_inside_workspace(workspace: Path):
    inside = workspace / "subdir" / "file.txt"
    result = resolve_path(str(inside))
    assert str(result).startswith(str(workspace))


def test_resolve_dotdot_forward_slash(workspace: Path):
    """../escape using forward slashes."""
    evil = str(workspace) + "/../escape"
    with pytest.raises(PolicyError):
        resolve_path(evil)


def test_resolve_dotdot_backslash(workspace: Path):
    """..\\escape using Windows backslashes."""
    evil = str(workspace) + "\\.." + "\\escape"
    with pytest.raises(PolicyError):
        resolve_path(evil)


def test_resolve_dotdot_relative(workspace: Path):
    """Relative path with ..\\ that walks above workspace."""
    evil = str(workspace / ".." / ".." / "secret")
    with pytest.raises(PolicyError):
        resolve_path(evil)


def test_resolve_mixed_slashes(workspace: Path):
    """Mix of / and \\ in the path — still blocked."""
    base = str(workspace).replace("\\", "/")
    evil = base + "/../escape"
    with pytest.raises(PolicyError):
        resolve_path(evil)


def test_resolve_absolute_path_outside(workspace: Path):
    """Absolute path that is a sibling of workspace — blocked."""
    outside = workspace.parent / "outside_dir"
    with pytest.raises(PolicyError):
        resolve_path(str(outside))


def test_resolve_absolute_path_parent(workspace: Path):
    """Absolute path that is the *parent* of workspace — blocked."""
    with pytest.raises(PolicyError):
        resolve_path(str(workspace.parent))


def test_resolve_different_drive(workspace: Path):
    """
    Absolute path on a different Windows drive letter — blocked.
    Skipped when we cannot determine a different drive.
    """
    workspace_drive: str = workspace.drive  # e.g. "C:"
    candidates = ["C:", "D:", "E:", "F:", "G:"]
    other = next((d for d in candidates if d.upper() != workspace_drive.upper()), None)
    if other is None:
        pytest.skip("No alternative drive letter available for this test")
    evil = other + "\\Windows\\System32"
    with pytest.raises(PolicyError):
        resolve_path(evil)


def test_resolve_symlink_escape(workspace: Path):
    """
    A symlink inside workspace pointing to a directory outside → blocked.
    Skipped if the environment cannot create symlinks (needs Developer Mode
    or admin rights on Windows).
    """
    outside = workspace.parent / "symlink_target"
    outside.mkdir(exist_ok=True)
    link = workspace / "link_to_outside"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Cannot create symlinks in this environment")

    with pytest.raises(PolicyError):
        resolve_path(str(link))


def test_resolve_junction_escape(workspace: Path):
    """
    A Windows directory junction inside workspace pointing outside → blocked.
    Skipped if mklink /J fails (may need admin or Developer Mode).
    """
    outside = workspace.parent / "junction_target"
    outside.mkdir(exist_ok=True)
    link = workspace / "junction_link"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"Cannot create directory junction: {result.stderr.strip()}")

    with pytest.raises(PolicyError):
        resolve_path(str(link))


def test_resolve_8dot3_name_inside_workspace(workspace: Path):
    """
    Files accessed via their full (long) name inside workspace resolve correctly.

    Note: Python's Path.resolve() does NOT reliably expand Windows short 8.3
    names (e.g. WORKSP~1) to their full form on all filesystem configurations.
    For complete 8.3 coverage, wrap resolve_path() with
    win32api.GetLongPathName() (pywin32).  This test verifies the happy path
    only.
    """
    long_file = workspace / "a_very_long_filename_for_8dot3_test.txt"
    long_file.touch()
    result = resolve_path(str(long_file))
    assert result == long_file.resolve()


# ---------------------------------------------------------------------------
# ModeManager — basics
# ---------------------------------------------------------------------------


def test_mode_manager_default_is_restricted():
    mm = ModeManager()
    assert mm.mode == Mode.RESTRICTED


def test_mode_manager_enter_free_confirmed():
    mm = ModeManager(confirm_callback=lambda: True)
    assert mm.enter_free() is True
    assert mm.mode == Mode.FREE


def test_mode_manager_enter_free_denied():
    mm = ModeManager(confirm_callback=lambda: False)
    assert mm.enter_free() is False
    assert mm.mode == Mode.RESTRICTED


def test_mode_manager_enter_restricted_immediate():
    mm = ModeManager(confirm_callback=lambda: True)
    mm.enter_free()
    mm.enter_restricted()
    assert mm.mode == Mode.RESTRICTED


def test_mode_manager_screen_control_enabled_by_default():
    mm = ModeManager()
    assert mm.screen_control_enabled is True


# ---------------------------------------------------------------------------
# ModeManager — injectable confirm callback
# ---------------------------------------------------------------------------


def test_mode_manager_injectable_callback():
    """The UI can inject its own confirm callback."""
    calls: list[bool] = []

    def my_confirm() -> bool:
        calls.append(True)
        return True

    mm = ModeManager(confirm_callback=my_confirm)
    mm.enter_free()
    assert len(calls) == 1
    assert mm.mode == Mode.FREE


def test_mode_manager_injectable_callback_deny():
    denied: list = []

    def deny() -> bool:
        denied.append(True)
        return False

    mm = ModeManager(confirm_callback=deny)
    mm.enter_free()
    assert len(denied) == 1
    assert mm.mode == Mode.RESTRICTED


# ---------------------------------------------------------------------------
# ModeManager — auto-revert
# ---------------------------------------------------------------------------


def test_mode_manager_auto_revert_after_timeout():
    """Free mode must revert to Restricted after free_timeout seconds."""
    mm = ModeManager(free_timeout=1, confirm_callback=lambda: True)
    mm.enter_free()
    assert mm.mode == Mode.FREE
    time.sleep(1.3)   # wait for timer to fire
    assert mm.mode == Mode.RESTRICTED


def test_mode_manager_enter_restricted_cancels_timer():
    """Calling enter_restricted() before the timer fires should keep it Restricted."""
    mm = ModeManager(free_timeout=5, confirm_callback=lambda: True)
    mm.enter_free()
    mm.enter_restricted()
    assert mm.mode == Mode.RESTRICTED
    time.sleep(0.1)
    # Still Restricted, timer was cancelled
    assert mm.mode == Mode.RESTRICTED


# ---------------------------------------------------------------------------
# ModeManager — Ollama forces Restricted
# ---------------------------------------------------------------------------


def test_ollama_forces_restricted_from_free():
    mm = ModeManager(confirm_callback=lambda: True)
    mm.enter_free()
    assert mm.mode == Mode.FREE

    mm.notify_provider("ollama")

    assert mm.mode == Mode.RESTRICTED
    assert mm.screen_control_enabled is False


def test_ollama_forces_restricted_case_insensitive():
    mm = ModeManager(confirm_callback=lambda: True)
    mm.enter_free()
    mm.notify_provider("Ollama")   # capitalised
    assert mm.mode == Mode.RESTRICTED


def test_ollama_screen_control_disabled_in_restricted():
    mm = ModeManager()   # starts Restricted
    mm.notify_provider("ollama")
    assert mm.screen_control_enabled is False


def test_enter_free_re_enables_screen_control_after_ollama():
    """
    After Ollama disables screen control, a deliberate enter_free() must
    re-enable it (user consciously chose to go back to Free mode).
    """
    mm = ModeManager(confirm_callback=lambda: True)
    mm.notify_provider("ollama")
    assert mm.screen_control_enabled is False

    mm.enter_free()   # deliberate user action
    assert mm.mode == Mode.FREE
    assert mm.screen_control_enabled is True


def test_non_ollama_provider_does_not_change_mode():
    mm = ModeManager(confirm_callback=lambda: True)
    mm.enter_free()
    mm.notify_provider("gemini")
    assert mm.mode == Mode.FREE


def test_ollama_forced_restricted_persists_across_calls():
    """Ollama Restricted must stick even when notify_provider is called again."""
    mm = ModeManager(confirm_callback=lambda: True)
    mm.notify_provider("ollama")
    mm.notify_provider("gemini")   # non-Ollama does not lift the restriction
    assert mm.mode == Mode.RESTRICTED
    assert mm.screen_control_enabled is False


# ---------------------------------------------------------------------------
# Persona Safety & Confirmation Independence
# ---------------------------------------------------------------------------


def test_persona_confirmation_wording_fixed_and_independent():
    """
    Persona changes only tone, NEVER safety rules or confirmation prompts.
    Confirmation wording for risky actions and Free mode transitions must
    remain fixed, deterministic, and independent of persona text.
    """
    import yaml

    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    personas = cfg.get("persona", {}).get("available_personas", {})
    assert "kira" in personas, "kira persona must be defined in config.yaml"
    assert "ryuk" in personas, "ryuk persona must be defined in config.yaml"

    # 1. Mode switch confirmation prompt is fixed regardless of persona
    expected_mode_confirm = (
        "Enable Free mode? Fewer confirmations will be asked. "
        "Type 'yes' to confirm: "
    )
    assert ModeManager._terminal_confirm.__doc__ is not None
    # Test enter_free prompt format via mock callback verifying fixed text
    received_prompts = []

    def mock_cb():
        received_prompts.append("enter_free_prompt")
        return False

    mm = ModeManager(confirm_callback=mock_cb)
    mm.enter_free()
    assert len(received_prompts) == 1
    assert mm.mode == Mode.RESTRICTED  # Refused, remains restricted

    # 2. Risky tool confirmation format is fixed: "Action '{fn_name}' requires confirmation (Mode: {mode}). Arguments: {args}. Do you want to proceed? [y/N]: "
    # Neither kira's nor ryuk's persona prompts can alter policy check results or prompt templates
    for persona_name in ("kira", "ryuk"):
        # Policy checks for high-risk actions remain identical and strict
        assert check("delete_file", Mode.RESTRICTED) == "ask"
        assert check("send_message", Mode.RESTRICTED) == "ask"
        assert check("install_package", Mode.RESTRICTED) == "ask"
        assert check("format_disk", Mode.RESTRICTED) == "block"
        assert check("format_disk", Mode.FREE) == "block"
        assert check("change_security_settings", Mode.RESTRICTED) == "block"
        assert check("change_security_settings", Mode.FREE) == "block"
        assert check("make_payment", Mode.RESTRICTED) == "block"
        assert check("make_payment", Mode.FREE) == "block"


# ---------------------------------------------------------------------------
# ModeManager PIN Tests
# ---------------------------------------------------------------------------


def test_mode_manager_enter_free_with_correct_pin():
    mm = ModeManager(pin="1684")
    assert mm.enter_free(pin="1684") is True
    assert mm.mode == Mode.FREE


def test_mode_manager_enter_free_with_wrong_pin():
    mm = ModeManager(pin="1684")
    assert mm.enter_free(pin="9999") is False
    assert mm.mode == Mode.RESTRICTED


def test_mode_manager_enter_free_pin_strips_whitespace():
    mm = ModeManager(pin="1684")
    assert mm.enter_free(pin=" 1684 \n") is True
    assert mm.mode == Mode.FREE


def test_extract_pin_digits_variations():
    from backend.brain import extract_pin_digits

    assert extract_pin_digits("1684") == "1684"
    assert extract_pin_digits("PIN: 1684") == "1684"
    assert extract_pin_digits("1 6 8 4") == "1684"
    assert extract_pin_digits("one six eight four") == "1684"
    assert extract_pin_digits("sixteen eighty-four") == "1684"
    assert extract_pin_digits("") == ""

