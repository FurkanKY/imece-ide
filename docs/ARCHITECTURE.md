# Mimari

## Genel bakış

Sistem üç katmandan oluşur:

```
┌─────────────────────────────────────────────────────────────┐
│  ARAYÜZLER   orchestrator.py (CLI) · app.py (web) · desktop.py │
│              desktop.py = mini-IDE: dosya gezgini + Monaco     │
│              editör (editor_panel.py + web/editor/) + ajan akışı│
├─────────────────────────────────────────────────────────────┤
│  ORKESTRASYON   runner.py (üretim)  ·  project_runner.py (proje)│
│                 → olay (event) üreten generator'lar            │
├─────────────────────────────────────────────────────────────┤
│  AJANLAR     agents.py (rol + routing)                        │
│  ADAPTÖRLER  adapters.py (claude / deepseek / gemini)         │
│  ARAÇLAR     project.py (dosya listele/oku/diff/uygula)       │
│  EDİTÖR      editor_panel.py + web/editor/ (Monaco, QWebChannel)│
└─────────────────────────────────────────────────────────────┘
```

Temel tasarım ilkesi: **her katman bir alttakine bağımlı, üsttekinden habersiz.**
Arayüzler orkestratörü çağırır; orkestratör ajanları; ajanlar adaptörleri. Bu sayede
aynı motor üç farklı arayüzde değişmeden kullanılır.

---

## Katman 1 — Adaptörler (`adapters.py`)

