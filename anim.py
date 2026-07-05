"""
anim.py
-------
Premium hareket katmanı. Qt stylesheet (QSS) CSS-benzeri `transition` desteklemez;
akıcı geçişler için QPropertyAnimation / QGraphicsOpacityEffect tabanlı yardımcılar.

Tasarım ilkeleri:
  - Fade ve pozisyon animasyonları en güvenli/en temiz görüneni; renk churn'ünden kaçın.
  - Opacity efekti metni hafif donuklaştırabildiğinden, fade bitince efekti KALDIR
    (crisp render'a dön).
  - QWebEngineView'e (Monaco editör) graphics effect UYGULAMA — render'ı bozar.
"""

from PySide6.QtCore import (
    QPropertyAnimation, QEasingCurve, QAbstractAnimation, QVariantAnimation,
    QObject, QPoint,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect

# Süreler (ms) — kısa ama fark edilir; premium his için "snappy".
FAST, BASE, SLOW = 150, 230, 340
EASE = QEasingCurve.Type.OutCubic

# Küresel açık/kapalı (ayarlardan). Kapalıyken yardımcılar anında son duruma geçer.
_ENABLED = True


def set_enabled(on):
    global _ENABLED
    _ENABLED = bool(on)


def is_enabled():
    return _ENABLED


def fade_in(widget, dur=BASE, start=0.0, ease=EASE, remove=True):
    """Widget'ı `start` opaklığından 1.0'a yumuşakça getir; bitince efekti kaldır."""
    if not _ENABLED:
        widget.setGraphicsEffect(None)
        return None
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(start)
    widget.setGraphicsEffect(eff)
    a = QPropertyAnimation(eff, b"opacity", widget)
    a.setDuration(dur)
    a.setStartValue(start)
    a.setEndValue(1.0)
    a.setEasingCurve(ease)
    if remove:
        a.finished.connect(lambda: widget.setGraphicsEffect(None))
    a.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    widget._anim_fade = a  # referansı canlı tut
    return a


def animate_geometry(widget, end_rect, dur=BASE, ease=EASE):
    """Widget geometrisini (QRect) hedefe akıcı taşı — serbest konumlu öğeler için."""
    if not _ENABLED:
        widget.setGeometry(end_rect)
        return None
    a = QPropertyAnimation(widget, b"geometry", widget)
    a.setDuration(dur)
    a.setEndValue(end_rect)
    a.setEasingCurve(ease)
    a.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    widget._anim_geo = a
    return a


def animate_height(widget, start, end, dur=BASE, ease=EASE, on_done=None):
    """maximumHeight'i animasyonla değiştir — kart aç/kapa (expand/collapse)."""
    if not _ENABLED:
        widget.setMaximumHeight(int(end))
        if on_done:
            on_done()
        return None
    a = QPropertyAnimation(widget, b"maximumHeight", widget)
    a.setDuration(dur); a.setStartValue(int(start)); a.setEndValue(int(end))
    a.setEasingCurve(ease)
    if on_done:
        a.finished.connect(on_done)
    a.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    widget._anim_h = a
    return a


def pop_in(widget, dur=BASE, dy=10, ease=EASE):
    """Fade + yukarıdan hafif kayma (overlay/palette açılışı). Serbest konumlu üst-düzey
    widget'lar için pos animasyonu; içerik widget'ı için sadece fade."""
    if not _ENABLED:
        return
    fade_in(widget, dur=dur, ease=ease)
    try:
        from PySide6.QtCore import QPoint
        end = widget.pos()
        widget.move(end + QPoint(0, -dy))
        a = QPropertyAnimation(widget, b"pos", widget)
        a.setDuration(dur); a.setEndValue(end); a.setEasingCurve(ease)
        a.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        widget._anim_pos = a
    except Exception:
        pass


class Blink(QObject):
    """Sürekli yanıp sönme (opacity) — 'yazıyor' imleci / aktif gösterge."""

    def __init__(self, widget, lo=0.25, hi=1.0, dur=850):
        super().__init__(widget)
        self.widget = widget
        self._eff = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(self._eff)
        self._a = QPropertyAnimation(self._eff, b"opacity", self)
        self._a.setDuration(dur)
        self._a.setKeyValueAt(0.0, hi); self._a.setKeyValueAt(0.5, lo); self._a.setKeyValueAt(1.0, hi)
        self._a.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._a.setLoopCount(-1)

    def start(self):
        if not _ENABLED:
            return
        self._a.start()

    def stop(self):
        self._a.stop(); self._eff.setOpacity(1.0); self.widget.setGraphicsEffect(None)


def count_to(setter, start, end, dur=SLOW, ease=EASE):
    """`start`→`end` arasını animasyonla gez, her adımda setter(değer) çağır (HUD sayaç)."""
    if not _ENABLED:
        setter(float(end))
        return None
    a = QVariantAnimation()
    a.setDuration(dur)
    a.setStartValue(float(start))
    a.setEndValue(float(end))
    a.setEasingCurve(ease)
    a.valueChanged.connect(lambda v: setter(float(v)))
    a.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return a


class Breathe(QObject):
    """Bir widget'a sürekli 'nefes' (opacity) nabzı — aktif ajan kartı için.
    Sert renk toggle yerine InOutSine ile yumuşak salınım."""

    def __init__(self, widget, lo=0.45, hi=1.0, dur=1100):
        super().__init__(widget)
        self.widget = widget
        self._eff = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(self._eff)
        self._a = QPropertyAnimation(self._eff, b"opacity", self)
        self._a.setDuration(dur)
        self._a.setKeyValueAt(0.0, hi)
        self._a.setKeyValueAt(0.5, lo)
        self._a.setKeyValueAt(1.0, hi)
        self._a.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._a.setLoopCount(-1)

    def start(self):
        if not _ENABLED:
            return
        self._a.start()

    def stop(self):
        self._a.stop()
        self._eff.setOpacity(1.0)
        self.widget.setGraphicsEffect(None)
