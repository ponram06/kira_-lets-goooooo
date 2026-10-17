# Kira System Limitations & Known Constraints

This document details architectural, security, and hardware limitations present in the Kira assistant prototype.

---

## 1. Script Runner Sandbox Boundary

- **Static AST Analysis**: The `run_script` tool applies Python `ast.parse` static checks to disallow dangerous standard library imports (`os`, `sys`, `subprocess`, `ctypes`, `socket`, `shutil`), dunder attribute access, and `eval`/`exec`.
- **Sandbox Limitation**: Python is an expressive, dynamic language. Static checks alone do **not** constitute a full OS-level sandbox (e.g. advanced format-string reflection or complex bytecode manipulation can bypass naive AST checks).
- **Hardened Sandboxing**: For untrusted script execution, users should enable `scripts.sandbox: docker` or `scripts.sandbox: windows_sandbox` in `config.yaml`. If configured, Kira strictly refuses to run scripts if the chosen container engine is unavailable.

---

## 2. WhatsApp Messaging Delivery Verification

- **Deep Link & Keystroke Protocol**: Kira interacts with WhatsApp via the standard `whatsapp://send?phone=...&text=...` URI protocol and sends `VK_RETURN` to the verified foreground process.
- **Delivery Verification**: Win32 API does not expose WhatsApp internal network delivery status or message delivery checkmarks to third-party applications.
- **Honest Status**: Kira explicitly reports:
  *"I pressed Enter. I can't verify delivery, check WhatsApp."*
  It never claims confirmed delivery when it cannot verify it.

---

## 3. Graphics & 3D Avatar Rendering

- **Target Hardware**: Intel Core 5 210H, 16 GB RAM, Intel Iris / integrated graphics.
- **Resource Constraints**: High-polygon 3D VRM models with complex physics or textures can stress integrated GPUs.
- **Fallback Guard**: If rendering drops below 20 FPS for 10 seconds or WebGL loses context, Kira automatically falls back to the lightweight 2D canvas sprite renderer with an audible notice.
- **Low-Power Mode**: The avatar menu provides an explicit toggle to lock rendering to the 2D sprite to minimize laptop battery consumption.

---

## 4. Code Signing & Packaging Warnings

- **Unsigned Binaries**: The bundled PyInstaller backend (`dist/backend/kira-backend.exe`) and Electron portable builds are not signed with a paid EV code signing certificate.
- **SmartScreen & Antivirus**: Windows SmartScreen or third-party antivirus software may flag unsigned `.exe` binaries on first execution. Users should click "More info -> Run anyway" on initial startup.

---

## 5. Screen Control Context Sensitivity

- **Process-Based Detection**: Screen control detects sensitive windows (password managers, banking sites, Windows Security) based on foreground window process names and window titles.
- **Limitation**: If a sensitive web application hides its purpose under a generic title (e.g. "Home - My Account"), title-based heuristics cannot reliably identify it without vision inspection.
