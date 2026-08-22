"""Continuously draining, bounded byte capture for child process pipes."""

from __future__ import annotations

CAPTURE_LIMIT = 64 * 1024
CAPTURE_HEAD = CAPTURE_LIMIT // 2
CAPTURE_TAIL = CAPTURE_LIMIT - CAPTURE_HEAD


class BoundedCapture:
    def __init__(self, limit: int = CAPTURE_LIMIT) -> None:
        if type(limit) is not int or limit <= 0:
            raise ValueError("BoundedCapture.limit must be a positive integer")
        self._limit = limit
        self._head_limit = limit // 2
        self._tail_limit = limit - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0
        self.error: BaseException | None = None

    def consume(self, stream) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                self._total += len(chunk)
                if len(self._head) < self._head_limit:
                    take = min(self._head_limit - len(self._head), len(chunk))
                    self._head.extend(chunk[:take])
                    chunk = chunk[take:]
                if chunk:
                    self._tail.extend(chunk)
                    if len(self._tail) > self._tail_limit:
                        del self._tail[:-self._tail_limit]
        except BaseException as exc:  # surfaced after process cleanup
            self.error = exc

    @property
    def total(self) -> int:
        return self._total

    @property
    def truncated(self) -> bool:
        return self._total > self._limit

    def text(self) -> str:
        data = bytes(self._head)
        if self.truncated:
            omitted = self._total - len(self._head) - len(self._tail)
            data += f"\n... <{omitted} bytes omitted> ...\n".encode("utf-8")
            data += bytes(self._tail)
        else:
            data += bytes(self._tail)
        return data.decode("utf-8", errors="replace")
