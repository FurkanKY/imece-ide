"""
theme.py
--------
Ortak renk paleti + ikon yardımcısı. desktop.py ve widget'lar (agent_pipeline.py)
bunu paylaşır — tek yerden tutarlı görünüm.
"""

import os
import tempfile

from PySide6.QtGui import QIcon, QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect
import qtawesome as qta

# Renk paleti — Cursor/Linear tonunda, near-black taban + net yükseklik kademeleri.
# Yüzey kademeleri: bg < activity/side < panel < card < card2 (yukarı çıktıkça açılır).
C = {
    "bg": "#0a0b0d",         # uygulama tabanı (near-black)
    "activity": "#0c0d10",   # aktivite çubuğu
    "side": "#0e0f12",       # gezgin
    "panel": "#131418",      # yükseltilmiş yüzey (paneller/loglar)
    "panel2": "#17181d",     # panel üstü hafif daha açık (gradyan tepe)
    "editor": "#1e1e1e",     # Monaco arka planı (kendi teması)
    "composer": "#131418",   # alt composer kartı
    "card": "#191b21",       # kartlar / butonlar
    "card2": "#20232b",      # hover / kabarık kart
    "field": "#0b0c0f",      # giriş alanı zemini
    "status": "#0a0b0d",     # durum çubuğu
    "line": "#1b1d23",       # çok ince ayraç
    # Kenarlar: ASTRYX/Linear tekniği — solid gri yerine YARI-SAYDAM BEYAZ; alttaki
    # yüzeye uyum sağlar, premium his verir (ASTRYX dark: #FFFFFF1A).
    "border": "rgba(255,255,255,0.10)",   # standart kenar
    "border2": "rgba(255,255,255,0.16)",  # belirgin kenar (combo/buton)
    "hair": "#34384340",     # tepe-parıltı (elevation highlight, alfa'lı)
    "text": "#f2f3f6", "text2": "#c6cad3", "muted": "#8b8f9b", "faint": "#565a63",
    "accent": "#6aa1ff", "accent2": "#5891f6", "accentdim": "#26344f", "on_accent": "#06122b",
    "green": "#4bd48a", "red": "#ff6b74", "amber": "#e9b45a",
}

# ---- Tasarım tokenları — tek ritim (aralık / radius / tip / hareket) ----
# Faz 6'da her yere uygulanır; build_style ve widget'lar buradan okur.
SP = {0: 0, 1: 4, 2: 8, 3: 12, 4: 16, 5: 20, 6: 24, 7: 32, 8: 40}
RADIUS = {"xs": 6, "sm": 8, "md": 11, "lg": 14, "xl": 18, "pill": 999}
TYPE = {  # rol -> (px, weight, letter-spacing px)
    "display": (20, 800, 0.2), "title": (14, 700, 0.2), "body": (13, 400, 0.0),
    "label": (12, 600, 0.1), "caption": (11, 500, 0.1),
    "overline": (10, 800, 1.4), "mono": (11, 500, 0.0),
}
MOTION = {"fast": 150, "base": 230, "slow": 340}

# Yoğunluk (density) — aralıkları ölçekler (ayarlardan seçilir)
_DENSITY = {"name": "comfortable", "k": 1.0}


def set_density(name):
    _DENSITY["name"] = name
    _DENSITY["k"] = 0.82 if name == "compact" else 1.0
    return name


def sp(n):
    """Density-ölçekli aralık (px)."""
    return max(0, round(SP.get(n, n) * _DENSITY["k"]))


# Accent seçenekleri (koyu içinde) — (accent, accent2, accentdim, on_accent)
ACCENTS = {
    "blue":   ("#6aa1ff", "#5891f6", "#26344f", "#06122b"),
    "indigo": ("#8b8cf0", "#7a7be6", "#2b2c4a", "#0c0b26"),
    "violet": ("#b07cf0", "#9d68e6", "#33244d", "#180a2b"),
    "green":  ("#4bd48a", "#3cc47b", "#193a2a", "#04180e"),
    "amber":  ("#e9b45a", "#e0a740", "#3a2e18", "#241703"),
    "rose":   ("#ff7a8a", "#f0637a", "#3d2028", "#2b0a12"),
}


def set_accent(name):
    """Aktif accent'i değiştirir (C sözlüğünü günceller); sonra build_style yeniden çağrılır."""
    a = ACCENTS.get(name)
    if a:
        C["accent"], C["accent2"], C["accentdim"], C["on_accent"] = a
    return name if a else None

# Fontlar — gömülü Inter (UI) + JetBrains Mono (kod). Yüklenemezse Windows fallback.
FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")


def load_fonts():
    """Gömülü variable fontları QFontDatabase'e yükler; aile adlarını FONT_UI/FONT_MONO'ya
    yazar. QApplication kurulduktan SONRA çağır. Yüklenemezse fallback korunur."""
    global FONT_UI, FONT_MONO
    from PySide6.QtGui import QFontDatabase

    def _load(filename, fallback):
        path = os.path.join(_FONT_DIR, filename)
        if os.path.exists(path):
            fid = QFontDatabase.addApplicationFont(path)
            fams = QFontDatabase.applicationFontFamilies(fid)
            if fams:
                return fams[0]
        return fallback

    FONT_UI = _load("Inter.ttf", FONT_UI)
    FONT_MONO = _load("JetBrainsMono.ttf", FONT_MONO)
    return FONT_UI, FONT_MONO


