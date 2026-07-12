# -*- mode: python ; coding: utf-8 -*-
"""Beta-3 Windows onedir paketi."""

from pathlib import Path

import nodejs_wheel
from PyInstaller.utils.hooks import collect_all


ROOT = Path(SPECPATH).parent
UI_DIST = ROOT / "web" / "ui" / "dist"

bp_datas, bp_binaries, bp_hidden = collect_all("basedpyright")
dp_datas, dp_binaries, dp_hidden = collect_all("debugpy")
# winpty: conpty.dll/winpty.dll YANINDA OpenConsole.exe + winpty-agent.exe ŞART.
# Bu .exe'ler ConPTY tarafından runtime'da spawn edilir (linklenmiş bağımlılık DEĞİL),
# PyInstaller otomatik analizi göremez → collect_all ile paketle (Beta-3 PTY blokeri).
wp_datas, wp_binaries, wp_hidden = collect_all("winpty")

node_exe = Path(nodejs_wheel.__file__).parent / "node.exe"

datas = [
    (str(UI_DIST), "web/ui/dist"),
    (str(node_exe), "nodejs_wheel"),
    *bp_datas,
    *dp_datas,
    *wp_datas,
]
binaries = [*bp_binaries, *dp_binaries, *wp_binaries]
hiddenimports = [
    *bp_hidden,
    *dp_hidden,
    *wp_hidden,
    "code",
    "http.server",
    "xmlrpc.client",
    "xmlrpc.server",
    "winpty",
    "winpty.ptyprocess",
]

a = Analysis(
    [str(ROOT / "shell.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["flask"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MultiAgentIDE",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    hide_console="hide-early",
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MultiAgentIDE",
)
