"""jsonrpc.py — Content-Length çerçeveli JSON mesaj kodeki.

LSP (P7, basedpyright) ve DAP (P8, debugpy) aynı tel biçimini kullanır:
    Content-Length: <n>\r\n
    \r\n
    <n baytlık UTF-8 JSON gövde>
Bu modül tek doğruluk kaynağı: encode() gönderim, FrameDecoder alım tarafı.
"""

import json


def encode(msg: dict) -> bytes:
    """Tek mesajı tel biçimine çevirir."""
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


class FrameDecoder:
    """Artımlı çözücü: feed(bytes) → o chunk'la TAMAMLANAN mesajların listesi.

    Chunk sınırları mesaj sınırlarıyla örtüşmek zorunda değildir (soket/boru
    gerçeği); yarım başlık/gövde tamponda bekletilir.
    """

    def __init__(self):
        self._buf = b""

    def feed(self, data: bytes) -> list[dict]:
        self._buf += data
        out: list[dict] = []
        while True:
            head_end = self._buf.find(b"\r\n\r\n")
            if head_end < 0:
                break
            length = None
            for line in self._buf[:head_end].split(b"\r\n"):
                key, _, val = line.partition(b":")
                if key.strip().lower() == b"content-length":
                    try:
                        length = int(val.strip())
                    except ValueError:
                        pass
            if length is None:
                # bozuk başlık bloğu — atla ve akışı kurtar
                self._buf = self._buf[head_end + 4:]
                continue
            start = head_end + 4
            if len(self._buf) < start + length:
                break  # gövde henüz tam gelmedi
            body = self._buf[start:start + length]
            self._buf = self._buf[start + length:]
            try:
                out.append(json.loads(body.decode("utf-8", errors="replace")))
            except ValueError:
                pass  # bozuk gövde — çerçeve sınırı korunur, akış sürer
        return out