def _lighten(hexc, amt):
    """hex rengi amt*255 kadar açar (bevel highlight için)."""
    hexc = hexc.lstrip("#")
    r, g, b = int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16)
    f = lambda v: max(0, min(255, int(v + amt * 255)))
    return f"#{f(r):02x}{f(g):02x}{f(b):02x}"


def grad(top, bottom, bevel=True):
    """Dikey gradyan + tepede ince açık "bevel" çizgisi → yükseklik/fiziksellik hissi.
    QSS background değeri. bevel=False düz gradyan verir."""
    if bevel:
        hi = _lighten(top, 0.055)
        return (f"qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                f"stop:0 {hi}, stop:0.05 {top}, stop:1 {bottom})")
    return f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {top}, stop:1 {bottom})"


def elev(level):
    """Yükseklik kademesi (0..3) → beveled gradyan yüzey. Tutarlı derinlik için."""
    tops = {0: C["bg"], 1: C["panel2"], 2: C["card2"], 3: C["card2"]}
    bots = {0: C["bg"], 1: C["panel"], 2: C["card"], 3: C["composer"]}
    return grad(tops.get(level, C["card2"]), bots.get(level, C["card"]))


def add_shadow(widget, blur=34, y=10, alpha=150, x=0):
    """Yumuşak drop shadow — DERINLIK. DİKKAT: içinde QWebEngineView olan bir widget'a
    (veya atasına) UYGULAMA; graphics effect webview render'ını bozar. Ayrıca bir widget
    tek bir graphics effect alabilir — fade/breathe alan widget'a shadow verme."""
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(x, y)
    eff.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(eff)
    return eff

# Combo oku vb. için qtawesome ikonunu PNG'ye render edip QSS url()'ine ver.
_ICON_CACHE = os.path.join(tempfile.gettempdir(), "magent_icons")


def icon_png(name, color, size=14):
    """qtawesome ikonunu bir PNG dosyasına yazıp QSS-uyumlu (ileri eğik çizgili) yol döner."""
    os.makedirs(_ICON_CACHE, exist_ok=True)
    safe = name.replace(".", "_") + "_" + color.lstrip("#") + f"_{size}.png"
    path = os.path.join(_ICON_CACHE, safe)
    if not os.path.exists(path):
        try:
            qta.icon(name, color=color).pixmap(size, size).save(path)
        except Exception:
            return ""
    return path.replace("\\", "/")

# Dosya uzantısı -> (ikon adı, renk)
FILE_ICONS = {
    ".py": ("mdi6.language-python", "#4f8cff"),
    ".js": ("mdi6.language-javascript", "#e6c74f"), ".mjs": ("mdi6.language-javascript", "#e6c74f"),
    ".jsx": ("mdi6.language-javascript", "#e6c74f"),
    ".ts": ("mdi6.language-typescript", "#4f8cff"), ".tsx": ("mdi6.language-typescript", "#4f8cff"),
    ".json": ("mdi6.code-json", "#e6a23c"),
    ".md": ("mdi6.language-markdown", "#8a8a92"),
    ".html": ("mdi6.language-html5", "#ef5b5b"), ".css": ("mdi6.language-css3", "#4f8cff"),
    ".c": ("mdi6.language-c", "#5c9fff"), ".h": ("mdi6.language-c", "#5c9fff"),
    ".cpp": ("mdi6.language-cpp", "#5c9fff"), ".cc": ("mdi6.language-cpp", "#5c9fff"),
    ".hpp": ("mdi6.language-cpp", "#5c9fff"),
    ".java": ("mdi6.language-java", "#e6a23c"), ".go": ("mdi6.language-go", "#4fd0e6"),
    ".rs": ("mdi6.language-rust", "#c98a5b"), ".rb": ("mdi6.language-ruby", "#ef5b5b"),
    ".php": ("mdi6.language-php", "#8a8ad6"), ".sql": ("mdi6.database-outline", "#4fd0e6"),
    ".sh": ("mdi6.console", "#31c774"),
    ".yml": ("mdi6.file-cog-outline", "#8a8a92"), ".yaml": ("mdi6.file-cog-outline", "#8a8a92"),
    ".xml": ("mdi6.xml", "#8a8a92"), ".txt": ("mdi6.file-document-outline", "#8a8a92"),
    ".toml": ("mdi6.file-cog-outline", "#8a8a92"), ".cfg": ("mdi6.file-cog-outline", "#8a8a92"),
    ".env": ("mdi6.key-outline", "#e6a23c"),
}


def icon(name, color=None, active=False):
    """qtawesome ikonu (ad yoksa boş ikon — çökmesin)."""
    try:
        return qta.icon(name, color=color or (C["text"] if active else C["muted"]))
    except Exception:
        return QIcon()


def file_icon(filename, is_dir=False):
    """Dosya/klasör için uzantıya göre renkli ikon."""
    if is_dir:
        return icon("mdi6.folder", color="#7a8aa0")
    import os
    ext = os.path.splitext(filename)[1].lower()
    name, color = FILE_ICONS.get(ext, ("mdi6.file-outline", "#8a8a92"))
    return icon(name, color=color)
