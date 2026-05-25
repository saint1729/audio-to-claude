# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for AudioToClaude.app (macOS)

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'pyte',
        'sounddevice',
        'soundfile',
        'numpy',
        'openai',
        'dotenv',
        'cffi',
        '_cffi_backend',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AudioToClaude',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,        # GUI app — no terminal window
    disable_windowed_traceback=False,
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
    name='AudioToClaude',
)

app = BUNDLE(
    coll,
    name='AudioToClaude.app',
    icon='assets/icon.icns',
    bundle_identifier='com.user.audio-to-claude',
    info_plist={
        # Required for microphone access on macOS
        'NSMicrophoneUsageDescription': (
            'AudioToClaude captures audio for real-time transcription.'
        ),
        'NSHighResolutionCapable': True,
        # Allow the process to appear as a regular app (shows in Dock)
        'LSUIElement': False,
        'CFBundleDisplayName': 'AudioToClaude',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0',
        # macOS 10.15+ requires this for subprocess / PTY
        'NSAppleScriptEnabled': False,
    },
)
