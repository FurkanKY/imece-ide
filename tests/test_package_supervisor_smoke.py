"""Execute the Node FD smoke harness against source helper dispatch.
This validates the harness, NOT an actual PyInstaller package.
"""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux package smoke helpers')
def test_linux_smoke_environment_selects_wayland_and_restricts_platforms(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Existing Node required; no dependencies installed')
    root = Path(__file__).parents[1]
    script = ('import { createServer } from "node:net";\n'
        'import { mkdtemp, mkdir, rm } from "node:fs/promises";\n'
        'import path from "node:path";\n'
        'import { linuxSmokeEnvironment, selectLinuxPlatform } from ' + json.dumps((root / 'packaging/smoke-runtime.mjs').as_uri()) + ';\n'
        'const base = await mkdtemp(path.join(' + json.dumps(str(tmp_path)) + ', "env-"));\n'
        'const runtime = path.join(base, "host-runtime"); await mkdir(runtime);\n'
        'const socketPath = path.join(runtime, "wayland-0"); const server = createServer();\n'
        'await new Promise((resolve, reject) => server.once("error", reject).listen(socketPath, resolve));\n'
        'const selected = selectLinuxPlatform({ XDG_RUNTIME_DIR: runtime, WAYLAND_DISPLAY: "wayland-0", DISPLAY: ":9" });\n'
        'const env = linuxSmokeEnvironment(base, { XDG_RUNTIME_DIR: runtime, WAYLAND_DISPLAY: "wayland-0" });\n'
        'if (selected.platform !== "wayland" || selected.waylandDisplay !== socketPath || env.XDG_RUNTIME_DIR === runtime || env.WAYLAND_DISPLAY !== socketPath) throw Error("Wayland isolation failed");\n'
        'if (selectLinuxPlatform({ QT_QPA_PLATFORM: "offscreen" }).platform !== "offscreen") throw Error("offscreen override failed");\n'
        'let rejected = false; try { selectLinuxPlatform({ QT_QPA_PLATFORM: "minimal" }); } catch { rejected = true; }\n'
        'if (!rejected) throw Error("unsupported platform accepted");\n'
        'await new Promise(resolve => server.close(resolve)); await rm(base, { recursive: true });');
    result = subprocess.run([node, '--input-type=module', '-e', script], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux inherited-FD smoke harness')
def test_linux_node_harness_authenticates_real_source_dispatch(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Existing Node required; no dependencies installed')
    root = Path(__file__).parents[1]
    wrapper = tmp_path / 'source-helper'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(root / 'shell.py')) + ' "$@"\n')
    wrapper.chmod(0o700)
    script = ('import { checkFrozenSupervisor } from ' + json.dumps((root / 'packaging/supervisor-smoke.mjs').as_uri()) + ';\n' +
        'const result = await checkFrozenSupervisor(' + json.dumps(str(wrapper)) + ', ' +
        json.dumps({'HOME': str(tmp_path), 'PATH': '', 'PYTHONDONTWRITEBYTECODE': '1'}) + ', ' + json.dumps(str(tmp_path)) + ', "linux");\n' +
        'console.log(JSON.stringify(result));')
    result = subprocess.run([node, '--input-type=module', '-e', script], cwd=tmp_path,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'commandExitCode': 7, 'producerQuiescent': True, 'invalidDispatchExitCode': 125}
    assert not (tmp_path / '.multi_agent_ide').exists()
