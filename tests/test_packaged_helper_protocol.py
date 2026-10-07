"""Bounded helper smoke protocols; mocked IO is not frozen/platform acceptance."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

HELPER = Path(__file__).parents[1]/'packaging/helper-smoke.py'


@pytest.mark.parametrize('raw', [b'Content-Length: -1\r\n\r\nx', b'Content-Length: 9999999\r\n\r\n',
    b'Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}', b'Content-Length: 2\r\n\r\n[]'])
def test_malformed_frames_fail_closed(raw):
    module=runpy.run_path(str(HELPER))
    with pytest.raises(ValueError): module['extract_frames'](raw)


@pytest.mark.parametrize('change', [{'nonce':'wrong'}, {'quiescent':False}, {'exit_code':1}, {'exit_code':True}, {'cancelled':True}])
def test_helper_receipt_requires_nonce_drain_and_real_zero_exit(tmp_path, change):
    module=runpy.run_path(str(HELPER))
    receipt=tmp_path/'receipt.json'
    receipt.write_text(json.dumps({'nonce':'expected','quiescent':True,'exit_code':0,**change}))
    class Proc:
        _nonce='expected'
        _receipt_path=receipt
        def wait(self, timeout): return 0
    with pytest.raises(ValueError): module['wait_target'](Proc(),1)


def test_interactive_lsp_small_frames_do_not_wait_for_4096_bytes(tmp_path, monkeypatch):
    script=tmp_path/'fake-lsp.py'
    script.write_text('''import json,sys
while True:
    header=sys.stdin.buffer.readline()
    if not header: break
    length=int(header.split(b":",1)[1])
    assert sys.stdin.buffer.readline()==b"\\r\\n"
    msg=json.loads(sys.stdin.buffer.read(length))
    method=msg["method"]
    if method=="exit": break
    if "id" in msg:
        response={"jsonrpc":"2.0","id":msg["id"],"result":{"capabilities":{}} if method=="initialize" else None}
        data=json.dumps(response).encode()
        sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n"%len(data)+data)
        sys.stdout.buffer.flush()
''')
    module=runpy.run_path(str(HELPER))
    lsp=module['lsp']
    def fake_launch(argv, env, cwd, exe):
        return subprocess.Popen([sys.executable,str(script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=cwd,env=env)
    monkeypatch.setitem(lsp.__globals__,'launch',fake_launch)
    monkeypatch.setitem(lsp.__globals__,'wait_target',lambda proc, timeout: proc.wait(timeout=timeout))
    monkeypatch.setitem(lsp.__globals__,'DEADLINE',3)
    lsp('fixture',dict(os.environ),str(tmp_path))
