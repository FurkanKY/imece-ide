# Mimari

## Genel bakış

Sistem üç katmandan oluşur:

```
┌─────────────────────────────────────────────────────────────┐
│  ARAYÜZLER   orchestrator.py (CLI) · app.py (web) · shell.py   │
│              shell.py = masaüstü mini-IDE (webhost/ + web/ui/) │
├─────────────────────────────────────────────────────────────┤
│  ORKESTRASYON   runner.py (üretim)  ·  project_runner.py (proje)│
│                 → olay (event) üreten generator'lar            │
├─────────────────────────────────────────────────────────────┤
│  AJANLAR     agents.py (rol + routing)                        │
│  ADAPTÖRLER  adapters.py (claude / deepseek / gemini)         │
│  ARAÇLAR     project.py (dosya listele/oku/diff/uygula)       │
└─────────────────────────────────────────────────────────────┘
```

Temel tasarım ilkesi: **her katman bir alttakine bağımlı, üsttekinden habersiz.**
Arayüzler orkestratörü çağırır; orkestratör ajanları; ajanlar adaptörleri. Bu sayede
aynı motor tüm arayüzlerde değişmeden kullanılır.

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

Public beta ekleri: paketli Windows anahtarları `secret_store.py` üzerinden
DPAPI ile korunur; `exec.preflight`/`exec.approveCommand` proje kaynaklı F5
komutunun ilk çalıştırma onayını taşır. Koşu sonunda `receipts.py`, proje içi
`.magent/receipts/<id>.json` makbuzunu atomik yazar; `history.json` yalnız hızlı
indeks ve makbuz kimliğini tutar.
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

## Katman — Masaüstü arayüzü (`shell.py` + `webhost/` + `web/ui/`) ★