Üç modelin "konuşma biçimi" farklıdır (Claude bir CLI, DeepSeek/Gemini birer web API'si).
Adaptörler hepsini **tek ortak imzaya** indirger:

```python
call_claude(system_prompt, user_prompt)   -> LLMResponse
call_deepseek(system_prompt, user_prompt) -> LLMResponse
call_gemini(system_prompt, user_prompt)   -> LLMResponse
```

`LLMResponse` sadece metni değil, **gözlemlenebilirlik** verisini de taşır:

```python
@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    cost_usd: float
```

- **Claude:** `claude -p <prompt> --output-format json` alt-süreç olarak çağrılır;
  JSON'dan `result`, `total_cost_usd`, `usage.input/output_tokens` okunur. Pro aboneliğini
  kullanır, API anahtarı gerekmez.
- **DeepSeek:** OpenAI-uyumlu `POST /chat/completions`. Fiyat `PRICING` tablosundan tahmin.
- **Gemini:** `POST .../models/{model}:generateContent`. Token `usageMetadata`'dan.

`PROVIDERS = {"claude": ..., "deepseek": ..., "gemini": ...}` sözlüğü isimle erişim sağlar.

## Katman 2 — Ajanlar (`agents.py`)

Bir ajan = **provider + rol talimatı**.

```python
ROLE_PROMPTS   = {"planner": "...", "coder": "...", "reviewer": "..."}
DEFAULT_ROUTING = {"planner": "claude", "coder": "deepseek", "reviewer": "gemini"}

class Agent:
    def run(self, user_prompt) -> LLMResponse   # provider'ı çağırır

build_agents(routing) -> {"planner": Agent, "coder": Agent, "reviewer": Agent}
```

`routing`'i değiştirmek = model yönlendirme (bir rolü başka modele almak). Talimatlar
(`ROLE_PROMPTS`) sabit kalır; sadece hangi modelin çalıştığı değişir.

## Katman 3 — Orkestrasyon

İki orkestratör de **generator**'dır: adım adım "olay" (`dict`) `yield` eder. Arayüzler
bu olayları canlı gösterir. Bu, akışı hem CLI'de hem web'de hem masaüstünde aynı tutar.

### `runner.py` — sıfırdan üretim

`run_task(task, routing, max_rounds, run_python)` akışı:

```
PLAN (Planner) → CODE (Coder) → [ çalıştır → REVIEW → düzelt ]×N → kaydet
```

`run_python_code(code, execute)` → derleme (`py_compile`) + isteğe bağlı çalıştırma;
`(ok, çıktı)` döner. Bu **execution grounding**'in çekirdeğidir.

**Olay tipleri:** `stage`, `metric`, `output`, `exec`, `note`, `done`.

### `project_runner.py` — lokal proje

`run_project_task(project_root, task, routing)` akışı:

```
Planner projeyi görür + 'FILES:' ile dosya seçer
  → seçili dosyalar okunur
  → Coder '### FILE:' bloklarıyla tam yeni içerik üretir
  → bloklar diff'e çevrilir
  → Reviewer diff'i inceler
  → 'proposal' olayı (arayüz onaya sunar)
```

Yardımcılar: `_parse_requested_files` (Planner'ın FILES listesi),
`_parse_file_blocks` (Coder'ın FILE blokları).

**Olay tipleri:** `info`, `stage`, `metric`, `output`, `diff`, `proposal`.

## Katman — Editör (`editor_panel.py` + `web/editor/`)

Masaüstü mini-IDE'nin kod editörü, `QWebEngineView` içine gömülü **Monaco** (VS Code'un
editörü). Python ile JavaScript arasında **QWebChannel** köprüsü kurulur:

```
editor_panel.py (Python)                 web/editor/ (JavaScript)
  EditorPanel(QWidget)                     index.html + editor.js + vs/ (Monaco)
    ├─ _run_js(...) ───────────────────▶  window.API.openFile/setContent/saveActive...
    └─ _Bridge(QObject) ◀───────────────  window.bridge.fileSaved/dirtyChanged/...
       Sinyaller: saved, dirtyChanged, breakpoints, ready
```

- **Python → JS:** `page().runJavaScript(...)` ile `window.API.*` çağrılır. Editör hazır
  olana kadar çağrılar kuyruğa alınır (`_pending`), `ready` gelince boşaltılır.
- **JS → Python:** JS `window.bridge.*` (QWebChannel slot'ları) çağırır; `_Bridge` bunları
  Qt sinyallerine çevirir. Örn. Ctrl+S → `bridge.fileSaved(path, content)` → `saved` sinyali.
- Monaco çok modelli çalışır (sekme başına bir model); dil, dosya uzantısından belirlenir.
- Gutter tıklama → breakpoint sinyali (Faz 3 debugger'a hazır).

## Katman — Araçlar (`project.py`)

`Project(root)` lokal proje üzerinde güvenli işlemler:

| Metot | İş |
|-------|----|
| `list_files()` | ilgili dosyaların göreli yolları (gürültü/ikili elenir) |
| `read_file(rel)` | içerik (azami `MAX_READ_CHARS`) |
| `make_diff(rel, new)` | unified diff üretir |
| `apply(rel, new)` | diske yazar; var olanı `.bak` yedekler |
| `_safe(rel)` | **yol güvenliği** — proje kökü dışına çıkışı engeller |

---

## Katman — Arayüz (masaüstü, `desktop.py` + widget modülleri)

Masaüstü mini-IDE, tek bir `desktop.py::MainWindow` altında bir dizi widget modülünü birleştirir:

| Modül | İş |
|-------|----|
| `theme.py` | tasarım tokenları (`SP/RADIUS/TYPE/MOTION`), palet, `build_style` yardımcıları (`grad/elev/icon/icon_png`), gömülü font yükleme, **accent/density çalışma-zamanı** (`set_accent/set_density`) |
| `anim.py` | mikro-animasyon yardımcıları (fade/geometry/height/pulse/count-up); **küresel `set_enabled`** (ayarlardan kapatılınca anında son duruma geçer) |
| `chrome.py` | frameless özel başlık çubuğu (`TitleBar` + `make_frameless`; Qt6 `startSystemMove/Resize`) |
| `chat_view.py` | kart tabanlı konuşma akışı (`ChatView` + aşama `StageCard`'ları, streaming, verdict rozeti) |
| `agent_pipeline.py` | canlı ajan pipeline/ekip paneli (Planner→Coder→Reviewer, nabız, metrik) |
| `changes_panel.py` | dosya bazında kabul/ret + diff görünümü |
| `command_palette.py` | Ctrl+K komut / Ctrl+P dosya paleti (fuzzy, overlay) |
| `settings_panel.py` | Ayarlar overlay'i (accent/yoğunluk/Enter/animasyon) |
| `ui_prefs.py` | tercih kalıcılığı (JSON, `~/.multi_agent_ide/prefs.json`) |
| `history.py` + `history_panel.py` | oturum geçmişi (proje-içi `.magent/history.json`) + overlay |
| `terminal.py` | entegre terminal çekirdeği (`Terminal`, komut-başına `QProcess`, canlı çıktı/geçmiş/cd/çıkış kodu, `commandFinished` sinyali) |
| `bottom_panel.py` | VS Code tarzı alt panel (`BottomPanel`, çok sekmeli terminal; Ctrl+`) |
| `tools/uishot.py` | görsel öz-doğrulama (offscreen render → `.uishots/*.png`) |

Tercih akışı: `ui_prefs.load()` → `theme.set_accent/set_density` + `anim.set_enabled` →
`build_style()`. Ayar değişince `MainWindow._apply_prefs` aynı zinciri canlı çalıştırır ve kaydeder.

## Veri akışı örneği (masaüstü, proje modu)

```
Kullanıcı: "utils.py'deki tarih formatını ISO 8601 yap"
  desktop.py → Worker(QThread) → run_project_task(...)
    yield stage/metric/output   → arayüz "Akış" paneline yazar
    yield diff                  → arayüz "Diff" paneline renklendirir
    yield proposal              → "Uygula/Vazgeç" aktifleşir
  Kullanıcı "Uygula" → Project.apply(...) → .bak yedek + yeni içerik
```

## Neden bu tasarım?

- **Tek ortak `LLMResponse`** → yeni model eklemek = bir `call_xxx` + `PROVIDERS`'a giriş.
- **Generator + olaylar** → aynı motor tüm arayüzlerde; akış canlı ve arayüzden bağımsız.
- **routing sözlüğü** → rolleri modeller arası tek satırla değiştirme.
- **Project katmanı** → dosya işlemleri tek yerde, yol güvenliği garantili.
