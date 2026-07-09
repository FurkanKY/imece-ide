# Multi-Agent Kodlama Asistanı

Farklı yapay zeka modellerini (**Claude · DeepSeek · Gemini**) bir "yazılım ekibi"
gibi çalıştıran, çok-modelli bir kodlama asistanı. Her model bir role atanır
(planlama / kod yazma / inceleme), bir orkestratör onları sırayla çalıştırır ve
aralarında bilgi taşır.

Ana arayüz: **masaüstü mini-IDE (`shell.py`)** — yerleşik PySide6 penceresi içinde
web teknolojisiyle çizilen (React+TS+Vite+Tailwind, Monaco, xterm.js) tam bir IDE:
dosya gezgini, çok sekmeli editör, gerçek terminal (ConPTY), projede arama, git
kaynak denetimi ve sağda canlı **AI EKİBİ paneli** (Planner→Coder→Reviewer pipeline,
merkez inline diff, uygula/vazgeç). Ek arayüzler: **terminal** ve **web**.

---

## Hızlı başlangıç

```bash
pip install -r requirements.txt
# .env dosyasını doldur (DEEPSEEK_API_KEY, GEMINI_API_KEY). Claude için anahtar gerekmez.

cd web/ui && npm ci && npm run build && cd ../..   # arayüzü derle (bir kez)
python shell.py                     # ★ masaüstü mini-IDE
python app.py                       # web arayüzü  -> http://127.0.0.1:5000
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
| [ROADMAP.md](docs/ROADMAP.md) | Yol haritası |
| [IDE-PLUS-PLAN.md](docs/IDE-PLUS-PLAN.md) | **Aktif program (P7–P10)**: LSP · F5/debug · eşli programlama · maliyet panosu |
| [AGENTIC-PLAN.md](docs/AGENTIC-PLAN.md) | **Sıradaki program (P11–P13)**: agentic araç döngüsü · bağlam motoru · orkestrasyon 2.0 |
| [DESIGN.md](docs/DESIGN.md) | Tasarım tokenları + ASTRYX/Linear referans kalibrasyonu |
| [CONTRIBUTING.md](docs/CONTRIBUTING.md) | Çalışma kuralları + **dokümantasyon güncelleme kuralı** |
| [CHANGELOG.md](docs/CHANGELOG.md) | Sürüm/iterasyon günlüğü |
| [HANDOFF.md](docs/HANDOFF.md) | **Devam notu** — yeni sohbette kaldığın yerden başlamak için |

## Dosya haritası

```
# --- Motor (tüm arayüzler paylaşır; yalnız geriye-uyumlu genişletilebilir) ---
adapters.py        modellere bağlanan fonksiyonlar (metin + token + maliyet)
agents.py          rol talimatları + routing
runner.py          sıfırdan üretim orkestrasyonu (execution grounding)
project.py         lokal proje araçları (listele/oku/diff/uygula, yol güvenliği)
project_runner.py  proje üzerinde çalışma orkestrasyonu (diff öner)
history.py         oturum geçmişi kalıcılığı (proje-içi .magent/history.json)
runconfig.py       F5 "çalıştır" komut sezgisi (.magent/run.json)
orchestrator.py    terminal arayüzü
app.py + templates/  web arayüzü

# --- ★ Masaüstü mini-IDE (shell.py) ---
shell.py           giriş (--dev = Vite HMR + DevTools)
webhost/           PySide6 host: scheme (app://) · bridge (RPC) · jsonrpc (LSP/DAP
                   çerçeveleme) · window (frameless + kapatma koruması) · state (aktif
                   proje) · watcher (fs.changed) · api/ (app·settings·project·fs·session·
                   run·history·terminal·search·scm·lsp·exec)
web/ui/            React+TS+Vite+Tailwind UI: bridge/ (protocol+qt+mock) · state/ (zustand)
                   · components/ (titlebar·activitybar·explorer·editor·scm·search·aipanel·
                   bottompanel·statusbar·welcome·palette·dialogs·toasts·settings) ·
                   styles/tokens.css (tasarım tokenları) · lib/ (monaco·lsp·keymap·commands…)
ui_prefs.py        arayüz tercihleri kalıcılığı (accent·window·son projeler)
tools/webshot.mjs  görsel öz-doğrulama (Playwright + mock bridge → .uishots/*.png)
tools/webshot-interact.mjs  etkileşim görüntüleri (palet·menü·diyalog·toast)
tests/             pytest: test_bridge (köprü sözleşmesi) · test_jsonrpc (çerçeveleme +
                   canlı basedpyright el sıkışması) · test_runconfig (komut sezgisi)

.env               API anahtarları (gizli)
docs/              bu dokümantasyon
```

## Gereksinimler

Python 3.14 · **PySide6 ≥ 6.11.1** (Py3.14) · Claude Code CLI (Pro/Max aboneliği) ·
DeepSeek ve Gemini API anahtarları · `requirements.txt` (requests, python-dotenv, flask,
PySide6, pywinpty, basedpyright) · **Node ≥ 20 + npm** (arayüzü `web/ui`'da derlemek
için, bkz. [SETUP](docs/SETUP.md) 2b).