Masaüstü arayüzü, **tüm UI'ı tek bir `QWebEngineView` içinde web teknolojisiyle**
çizer (Cursor/VS Code'un Electron modeli; ama host native PySide6). Motor Python'da kalır,
tek bir **RPC köprüsü** üzerinden konuşulur.

```
web/ui/  (React 19 + TS + Vite + Tailwind v4)      webhost/  (PySide6 host)
  titlebar · activitybar · explorer · editor          shell.py         giriş (--dev)
  (Monaco) · search · scm · aipanel · terminal        scheme.py        app:// özel şeması (dist servis)
  (xterm) · statusbar · welcome · toasts              window.py        frameless pencere + webview
    ▲                                                 bridge.py        RPC dispatcher (@handler)
    │ bridge/{protocol,qt,mock}.ts                    state.py         aktif Project tekili
    │  call(method,params) → Promise                  watcher.py       QFileSystemWatcher → fs.changed
    │  on(channel, cb)     → olay akışı               api/…            domain handler'ları (aşağıda)
    ▼
  QWebChannel  ◀──── host.call / reply / event ────▶
```

**Köprü sözleşmesi (tek doğruluk kaynağı `web/ui/src/bridge/protocol.ts`):** zarf
`{id, method, params}` → `{id, ok, result|error}`; olaylar `{channel, payload}`. Python
tarafında `@handler("domain.metot")` ile kaydedilir; uzun işler QThread'e alınıp sinyalle
çözülür. Domain'ler: `window · app · settings · project · fs · session` (P1); `run ·
terminal · history · search · scm` (P2–P4); `lsp · exec · debug` (P7–P8, IDE+). `scm`
(api/scm.py) git CLI alt-süreçleriyle status/diff/stage/unstage/discard/commit sağlar
(UTF-8 + `stdin=DEVNULL`; kenar çubuğu KAYNAK DENETİMİ görünümü buradan beslenir,
satır diff'i merkez Monaco'da açılır).

**Dil zekâsı (P7 — `webhost/jsonrpc.py` + `api/lsp.py` + `web/ui/src/lib/lsp.ts`):**
Python için **basedpyright** dil sunucusu köprüden bağlanır. `jsonrpc.py` Content-Length
çerçevelemesini çözer (LSP ve P8.2 DAP'ın ortak tel biçimi); `api/lsp.py` sunucu yaşam
döngüsünü yönetir (proje açılınca `initialize`, proje değişince yeniden başlatma,
kapanışta shutdown; sunucudan gelen `workspace/configuration` isteklerine asgari yanıt).
Web'de `lib/lsp.ts` **el yazımı ince istemci**dir (monaco-languageclient kullanılmaz):
Monaco model yaşam döngüsünden belge senkronu (didOpen/didChange 200ms debounce),
tamamlama·hover·tanıma-git·imza-yardımı provider'ları, `publishDiagnostics` →
`setModelMarkers` (hata alt çizgileri). URI çevirisi (`file:///<rel>` ↔ mutlak LSP URI)
iki yönde de bu modülde. TS/JS aynı özellikleri Monaco'nun kendi worker servisinden alır
(`lib/monaco.ts`: eagerModelSync + compilerOptions; F12 başka dosyaya `registerEditorOpener`
ile sekme açar).

**F5 çalıştır (P8.1 — `runconfig.py` + `api/exec.py`):** `runconfig.py` motor-yanı
komut sezgisidir (dosya: uzantıdan; proje: `.magent/run.json` kaydı > npm/cargo/go/
main.py sezgisi). `api/exec.py` komutu kabuk üzerinden koşar, çıktıyı 16ms birleştirmeli
`exec.output` olayıyla akıtır (terminal deseni) ve `exec.exited {code, durationS}` ile
kapatır — çıktı YAKALANIR (alt panel ÇIKTI sekmesi, salt-okunur xterm; ham metin P9.2
"hatayı ekibe gönder" için `state/exec.ts`'te saklanır). Tek eşzamanlı koşu; Windows'ta
süreç ağacı `taskkill /T /F` ile temizlenir.

**Debugger (P8.2 — `api/debug.py` + `state/debug.ts` + `components/debug/`):**
debugpy DAP üzerinden: serbest porta `python -m debugpy --listen --wait-for-client
<dosya>` (debuggee bizim alt-sürecimiz; stdout'u ÇIKTI sekmesine akar — `state/exec.ts`
dış-koşu kanalı), retry'lı soket bağlantısı + `jsonrpc.py` çerçevesi (LSP ile ortak).
El sıkışma: initialize→attach→[initialized]→setBreakpoints→configurationDone.
`stopped` olayında yığını Python çeker ve `debug.stopped {path, line, frames}` tek
pakette yollar; scopes/variables tembel (`variablesReference`). Web: Monaco glyph
margin breakpoint'leri (proje başına localStorage) + durulan satırda amber vurgu;
kenar çubuğu ÇALIŞTIR VE DEBUG görünümü (kontrol şeridi, çağrı yığını, değişken
ağacı). F5 VS Code düzeni: durmuşsa devam · .py ise debug · değilse koş.

**app:// özel şeması** (`scheme.py`): Vite ES modülleri/worker'ları `file://` altında
CORS'a takıldığı için `SecureScheme|CorsEnabled|FetchApiAllowed` bayraklı özel şema
kullanılır; `web/ui/dist/`'i diskten servis eder. `--dev` modunda bunun yerine Vite dev
sunucusu (`localhost:5173`, HMR) yüklenir — **gerçek köprüyle**.

**Frameless mekanik:** HTML titlebar `pointerdown` → köprü `window.startSystemMove()`
(chrome.py deseninin portu; native snap/move korunur). Kenar tutamaçları →
`startSystemResize(edge)`. Kısayol sahipliği %100 web (`lib/keymap.ts`).

**Mock bridge** (`bridge/mock/`): aynı UI sahte veri + sanal FS ile düz tarayıcıda çalışır
(`?scenario=…`) → geliştirme + `tools/webshot.mjs` (Playwright) ile görsel doğrulama
(Monaco/xterm dahil her şey görünür).

**Durum yönetimi (web, zustand):** `state/workspace.ts` (proje + lazy ağaç),
`state/editor.ts` (sekmeler/dirty/kaydet/merkez diff), `state/run.ts` (koşu olayları),
`state/scm.ts` (git durumu), `state/terminal.ts`, `state/search.ts`, `state/ui.ts`
(panel görünürlük/boyutları), `state/settings.ts` (prefs → `<html data-*>`).
Tasarım tokenları `web/ui/src/styles/tokens.css`'te (bkz. DESIGN.md).

**Editör:** Monaco npm paketiyle gelir (`lib/monaco.ts` — worker kablolaması + tokenlardan
üretilmiş `magent-dark` teması + diff renkleri). Merkez **inline diff** hem AI önerileri
hem SCM satır diff'leri için aynı `DiffView` bileşenini kullanır.

**Terminal:** `api/terminal.py` pywinpty (ConPTY) PTY'si açar; okuyucu QThread →
16ms/256KB birleştirmeli flush → `terminal.data` olayı → xterm.js.

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

## Veri akışı örneği (masaüstü, proje modu)

```
Kullanıcı: "utils.py'deki tarih formatını ISO 8601 yap"  (AI paneli composer'ı)
  web → bridge run.start → api/run.py Worker(QThread) → run_project_task(...)
    yield stage/metric/output   → run.event olayı → Akış sekmesi + pipeline canlanır
    yield diff                  → Değişiklikler listesi dolar
    yield proposal              → merkez inline diff açılır, Uygula/Vazgeç aktifleşir
  Kullanıcı "Uygula" → run.applyProposals → Project.apply(...) → .bak yedek + yeni içerik
    → fs.changed → gezgin/sekmeler/SCM görünümü tazelenir
```

## Neden bu tasarım?

- **Tek ortak `LLMResponse`** → yeni model eklemek = bir `call_xxx` + `PROVIDERS`'a giriş.
- **Generator + olaylar** → aynı motor tüm arayüzlerde; akış canlı ve arayüzden bağımsız.
- **routing sözlüğü** → rolleri modeller arası tek satırla değiştirme.
- **Project katmanı** → dosya işlemleri tek yerde, yol güvenliği garantili.
