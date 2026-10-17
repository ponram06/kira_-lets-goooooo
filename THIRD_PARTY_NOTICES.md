# Third-Party Notices & Licenses

This document acknowledges third-party software, open-source libraries, and character assets used in the Kira Desktop Assistant prototype.

---

## 1. Character & Intellectual Property Notices

- **Ryuk & Death Note Motifs**:
  The "Ryuk" persona, apple references, and gothic notebook aesthetic are based on the character from *Death Note*, created by Tsugumi Ohba and Takeshi Obata, published by Shueisha. This project is a non-commercial educational prototype. No official character assets are redistributed in this repository.
- **3D VRM Avatars**:
  Kira does not bundle proprietary third-party 3D models. The repository includes an automated generator for testing purposes only (`scripts/vrm-smoke.js`). Users supplying custom `.vrm` files (e.g. exported from VRoid Studio) must ensure compliance with their model author's redistribution and commercial use permissions.

---

## 2. Python Backend Dependencies

| Package | License | Copyright / Publisher |
|---|---|---|
| `openai` | Apache-2.0 | OpenAI |
| `playwright` | Apache-2.0 | Microsoft Corporation |
| `edge-tts` | GPL-3.0-or-later | rany2 |
| `faster-whisper` | MIT | Guillaume Klein |
| `sounddevice` | MIT | Matthias Geier |
| `miniaudio` | MIT | David Reid |
| `pyttsx3` | MPL-2.0 | Natesh M Bhat |
| `pyautogui` | BSD-3-Clause | Al Sweigart |
| `pycaw` | MIT | Andre Miras |
| `psutil` | BSD-3-Clause | Jay Loden, Dave Daeschler, Giampaolo Rodola |
| `screen-brightness-control` | MIT | Crozzers |
| `websockets` | BSD-3-Clause | Aymeric Augustin |
| `pyyaml` | MIT | Kirill Simonov |
| `python-dotenv` | BSD-3-Clause | Saurabh Kumar |
| `pyinstaller` | GPL-2.0-or-later with Bootloader Exception | Hartmut Goebel, PyInstaller Team |

---

## 3. Node.js & Electron Dependencies

| Package | License | Copyright / Publisher |
|---|---|---|
| `electron` | MIT | OpenJS Foundation / GitHub |
| `three` | MIT | Ricardo Cabello (mrdoob) |
| `@pixiv/three-vrm` | MIT | pixiv Inc. |
| `ws` | MIT | Einar Otto Stangvik |
| `electron-builder` | MIT | Vladimir Krivosheev |
