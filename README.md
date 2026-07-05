# Multi-Agent Kodlama Asistanı

Farklı yapay zeka modellerini (**Claude · DeepSeek · Gemini**) bir "yazılım ekibi"
gibi çalıştıran, çok-modelli bir kodlama asistanı. Her model bir role atanır
(planlama / kod yazma / inceleme), bir orkestratör onları sırayla çalıştırır ve
aralarında bilgi taşır.

Üç arayüzü var: **terminal**, **web** ve **masaüstü (PySide6)**. Hedef, bunu içinde
multi-agent sistemi olan modern bir mini-IDE'ye dönüştürmek ([yol haritası](docs/ROADMAP.md)).

---

## Hızlı başlangıç

```bash
pip install -r requirements.txt
# .env dosyasını doldur (DEEPSEEK_API_KEY, GEMINI_API_KEY). Claude için anahtar gerekmez.

python desktop.py                 # masaüstü uygulaması (lokal projede çalışmak için)
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
adapters.py        modellere bağlanan fonksiyonlar (metin + token + maliyet)
agents.py          rol talimatları + routing
runner.py          sıfırdan üretim orkestrasyonu (execution grounding)
project.py         lokal proje araçları (listele/oku/diff/uygula, yol güvenliği)
project_runner.py  proje üzerinde çalışma orkestrasyonu (diff öner)
orchestrator.py    terminal arayüzü
app.py + templates/  web arayüzü
desktop.py         masaüstü mini-IDE (PySide6): gezgin + Monaco editör + sağ AI paneli
editor_panel.py    Monaco editör köprüsü (QWebEngineView + QWebChannel)
agent_pipeline.py  canlı EKİP timeline (Planner→Coder→Reviewer, yatay/dikey)
changes_panel.py   dosya bazında kabul/ret + Reviewer verdict rozeti + diff
theme.py           palet + gradyan/derinlik + ikon/font yükleyici
anim.py            premium hareket katmanı (fade, kayan gösterge, nefes, count-up; set_enabled)
chrome.py          frameless özel başlık çubuğu (Qt6 startSystemMove/Resize)
chat_view.py       kart tabanlı sohbet akışı (aşama kartları, streaming, verdict)
command_palette.py Ctrl+K komut / Ctrl+P dosya paleti (fuzzy overlay)
settings_panel.py  Ayarlar overlay'i (accent · yoğunluk · Enter · animasyon)
ui_prefs.py        arayüz tercihleri kalıcılığı (JSON, ~/.multi_agent_ide/prefs.json)
history.py         oturum geçmişi kalıcılığı (proje-içi .magent/history.json)
history_panel.py   geçmiş overlay'i (görev→sonuç→maliyet; tıkla→geri yükle)
terminal.py        entegre terminal çekirdeği (QProcess, canlı çıktı, geçmiş, cd)
bottom_panel.py    alt panel (çok sekmeli terminal; Ctrl+`)
tools/uishot.py    görsel öz-doğrulama (offscreen render → .uishots/*.png)
web/editor/        Monaco editör web-uygulaması (index.html, editor.js, vs/)
assets/fonts/      gömülü fontlar (Inter UI + JetBrains Mono, SIL OFL)
.env               API anahtarları (gizli)
docs/              bu dokümantasyon
```

## Gereksinimler

Python 3.14 · Claude Code CLI (Pro/Max aboneliği) · DeepSeek ve Gemini API anahtarları ·
`requirements.txt` (requests, python-dotenv, flask, PySide6, qtawesome) ·
Monaco editör (npm ile yerele indirilir, bkz. SETUP).
