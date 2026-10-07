"""Evaluate the spec using build-tool stubs, without claiming a frozen build."""
import runpy
import sys
import importlib.metadata
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.mark.parametrize('missing_license', [False, True])
@pytest.mark.parametrize('platform, node_path, destination, pty', [
    ('win32', 'node.exe', 'nodejs_wheel', 'winpty'),
    ('linux', 'bin/node', 'nodejs_wheel/bin', 'ptyprocess'),
])
def test_spec_keeps_node_wheel_layout_and_platform_pty(tmp_path, monkeypatch, platform, node_path, destination, pty, missing_license):
    root = tmp_path / 'source'
    (root / 'packaging/licenses').mkdir(parents=True)
    (root / 'packaging/licenses/node-v24.19.0-LICENSE.txt').write_text('fixture license')
    for name in ('LGPL-3.0.txt', 'GPL-3.0.txt'):
        (root / 'packaging/licenses' / name).write_text('fixture legal text')
    monkeypatch.setattr(importlib.metadata, 'version', lambda name: '24.19.0')
    (root / 'packaging/licenses/COPYING.txt').write_text('fixture bootloader license')
    monkeypatch.setattr(importlib.metadata, 'distribution', lambda name: SimpleNamespace(
        files=[Path('COPYING.txt')], locate_file=lambda record: root / 'packaging/licenses/COPYING.txt'))
    (root / 'packaging/dependencies.py').write_text("def build_inventory(path):\n    return {'packages': [{'name': 'runtime-fixture'}]}\n")
    source_payloads = Path(__file__).parents[1] / 'packaging/license_payloads.py'
    (root / 'packaging/license_payloads.py').write_text(source_payloads.read_text())
    (root / 'requirements.txt').write_text('runtime-fixture\n')
    ui = root / 'web/ui'
    ui.mkdir(parents=True, exist_ok=True)
    (ui / 'package-lock.json').write_text('{\"packages\": {\"\": {\"dependencies\": {}}}}')
    (root / 'web/ui/dist').mkdir(parents=True)
    (root / 'web/ui/dist/index.html').write_text('<html/>')
    wheel = tmp_path / 'node-wheel'
    node = wheel / node_path
    node.parent.mkdir(parents=True)
    node.write_bytes(b'fixture')
    fake_node = ModuleType('nodejs_wheel')
    fake_node.__file__ = str(wheel / '__init__.py')
    monkeypatch.setitem(sys.modules, 'nodejs_wheel', fake_node)
    collected, analyses = [], []
    hooks = ModuleType('PyInstaller.utils.hooks')
    def collect(name):
        collected.append(name)
        return [], [], []
    hooks.collect_all = collect
    hooks.copy_metadata = lambda name: [(name + '.dist-info', name + '.dist-info')]
    monkeypatch.setitem(sys.modules, 'PyInstaller.utils.hooks', hooks)
    monkeypatch.setattr(sys, 'platform', platform)
    def analysis(scripts, **kwargs):
        analyses.append(kwargs)
        return SimpleNamespace(pure=[], scripts=scripts, binaries=[], datas=[])
    globals_ = {
        'SPECPATH': str(root / 'packaging'), 'Analysis': analysis,
        'PYZ': lambda *args: None, 'EXE': lambda *args, **kwargs: None,
        'COLLECT': lambda *args, **kwargs: None,
    }
    spec = str(Path(__file__).parents[1] / 'packaging/ImeceIDE.spec')
    if missing_license:
        (root / 'packaging/licenses/node-v24.19.0-LICENSE.txt').unlink()
        with pytest.raises(SystemExit, match='Matching Node.js .* license is missing'):
            runpy.run_path(spec, init_globals=globals_)
        assert not analyses
        return
    runpy.run_path(spec, init_globals=globals_)
    assert analyses[0]['binaries'][0] == (str(node), destination)
    assert ('runtime-fixture.dist-info', 'runtime-fixture.dist-info') in analyses[0]['datas']
    assert 'readline' in analyses[0]['excludes']
    assert analyses[0]['hookspath'] == [str(root / 'packaging/hooks')]
    assert all(not destination.endswith('.txt') for _, destination in analyses[0]['datas'])
    assert pty in collected
    assert ('winpty' if platform == 'linux' else 'ptyprocess') not in collected
    assert 'process_runtime.supervisor' in analyses[0]['hiddenimports']
    assert 'process_runtime.windows_job' in analyses[0]['hiddenimports']
