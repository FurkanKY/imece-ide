"""_clamp_i32 testi — ConPTY başarısızlığında büyük çıkış kodları Qt Signal(int)
taşmasına (traceback'siz OverflowError, Beta-3 blokeri) yol açmamalı."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# terminal.py PySide6 import eder; yoksa test atlanır (CI/başsız ortam güvenliği).
import pytest

winpty_or_qt = pytest.importorskip("PySide6.QtCore")

from webhost.api.terminal import _clamp_i32  # noqa: E402

I32_MAX = 2_147_483_647
I32_MIN = -2_147_483_648


def test_normal_codes_passthrough():
    assert _clamp_i32(0) == 0
    assert _clamp_i32(1) == 1
    assert _clamp_i32(-1) == -1
    assert _clamp_i32(I32_MAX) == I32_MAX
    assert _clamp_i32(I32_MIN) == I32_MIN


def test_none_is_zero():
    assert _clamp_i32(None) == 0


def test_conpty_failure_code_does_not_overflow():
    # 0xC000013A (STATUS_CONTROL_C_EXIT) ve 4294967295 (unsigned -1) — int32'yi aşar
    for bad in (0xC000013A, 4294967295, 0xFFFFFFFF, 3221225786):
        out = _clamp_i32(bad)
        assert I32_MIN <= out <= I32_MAX, f"{bad} -> {out} hâlâ taşıyor"


def test_garbage_is_safe():
    assert _clamp_i32("değil-sayı") == -1
    assert _clamp_i32(object()) == -1
