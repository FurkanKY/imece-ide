# Multi-Agent Kodlama Asistanı

Farklı yapay zeka modellerini (**Claude · DeepSeek · Gemini**) bir "yazılım ekibi"
gibi çalıştıran, çok-modelli bir kodlama asistanı. Her model bir role atanır
(planlama / kod yazma / inceleme), bir orkestratör onları sırayla çalıştırır ve
aralarında bilgi taşır.

Arayüzleri: **web-shell masaüstü (`shell.py`)** ★, **terminal**, **web** ve eski
**masaüstü classic (`desktop.py`)**. Hedef, bunu içinde multi-agent sistemi olan modern
bir mini-IDE'ye dönüştürmek.

> **⚠️ Aktif dönüşüm (`web-shell` branch):** masaüstü arayüzü QWidgets/QSS'ten **tek
> `QWebEngineView` içinde web-shell'e** (React+TS+Vite+Tailwind) taşınıyor; uygulama
> yerleşik Windows programı kalır, motor değişmez. Neden ve plan: `.claude/plans/
> web-shell-ui.md` + [docs/HANDOFF.md](docs/HANDOFF.md). Eski `desktop.py` cutover'a dek
> `python shell.py --classic` ile çalışır.

---

## Hızlı başlangıç

```bash
pip install -r requirements.txt
# .env dosyasını doldur (DEEPSEEK_API_KEY, GEMINI_API_KEY). Claude için anahtar gerekmez.

cd web/ui && npm ci && npm run build && cd ../..   # web-shell arayüzünü derle (bir kez)
python shell.py                   # ★ web-shell masaüstü mini-IDE
python shell.py --classic         # eski Qt arayüzü (desktop.py)
python app.py                     # web arayüzü  -> http://127.0.0.1:5000
python orchestrator.py "..." --run  # terminal (sıfırdan tek dosya üret + çalıştır)
```

Ayrıntılı kurulum ve API anahtarları için → [docs/SETUP.md](docs/SETUP.md)

---

## Ekip / roller

| Rol | Varsayılan model | Görevi |
|-----|------------------|--------|
| **Planner** | Claude (Claude Code headless) | Görevi adımlara böler, dosyaları seçer |
| **Coder** | DeepSeek | Planı koda / diff'e döker |
| **Reviewer** | Gemini (flash) | Kodu/diff'i inceler, hata bulur |

Roller arayüzden yeniden atanabilir (model yönlendirme). Nedenleri ve model adları →
[docs/MODELS.md](docs/MODELS.md)

## İki çalışma modu

1. **Sıfırdan üretim** — tek dosya üretir, çalıştırır, inceler, düzeltir (`runner.py`).
2. **Lokal proje üzerinde çalışma** — var olan bir projeyi okur, ilgili dosyaları seçer,
   değişiklikleri **diff** olarak önerir; onayınca uygular (`project_runner.py`).

## Uygulanan optimizasyonlar

Execution grounding (kodu gerçekten çalıştırıp hatayı geri besleme) · model yönlendirme ·
gözlemlenebilirlik (süre/token/maliyet) · yansıma (reflection) döngüsü.
Ayrıntı → [docs/OPTIMIZATIONS.md](docs/OPTIMIZATIONS.md)

---

## Dokümantasyon

| Belge | İçerik |
|-------|--------|
| [SETUP.md](docs/SETUP.md) | Kurulum, API anahtarları, ortam tuzakları (Python/encoding) |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Sistem mimarisi, modüller, veri akışı, olay tipleri |
| [USAGE.md](docs/USAGE.md) | Üç arayüzün kullanımı, örnekler |
| [OPTIMIZATIONS.md](docs/OPTIMIZATIONS.md) | Multi-agent optimizasyon teknikleri |
| [MODELS.md](docs/MODELS.md) | Model sağlayıcıları, çalışan model adları, fiyatlandırma |
| [ROADMAP.md](docs/ROADMAP.md) | Mini-IDE yol haritası (Monaco + konsol + debugger) |
| [DESIGN.md](docs/DESIGN.md) | Tasarım tokenları + ASTRYX/Linear referans kalibrasyonu |
| [CONTRIBUTING.md](docs/CONTRIBUTING.md) | Çalışma kuralları + **dokümantasyon güncelleme kuralı** |
| [CHANGELOG.md](docs/CHANGELOG.md) | Sürüm/iterasyon günlüğü |
| [HANDOFF.md](docs/HANDOFF.md) | **Devam notu** — yeni sohbette kaldığın yerden başlamak için |

## Dosya haritası

```
# --- Motor (tüm arayüzler paylaşır; DOKUNULMAZ) ---
adapters.py        modellere bağlanan fonksiyonlar (metin + token + maliyet)
agents.py          rol talimatları + routing
runner.py          sıfırdan üretim orkestrasyonu (execution grounding)
project.py         lokal proje araçları (listele/oku/diff/uygula, yol güvenliği)
project_runner.py  proje üzerinde çalışma orkestrasyonu (diff öner)
history.py         oturum geçmişi kalıcılığı (proje-içi .magent/history.json)
orchestrator.py    terminal arayüzü
app.py + templates/  web arayüzü

# --- ★ Yeni web-shell masaüstü arayüzü (shell.py) ---
shell.py           web-shell girişi (--dev / --classic)
webhost/           PySide6 host: scheme (app://) · bridge (RPC) · window (frameless) ·
                   state (aktif proje) · api/ (app·settings·project·fs domain handler'ları)
web/ui/            React+TS+Vite+Tailwind UI: bridge/ (protocol+qt+mock) · state/ (zustand)
                   · components/ (titlebar·activitybar·explorer·editor·statusbar·welcome)
                   · styles/tokens.css (theme.py CSS portu) · lib/ (monaco·keymap·fileIcons)
ui_prefs.py        arayüz tercihleri kalıcılığı v2 (accent·window·son projeler)
tools/webshot.mjs  görsel öz-doğrulama (Playwright + mock bridge → .uishots/*.png)
tests/test_bridge.py  köprü sözleşme testleri (webview'suz, pytest)
assets/fonts/      gömülü fontlar (Inter UI + JetBrains Mono, SIL OFL)

# --- Eski masaüstü classic (cutover'da silinecek) ---
desktop.py · editor_panel.py · agent_pipeline.py · changes_panel.py · chat_view.py ·
command_palette.py · settings_panel.py · history_panel.py · terminal.py · bottom_panel.py ·
theme.py · anim.py · chrome.py · tools/uishot.py · web/editor/

.env               API anahtarları (gizli)
docs/              bu dokümantasyon
```

## Gereksinimler

Python 3.14 · **PySide6 ≥ 6.11.1** (Py3.14) · Claude Code CLI (Pro/Max aboneliği) ·
DeepSeek ve Gemini API anahtarları · `requirements.txt` (requests, python-dotenv, flask,
PySide6, qtawesome, pywinpty) · **Node ≥ 20 + npm** (web-shell arayüzü `web/ui`'ı derlemek
için, bkz. [SETUP](docs/SETUP.md) 2b).
