"""jsonrpc.py (Content-Length framing) testleri — webview'suz.

Ek olarak basedpyright kuruluysa gerçek dil sunucusuyla initialize el sıkışması
duman testi yapılır (LSP köprüsünün tel-biçimi kanıtı; Qt gerektirmez).
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webhost.jsonrpc import encode, FrameDecoder  # noqa: E402


def test_encode_decode_roundtrip():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"ç": "ü"}}
    dec = FrameDecoder()
    out = dec.feed(encode(msg))
    assert out == [msg]


def test_decoder_handles_split_chunks():
    """Chunk sınırı başlık ve gövdenin ortasına düşebilir."""
    msg = {"id": 7, "result": {"x": list(range(50))}}
    wire = encode(msg)
    dec = FrameDecoder()
    collected = []
    for i in range(0, len(wire), 11):  # 11 baytlık parçalar
        collected += dec.feed(wire[i:i + 11])
    assert collected == [msg]


def test_decoder_handles_two_messages_in_one_chunk():
    a, b = {"id": 1, "result": None}, {"method": "n", "params": {}}
    dec = FrameDecoder()
    assert dec.feed(encode(a) + encode(b)) == [a, b]


def test_decoder_recovers_from_bad_header():
    dec = FrameDecoder()
    good = {"id": 2, "result": "ok"}
    out = dec.feed(b"X-Bozuk: evet\r\n\r\n" + encode(good))
    assert out == [good]


@pytest.mark.skipif(shutil.which("basedpyright-langserver") is None,
                    reason="basedpyright kurulu değil")
def test_basedpyright_initialize_handshake(tmp_path):
    """Gerçek LS ile initialize → yanıt (id eşleşir, capabilities döner)."""
    proc = subprocess.Popen(
        [shutil.which("basedpyright-langserver"), "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd=str(tmp_path),
    )
    try:
        uri = tmp_path.as_uri()
        proc.stdin.write(encode({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"processId": None, "rootUri": uri, "capabilities": {},
                       "workspaceFolders": [{"uri": uri, "name": "t"}]},
        }))
        proc.stdin.flush()
        dec = FrameDecoder()
        reply = None
        for _ in range(200):  # ~LS açılışı; ilk yanıt genelde <5sn
            data = proc.stdout.read1(65536)
            if not data:
                break
            for msg in dec.feed(data):
                if msg.get("id") == 1:
                    reply = msg
                    break
            if reply:
                break
        assert reply is not None, "initialize yanıtı gelmedi"
        assert "capabilities" in reply.get("result", {})
    finally:
        proc.kill()
