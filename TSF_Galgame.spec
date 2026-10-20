# -*- mode: python ; coding: utf-8 -*-
# 阶段 4（M-13a/M-13b）打包配置：onefile+console → **onedir + 窗口化**。
#   · 入口改为 window_host.py（原生窗口宿主；pywebview 占主线程、server 跑后台线程）
#   · server.py 同时作为**被分析的脚本**与**随包数据文件**：
#     分析它 → app.* / rembg / onnxruntime 等依赖才会被自动收集；
#     随包发出 → 冻结形态下 runpy 可按路径兜底执行。
#   · console=False：windowed 形态（黑框消失）。⚠️ 代价见 t36 契约 §3.2：
#     uvicorn 的 default/access handler 指向 sys.stderr 且 propagate=False，
#     windowed 下**必须显式改道文件**，否则访问日志静默丢失。

# ⚠️ CoC 内容层：`data/` 被 .gitignore 忽略，但 `app/coc/paths.py` 是从 `__file__` 推
# `ROOT/data/coc` 的 ⇒ **必须显式打进包**，落到 `_internal/data/coc/**`
# （onedir 下 `app.coc.paths.__file__` 解析到 `_internal/app/coc/paths.py`，其 ROOT 即 `_internal`）。
# 不打包的后果是启动/进入 CoC 时抛 `TuningMissingError`（有意为之的明确失败，不是静默兜底）。
# 1.8.0 硬依赖：本机 window_host 的 QtWebEngine 窗口后端 = PySide6。
# 缺它时 PyInstaller 仍能构建，但产物只剩 Edge 回退链（实测无地址栏校验失败 →
# 用户双击无窗口）。宁可在构建期明确失败，也不要产出"能打包但打不开"的 exe。
# 本机用法：创建 .build-venv 装 PySide6 后用
#   $env:PYTHONPATH='D:\TSF_Galgame\.build-venv\Lib\site-packages'
#   python -m PyInstaller --noconfirm TSF_Galgame.spec
import importlib.util as _ilu
if _ilu.find_spec('PySide6') is None:
    raise SystemExit(
        '[spec] 未找到 PySide6：窗口版必须带 QtWebEngine 后端。'
        '请先 pyenv_install PySide6 并设置 PYTHONPATH（见 交接上下文.md 1.8.0 构建说明）。')

import pathlib as _pl
_coc_root = _pl.Path('data/coc')
_coc_datas = []
if _coc_root.is_dir():
    for _p in sorted(_coc_root.rglob('*')):
        if not _p.is_file() or '__pycache__' in _p.parts or _p.suffix == '.pyc':
            continue
        _rel = _p.relative_to(_coc_root)
        if 'saves' in _rel.parts:        # 玩家存档不随包（运行时在 DATA_DIR 生成）
            continue
        _coc_datas.append((str(_p), str(_pl.PurePosixPath('data/coc') / _rel.parent)))

a = Analysis(
    ['window_host.py', 'server.py'],
    pathex=['.'],
    binaries=[],
    datas=[('static', 'static'), ('config.example.json', '.'),
           # 冻结后 runpy 兜底要能按路径找到它（source 形态同文件已存在，这里是随包副本）
           ('server.py', '.')] + _coc_datas,
    hiddenimports=['rembg', 'rembg.sessions', 'onnxruntime', 'pymatting',
                   'rembg.bg', 'rembg.session_factory',
                   # 阶段 4 P2：windowed + 原生窗口的保底收集（防 PyInstaller 升级误裁）
                   'server', 'webview', 'webview.platforms.winforms',
                   'clr', 'clr_loader',
                   # 1.8.0：本机实际窗口后端 = PySide6/QtWebEngine（window_host 第二级首选）。
                   # 构建机需可 import PySide6（用 .build-venv 的 site-packages 经 PYTHONPATH 注入）；
                   # 其余 webview/cr 缺失只产生 warning，不影响 Qt 路径。
                   'PySide6', 'PySide6.QtCore', 'PySide6.QtGui',
                   'PySide6.QtWidgets', 'PySide6.QtNetwork',
                   'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets'],
    hookspath=[],
    hooksconfig={'collect_all': ['rembg', 'onnxruntime', 'webview', 'pythonnet']},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# onedir：可执行文件不内嵌负载，产物是「目录 + exe」（无首启解压、单进程）
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TSF_Galgame',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                 # ★ 阶段 4 P1：唯一必须改的一行
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TSF_Galgame',
)
