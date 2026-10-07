"""Engineering archive guards/provenance; fixtures are not native acceptance."""
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

HERE=Path(__file__).parents[1]/'packaging'


def test_native_copyright_staging_uses_safe_relative_report(tmp_path, monkeypatch):
    namespace=runpy.run_path(str(HERE/'native_licenses.py'))
    collect=namespace['collect_native']
    monkeypatch.setitem(collect.__globals__, '_query_owner', lambda source: ('dummy','1.0'))
    root=tmp_path/'root'
    (root/'build/ImeceIDE').mkdir(parents=True)
    (root/'build/ImeceIDE/COLLECT-00.toc').write_text(repr(([('libdummy.so','/lib/libdummy.so','BINARY')],)))
    bundle=tmp_path/'bundle'
    (bundle/'_internal').mkdir(parents=True)
    (bundle/'_internal/libdummy.so').write_bytes(b'fixture ELF')
    docs=tmp_path/'docs'
    (docs/'dummy').mkdir(parents=True)
    (docs/'dummy/copyright').write_text('Copyright fixture. See /usr/share/common-licenses/MIT')
    common=tmp_path/'common'
    common.mkdir()
    (common/'MIT').write_text('MIT fixture full text')
    report=collect(root,bundle,'linux',docs,common)
    assert report['reviewRequired']==[]
    assert report['libraries'][0]['sourcePackage']=='dummy'
    assert (bundle/'_internal/licenses/native/common/MIT').read_text()=='MIT fixture full text'
    assert str(tmp_path) not in json.dumps(report)
    assert report['libraries'][0]['sha256']==hashlib.sha256(b'fixture ELF').hexdigest()


def test_windows_native_report_is_explicitly_unaccepted(tmp_path):
    report=runpy.run_path(str(HERE/'native_licenses.py'))['collect_native'](tmp_path,tmp_path/'bundle','win32')
    assert report['reviewRequired'] and not report['libraries']
    assert (tmp_path/'bundle/_internal/licenses/native/inventory.json').is_file()


def test_archive_name_validation_is_locale_independent():
    import os
    import subprocess
    script = (HERE/'verify.sh').read_text()
    validation = next(line for line in script.splitlines() if line.startswith('( export LC_ALL=C;'))
    for filename, expected in [('ImeceIDE-linux-manual.tar.gz', 0), ('ImeceIDE-../../bad.tar.gz', 2)]:
        result=subprocess.run(['bash','-c',validation],env={**os.environ,'LC_ALL':'tr_TR.UTF-8','ARCHIVE':filename},capture_output=True)
        assert result.returncode==expected


def test_archive_requires_positive_normal_close_and_supervisor(tmp_path):
    archive=runpy.run_path(str(HERE/'deliver.py'))['archive']
    report=tmp_path/'smoke.json'
    report.write_text(json.dumps({'ok':True,'normalCloseOk':False}))
    with pytest.raises(ValueError, match='smoke report required'):
        archive(tmp_path,tmp_path/'bundle','linux',report,tmp_path/'helper.json',tmp_path/'out.tar.gz')
    assert not (tmp_path/'out.tar.gz').exists()


def _receipts(bundle, tmp_path):
    (bundle/'package-manifest.json').write_text('{}')
    (bundle/'ImeceIDE').write_bytes(b'executable')
    manifest=hashlib.sha256((bundle/'package-manifest.json').read_bytes()).hexdigest()
    executable=hashlib.sha256((bundle/'ImeceIDE').read_bytes()).hexdigest()
    common={'manifestSha256':manifest,'executableSha256':executable}
    gui=tmp_path/'smoke.json'
    gui.write_text(json.dumps({'ok':True,'normalCloseOk':True,'supervisor':{'producerQuiescent':True,'commandExitCode':7,'invalidDispatchExitCode':125},**common}))
    helper=tmp_path/'helper.json'
    helper.write_text(json.dumps({'ok':True,'checks':{'nodeVersionOk':True,'debugpyVersionOk':True,'lspInitializeShutdownOk':True,'producerQuiescent':True},**common}))
    return gui,helper


def test_archive_rejects_recursive_destination_even_with_positive_smoke(tmp_path):
    archive=runpy.run_path(str(HERE/'deliver.py'))['archive']
    bundle=tmp_path/'bundle'
    bundle.mkdir()
    gui,helper=_receipts(bundle,tmp_path)
    with pytest.raises(ValueError, match='within the bundle'):
        archive(tmp_path,bundle,'linux',gui,helper,bundle/'recursive.tar.gz')


def test_archive_requires_current_gui_and_helper_receipts(tmp_path):
    archive=runpy.run_path(str(HERE/'deliver.py'))['archive']
    bundle=tmp_path/'bundle'; bundle.mkdir()
    gui,helper=_receipts(bundle,tmp_path)
    with pytest.raises(ValueError, match='helper smoke receipt required'):
        archive(tmp_path,bundle,'linux',gui,tmp_path/'missing.json',tmp_path/'out.tar.gz')
    helper.write_text(json.dumps({'ok':True,'checks':{'nodeVersionOk':True,'debugpyVersionOk':True,'lspInitializeShutdownOk':True,'producerQuiescent':True},'manifestSha256':'stale','executableSha256':'stale'}))
    with pytest.raises(ValueError, match='stale'):
        archive(tmp_path,bundle,'linux',gui,helper,tmp_path/'out.tar.gz')
    gui.write_text(json.dumps({'ok':True,'normalCloseOk':True,'supervisor':{'producerQuiescent':True,'commandExitCode':7,'invalidDispatchExitCode':125},'manifestSha256':'stale','executableSha256':'stale'}))
    with pytest.raises(ValueError, match='GUI smoke receipt'):
        archive(tmp_path,bundle,'linux',gui,helper,tmp_path/'out.tar.gz')


def test_helper_smoke_lsp_frame_parser_handles_partial_and_multiple_frames():
    module=runpy.run_path(str(HERE/'helper-smoke.py'))
    one=module['frame']({'jsonrpc':'2.0','id':1})
    two=module['frame']({'jsonrpc':'2.0','id':2})
    messages,rest=module['extract_frames'](one[:8])
    assert messages==[] and rest==one[:8]
    messages,rest=module['extract_frames'](rest+one[8:]+two)
    assert messages==[{'jsonrpc':'2.0','id':1},{'jsonrpc':'2.0','id':2}] and rest==b''


def test_helper_smoke_cleanup_terminates_unresponsive_child():
    module=runpy.run_path(str(HERE/'helper-smoke.py'))
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
    module['terminate'](child)
    assert child.poll() is not None
