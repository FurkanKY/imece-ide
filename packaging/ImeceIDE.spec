# -*- mode: python ; coding: utf-8 -*-
"""Windows/Linux onedir package; acceptance is tracked separately."""

from pathlib import Path
import sys
import runpy
import tempfile
from importlib.metadata import version

import nodejs_wheel
from PyInstaller.utils.hooks import collect_all, copy_metadata


if sys.platform not in ("win32", "linux"):
    raise SystemExit("Packaging supports Windows and Linux only")
ROOT = Path(SPECPATH).parent
UI_DIST = ROOT / "web" / "ui" / "dist"
if not (UI_DIST / "index.html").is_file():
    raise SystemExit(f"Built UI is missing: {UI_DIST / 'index.html'}")
node_exe = Path(nodejs_wheel.__file__).parent / ("node.exe" if sys.platform == "win32" else "bin/node")
if not node_exe.is_file():
    raise SystemExit(f"basedpyright node runtime is missing: {node_exe}")

node_version = version("nodejs-wheel-binaries")
node_license = ROOT / "packaging" / "licenses" / f"node-v{node_version}-LICENSE.txt"
if not node_license.is_file():
    raise SystemExit(f"Matching Node.js {node_version} license is missing: {node_license}; vendor the official version-specific license before packaging")

bp_datas, bp_binaries, bp_hidden = collect_all("basedpyright")
dp_datas, dp_binaries, dp_hidden = collect_all("debugpy")
# Platform-specific PTY runtime.
if sys.platform == "win32":
    wp_datas, wp_binaries, wp_hidden = collect_all("winpty")
    pty_hidden = ["winpty", "winpty.ptyprocess"]
else:
    wp_datas, wp_binaries, wp_hidden = collect_all("ptyprocess")
    pty_hidden = ["ptyprocess"]

# Jev System One karar katmanı (decision_runtime, Spike S1b): İSTEĞE BAĞLI.
# typesafe-sdk yalnızca derleme ortamında kuruluysa toplanır; kurulu değilse
# karar katmanı "rules/off" ile çalışmaya devam eder (design rule 1 —
# decision_runtime.typesafe-sdk'ı yalnızca decide() içinde lazy import eder).
try:
    ts_datas, ts_binaries, ts_hidden = collect_all("typesafe_sdk")
except Exception:
    ts_datas, ts_binaries, ts_hidden = [], [], []

inventory = runpy.run_path(str(ROOT / "packaging" / "dependencies.py"))["build_inventory"](ROOT / "requirements.txt")
license_metadata = []
for package in inventory["packages"]:
    license_metadata.extend(copy_metadata(package["name"]))
license_temp = tempfile.TemporaryDirectory(prefix="imece-license-payloads-")
license_stage = Path(license_temp.name)
runpy.run_path(str(ROOT / "packaging" / "license_payloads.py"))["collect"](ROOT, license_stage)
payload_datas = [(str(path), (Path("licenses") / path.parent.relative_to(license_stage)).as_posix())
                 for path in license_stage.rglob("*") if path.is_file()]

datas = [
    *license_metadata,
    *payload_datas,
    (str(node_license), "licenses"),
    (str(UI_DIST), "web/ui/dist"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD-PARTY-NOTICES.md"), "."),
    *bp_datas,
    *dp_datas,
    *wp_datas,
    *ts_datas,
]
binaries = [(str(node_exe), "nodejs_wheel" if sys.platform == "win32" else "nodejs_wheel/bin"), *bp_binaries, *dp_binaries, *wp_binaries, *ts_binaries]
hiddenimports = [
    *bp_hidden,
    *dp_hidden,
    *wp_hidden,
    *ts_hidden,
    "code",
    "http.server",
    "xmlrpc.client",
    "xmlrpc.server",
    *pty_hidden,
    "process_runtime.supervisor_launch",
    "process_runtime.supervisor",
    "process_runtime.windows_job",
]

a = Analysis(
    [str(ROOT / "shell.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT / 'packaging/hooks')],
    hooksconfig={},
    runtime_hooks=[],
    # GNU readline is unnecessary for the GUI debugger console; exclude its
    # GPL-only extension/runtime rather than redistributing it accidentally.
    excludes=["flask", "readline"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ImeceIDE",
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
    name="ImeceIDE",
)
license_temp.cleanup()
