# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['server.py'],
    pathex=['.'],
    binaries=[],
    datas=[('static', 'static'), ('config.example.json', '.')],
    hiddenimports=['rembg', 'rembg.sessions', 'onnxruntime', 'pymatting',
                   'rembg.bg', 'rembg.session_factory'],
    hookspath=[],
    hooksconfig={'collect_all': ['rembg', 'onnxruntime']},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TSF_Galgame',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
