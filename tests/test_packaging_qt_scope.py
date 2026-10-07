"""Keep Qt binding/native ABI resolution but omit unused QML application assets."""
import importlib.util
from pathlib import Path
import runpy
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('imece_scope_check', ROOT/'packaging/check.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_qml_hook_retains_native_dependencies_not_all_installed_qml(tmp_path, monkeypatch):
    module = ModuleType('PyInstaller.utils.hooks.qt')
    calls = []
    def native(path):
        calls.append(path)
        return ['PySide6.QtQuick'], [('Qt6Qml.so','lib')], [('qtdeclarative.qm','translations')]
    module.add_qt6_dependencies = native
    monkeypatch.setitem(sys.modules,'PyInstaller.utils.hooks.qt',module)
    result = runpy.run_path(str(ROOT/'packaging/hooks/hook-PySide6.QtQml.py'))
    assert calls and result['hiddenimports'] == ['PySide6.QtQuick']
    assert result['binaries'] == [('Qt6Qml.so','lib')]
    assert result['datas'] == [('qtdeclarative.qm','translations')]


def test_gui_hook_filters_only_pdf_and_virtual_keyboard_plugins(monkeypatch):
    module = ModuleType('PyInstaller.utils.hooks.qt')
    plugins = ['libqpdf.so', 'qtvirtualkeyboardplugin.dll', 'libqxcb.so', 'qwindows.dll', 'libqsvg.so', 'libibusplatforminputcontextplugin.so']
    module.add_qt6_dependencies = lambda path: (['PySide6.QtCore'], [(name, 'plugins') for name in plugins], [('translation.qm','translations')])
    monkeypatch.setitem(sys.modules,'PyInstaller.utils.hooks.qt',module)
    result = runpy.run_path(str(ROOT/'packaging/hooks/hook-PySide6.QtGui.py'))
    assert [name for name, target in result['binaries']] == plugins[2:]
    assert result['hiddenimports'] == ['PySide6.QtCore'] and result['datas']


@pytest.mark.parametrize('payload',['_internal/PySide6/Qt/lib/libQt6Charts.so.6','_internal/PySide6/Qt6Pdf.dll','_internal/PySide6/Qt/qml/QtQuick/qmldir'])
def test_bundle_refuses_unreviewed_qml_addons(tmp_path, monkeypatch, payload):
    bundle=tmp_path/'bundle'; bundle.mkdir()
    target=bundle/payload
    target.parent.mkdir(parents=True)
    target.write_bytes(b'unneeded')
    monkeypatch.setattr(check,'check_sources',lambda root:'fixture')
    with pytest.raises(check.PackagingError,match='Qt QML/add-on'):
        check.check_bundle(tmp_path,bundle,platform='linux')
