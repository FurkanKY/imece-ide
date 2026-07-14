"""DAP (debugpy) tel-protokolü duman testi — webview'suz, Qt'siz.

webhost/api/debug.py'nin kullandığı akışın saf soketle kanıtı:
  spawn(debugpy --listen --wait-for-client) → connect → initialize → attach →
  [initialized] → setBreakpoints → configurationDone → stopped(breakpoint) →
  stackTrace → scopes → variables → continue → terminated/exited.

Çerçeve: LSP ile ortak Content-Length (webhost/jsonrpc.py).
"""

import importlib.util
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webhost.jsonrpc import encode, FrameDecoder  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("debugpy") is None, reason="debugpy kurulu değil")

SCRIPT = """\
def topla(a, b):
    sonuc = a + b
    return sonuc

print("bas")
x = topla(2, 3)
print("son", x)
"""
BP_LINE = 2  # `sonuc = a + b`


class DapClient:
    """Test için minimal senkron DAP istemcisi."""

    def __init__(self, sock):
        self.sock = sock
        self.dec = FrameDecoder()
        self.seq = 0
        self.inbox: list[dict] = []

    def send(self, command: str, arguments: dict | None = None) -> int:
        self.seq += 1
        self.sock.sendall(encode({"seq": self.seq, "type": "request",
                                  "command": command, "arguments": arguments or {}}))
        return self.seq

    def _pump(self, timeout: float) -> None:
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return
        if data:
            self.inbox.extend(self.dec.feed(data))

    def wait(self, pred, timeout: float = 15.0, what: str = "mesaj") -> dict:
        """pred(msg) doğru olana dek mesaj bekle (öncekiler de taranır)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for i, msg in enumerate(self.inbox):
                if pred(msg):
                    return self.inbox.pop(i)
            self._pump(0.25)
        raise AssertionError(f"{what} gelmedi (timeout {timeout}s)")

    def wait_response(self, seq: int, what: str = "yanıt") -> dict:
        msg = self.wait(
            lambda m: m.get("type") == "response" and m.get("request_seq") == seq,
            what=what)
        assert msg.get("success"), f"{what} başarısız: {msg.get('message')}"
        return msg

    def wait_event(self, name: str) -> dict:
        return self.wait(
            lambda m: m.get("type") == "event" and m.get("event") == name,
            what=f"'{name}' olayı")


@pytest.fixture()
def debuggee(tmp_path):
    script = tmp_path / "prog.py"
    script.write_text(SCRIPT, encoding="utf-8")
    # serbest port
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen(
        [sys.executable, "-m", "debugpy", "--listen", f"127.0.0.1:{port}",
         "--wait-for-client", str(script)],
        cwd=str(tmp_path), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    # bağlanmayı dene (adapter açılışı sürebilir)
    sock = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=1.0)
            break
        except OSError:
            time.sleep(0.15)
    if sock is None:
        proc.kill()
        pytest.fail("debugpy'a bağlanılamadı")
    yield DapClient(sock), str(script), proc
    try:
        sock.close()
    except OSError:
        pass
    if proc.poll() is None:
        proc.kill()


def test_dap_full_flow(debuggee):
    cli, script, proc = debuggee

    # 1) initialize → attach
    seq = cli.send("initialize", {
        "clientID": "imece-test", "adapterID": "debugpy",
        "pathFormat": "path", "linesStartAt1": True, "columnsStartAt1": True,
        "supportsVariableType": True,
    })
    cli.wait_response(seq, "initialize")
    cli.send("attach", {"justMyCode": True})

    # 2) initialized olayı → breakpoint + configurationDone
    cli.wait_event("initialized")
    seq = cli.send("setBreakpoints", {
        "source": {"path": script},
        "breakpoints": [{"line": BP_LINE}],
    })
    r = cli.wait_response(seq, "setBreakpoints")
    bps = r["body"]["breakpoints"]
    assert bps and bps[0]["verified"], "breakpoint doğrulanmadı"
    seq = cli.send("configurationDone")
    cli.wait_response(seq, "configurationDone")

    # 3) breakpoint'te durur
    stopped = cli.wait_event("stopped")
    assert stopped["body"]["reason"] == "breakpoint"
    tid = stopped["body"]["threadId"]

    # 4) yığın → doğru dosya/satır; kapsam → değişkenler doğru okunur
    seq = cli.send("stackTrace", {"threadId": tid, "startFrame": 0, "levels": 10})
    frames = cli.wait_response(seq, "stackTrace")["body"]["stackFrames"]
    assert frames[0]["line"] == BP_LINE
    assert frames[0]["name"] == "topla"
    assert Path(frames[0]["source"]["path"]).name == "prog.py"

    seq = cli.send("scopes", {"frameId": frames[0]["id"]})
    scopes = cli.wait_response(seq, "scopes")["body"]["scopes"]
    ref = scopes[0]["variablesReference"]
    seq = cli.send("variables", {"variablesReference": ref})
    variables = {v["name"]: v["value"]
                 for v in cli.wait_response(seq, "variables")["body"]["variables"]}
    assert variables.get("a") == "2" and variables.get("b") == "3"

    # 5) next → bir satır ilerler (sonuc artık tanımlı)
    seq = cli.send("next", {"threadId": tid})
    cli.wait_response(seq, "next")
    stopped = cli.wait_event("stopped")
    assert stopped["body"]["reason"] == "step"
    seq = cli.send("stackTrace", {"threadId": tid, "startFrame": 0, "levels": 5})
    frames = cli.wait_response(seq, "stackTrace")["body"]["stackFrames"]
    assert frames[0]["line"] == BP_LINE + 1

    # 6) continue → program biter
    seq = cli.send("continue", {"threadId": tid})
    cli.wait_response(seq, "continue")
    cli.wait_event("terminated")
    proc.wait(timeout=10)
    out = proc.stdout.read().decode("utf-8", errors="replace")
    assert "bas" in out and "son 5" in out
