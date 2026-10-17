# -*- mode: python ; coding: utf-8 -*-
"""
kira_backend.spec
=================
PyInstaller spec file to bundle the Kira backend into a one-folder distribution.
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Collect all backend submodules
backend_modules = collect_submodules('backend')

# Static data files to include next to the binary
datas = [
    ('config.yaml', '.'),
    ('.env.example', '.'),
]

# Hidden imports for third-party libraries dynamically loaded
hiddenimports = backend_modules + [
    'websockets',
    'websockets.legacy',
    'websockets.legacy.server',
    'pycaw',
    'comtypes',
    'comtypes.client',
    'screen_brightness_control',
    'psutil',
    'pyautogui',
    'pygetwindow',
    'pyperclip',
    'sounddevice',
    'edge_tts',
    'pyttsx3',
    'pynput',
    'pynput.keyboard',
    'pynput.mouse',
    'pynput.keyboard._win32',
    'pynput.mouse._win32',
    'numpy',
    'playwright',
    'openai',
    'dotenv',
    'yaml',
]

a = Analysis(
    ['backend/main.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'notebook'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='kira-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='backend',
)
