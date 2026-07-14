# Kurulum

## 1. Gereksinimler

| Bileşen | Sürüm / not |
|---------|-------------|
| Python | 3.14 (bu makinede kurulu) |
| Claude Code CLI | `claude --version` çalışmalı; Pro/Max aboneliği yeter (API anahtarı gerekmez) |
| DeepSeek API anahtarı | https://platform.deepseek.com → API Keys |
| Gemini API anahtarı | https://aistudio.google.com/apikey |

## 2. Bağımlılıklar

```bash
pip install -r requirements.txt
```

Test ve katkı ortamında bunun yerine `pip install -r requirements-dev.txt`
kullanın; bu dosya uygulama bağımlılıklarına ek olarak `pytest` içerir.

İçerik: `requests`, `python-dotenv`, `flask` (web arayüzü), `PySide6 ≥ 6.11.1`
(masaüstü; Python 3.14 gerekliliği), `pywinpty` (entegre terminal, ConPTY),
`basedpyright` (Python IntelliSense — Node gömülü gelir, ek kurulum istemez).

**Yazı tipi:** arayüz Windows'ta `Segoe UI Variable` / `Segoe UI` sistem zincirini,
kod ve sayısal veri için gömülü JetBrains Mono'yu kullanır. Ek kurulum gerekmez.
Eski `Inter.ttf` repo uyumluluğu için durur fakat arayüz tarafından yüklenmez.

## 2b. Masaüstü arayüzü (`shell.py`)

Arayüz `web/ui/` altında Vite ile derlenir (Node ≥ 20 gerekir; Monaco/xterm dahil tüm
web bağımlılıkları npm ile gelir):

```bash
cd web/ui
npm ci            # ilk kurulumda (veya npm install)
npm run build     # → web/ui/dist  (python shell.py bunu app:// üzerinden yükler)
```

Geliştirme: `npm run dev` (HMR, mock-bridge ile düz tarayıcıda) + `python shell.py --dev`
(aynı arayüz gerçek köprüyle yerleşik pencerede). Görsel doğrulama:
`node tools/webshot.mjs` → `.uishots/*.png`.

## 4. API anahtarları (.env, kaynak geliştirme)

`.env.example` dosyasını `.env` olarak kopyala ve doldur:

```ini
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-chat        # veya deepseek-v4-pro

GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.5-flash       # hesabında olan bir model (bkz. MODELS.md)

CLAUDE_CLI=claude                   # Claude için anahtar YOK, sadece CLI adı
```

> **Önemli:** Model adları API'nin gerçek adları olmalı. `gemini-3.1-pro` gibi bir ad
> 404/429 verir. Çalışan adlar için → [MODELS.md](MODELS.md).

`.env` gizlidir ve `.gitignore` ile dışlanmıştır — kimseyle paylaşma. Bu yöntem
yalnız kaynak geliştirme içindir. Paketli Windows uygulamasında anahtarları
Ayarlar'dan girin; Windows DPAPI bunları mevcut kullanıcı hesabına bağlı şifreli
depoda saklar. Eski paket `.env` anahtarları ilk Ayarlar durum kontrolünde taşınır
ve düz metin değerleri temizlenir.

## 5. Doğrulama

```bash
python -c "from dotenv import load_dotenv; load_dotenv(); \
from adapters import call_deepseek, call_gemini; \
print(call_deepseek('Kısa cevap.', 'tek kelime: test')); \
print(call_gemini('Kısa cevap.', 'tek kelime: test'))"
```

Claude'u ayrıca test etmek için:

```bash
python -c "from adapters import call_claude; print(call_claude('Kısa cevap.', '2+2?'))"
```

---

## Ortam tuzakları (bu makineye özgü, önemli)

Bu üç konu, geliştirme sırasında zaman kaybettiren gerçek tuzaklardı; not edildi.

### A) git-bash'te `python` bulunamıyor

git-bash'te `python`, gerçek yorumlayıcı yerine **Microsoft Store yönlendirme
stub'ına** (`AppData/Local/Microsoft/WindowsApps/python`) düşebilir ve
"Python bulunamadı" verir.

- **PowerShell'de** `python` genelde doğru çalışır — komutları orada çalıştır.
- git-bash'te gerçek yorumlayıcının tam yolu:
  `/c/Users/<kullanıcı>/AppData/Local/Python/bin/python`
- Kalıcı çözüm: Windows Ayarları → *Uygulama yürütme takma adları* → `python.exe`
  ve `python3.exe` toggle'larını kapat; ve gerçek Python'u PATH'e ekle.

### B) Windows Türkçe (cp1254) kodlaması

Alt-süreçle kod çalıştırınca (execution grounding, konsol) çocuk süreç Türkçe
karakterleri cp1254 ile yazar; utf-8 çözme çökerse çıktı sessizce kaybolur.
Kod bunu şöyle çözer (`runner.py: run_python_code`):

```python
env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
subprocess.run(..., encoding="utf-8", errors="replace", env=env)
```

Terminal çıktısında emoji için: `sys.stdout.reconfigure(encoding="utf-8")`.

### C) Flask debug reloader

`app.py` `use_reloader=False` ile çalışır; aksi halde her çalıştırmada yazılan
`output/result.py` reloader'ı tetikleyip devam eden isteği koparır.
