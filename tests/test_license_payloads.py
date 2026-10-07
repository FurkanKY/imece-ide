import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "packaging/license_payloads.py"
_spec = importlib.util.spec_from_file_location("imece_license_payloads", _SCRIPT)
_payloads = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_payloads)


def test_frontend_runtime_closure_stages_transitive_text_only(tmp_path):
    ui = tmp_path / "web/ui"
    root = ui / "node_modules/root"
    child = ui / "node_modules/child"
    dev = ui / "node_modules/dev-only"
    for package in (root, child, dev):
        package.mkdir(parents=True)
        (package / 'package.json').write_text(json.dumps({'name': package.name, 'version': '2' if package == child else '1'}))
    (root / "LICENSE").write_text("root license")
    (child / "NOTICE.txt").write_text("child notice")
    (dev / "LICENSE").write_text("dev license")
    lock = {"packages": {
        "": {"dependencies": {"root": "1"}, "devDependencies": {"dev-only": "1"}},
        "node_modules/root": {"name": "root", "version": "1", "dependencies": {"child": "1"}},
        "node_modules/child": {"name": "child", "version": "2"},
        "node_modules/dev-only": {"name": "dev-only", "version": "1"},
    }}
    (ui / "package-lock.json").write_text(json.dumps(lock))
    output = tmp_path / "out"
    report = _payloads._frontend_payload(ui, output)
    assert {package["name"] for package in report["packages"]} == {"root", "child"}
    assert (output / "node_modules/root/LICENSE").read_text() == "root license"
    assert (output / "node_modules/child/NOTICE.txt").read_text() == "child notice"
    assert not (output / "node_modules/dev-only/LICENSE").exists()
    assert report["reviewRequired"] == []


def test_frontend_declared_missing_package_fails_without_path_leak(tmp_path):
    ui = tmp_path / "web/ui"
    ui.mkdir(parents=True)
    (ui / "package-lock.json").write_text(json.dumps({"packages": {"": {"dependencies": {"missing": "1"}}}}))
    with pytest.raises(_payloads.PayloadError, match="absent from lockfile"):
        _payloads._frontend_payload(ui, tmp_path / "out")


def test_safe_relative_paths_reject_traversal_and_absolute_paths():
    assert _payloads._safe_rel("../secret") is None
    assert _payloads._safe_rel("/home/private/LICENSE") is None
    assert _payloads._safe_rel("pkg/LICENSE") is not None


def test_scoped_same_leaf_and_deduped_peer_have_distinct_payloads(tmp_path):
    ui = tmp_path / 'web/ui'
    entries = {'': {'dependencies': {'@one/item': '1', '@two/item': '1'}}}
    for name in ('@one/item', '@two/item', 'peer'):
        path = ui / 'node_modules' / name
        path.mkdir(parents=True)
        (path/'package.json').write_text(json.dumps({'name':name,'version':'1'}))
        (path/'LICENSE').write_text('License of '+name)
        entries['node_modules/'+name] = {'name':name,'version':'1'}
    entries['node_modules/@one/item']['peerDependencies'] = {'peer':'1'}
    (ui/'package-lock.json').write_text(json.dumps({'packages':entries}))
    output = tmp_path/'out'
    report = _payloads._frontend_payload(ui,output)
    assert {p['name'] for p in report['packages']} == {'@one/item','@two/item','peer'}
    assert (output/'node_modules/@one/item/LICENSE').read_text() == 'License of @one/item'
    assert (output/'node_modules/@two/item/LICENSE').read_text() == 'License of @two/item'


def test_installed_frontend_version_mismatch_is_not_silently_licensed(tmp_path):
    ui=tmp_path/'web/ui'
    package=ui/'node_modules/a'
    package.mkdir(parents=True)
    (package/'package.json').write_text('{"name":"a","version":"2"}')
    (ui/'package-lock.json').write_text(json.dumps({'packages':{'':{'dependencies':{'a':'1'}},'node_modules/a':{'version':'1'}}}))
    with pytest.raises(_payloads.PayloadError,match='differs from lockfile'):
        _payloads._frontend_payload(ui,tmp_path/'out')
