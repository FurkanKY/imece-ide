# M5 mühendislik denetimi ve tekrar üretilebilir teslim

Son tam offline regresyon: sekiz ayrık CI shard'ında **4155 passed, 13 skipped**;
4168 test toplamı tamamlandı. Tek-process 360 saniye sınırına takılan ilk deneme
başarı sayılmadı. İlk paralel koşuda bulunan iki hata düzeltildi: ACP kontrolü
artık başka shard'ın fixture sürecini "leak" saymıyor; shared-candidate Source
Git okuması artık index stat-cache'ini bile yenilemiyor (`--no-optional-locks`).
Son yeniden koşunun sekiz shard'ı da geçti. POSIX PTY forkpty deprecation uyarısı
ayrıca korundu; native Windows/platform skip'leri kabul kanıtı değildir.

Bu belge mühendislik kanıtıdır; gerçek sağlayıcı, Windows, temiz makine veya
iki fiziksel makine LAN kabulü ve yayın izni yerine geçmez.

## Tek komutla teslim

Mevcut `.venv`, UI bağımlılıkları ve belgelenen sistem gereksinimleriyle:

```sh
bash packaging/verify.sh
```

Windows ortamında:

```powershell
packaging/verify.ps1
```

Akış: taze UI/build → lisans/provenance ve kılavuzları yerleştir → manifest →
yalıtılmış frozen supervisor/Node/debugpy/LSP/GUI/terminal/normal kapanış smoke → dosya/bytecode
incelemesi → arşiv ve SHA256. `--skip-web-build`/`-SkipWebBuild` yalnız geliştirici
seçeneğidir; UI tazeliğini kanıtlamaz. Kullanıcı verileri, sağlayıcı/agent çağrısı,
LAN host/join veya yayın otomatik başlatılmaz.

`dist/` yan dosyaları: `package-smoke.json`, `helper-smoke.json`, `artifact-audit.json`,
`delivery-receipt.json`, `SHA256SUMS.txt`. Arşiv manifesti değişmez; raporlar
manifest içine sonradan eklenip dairesel bir kanıt oluşturmaz. Arşivde
`BUILD-INFO.json`, `DEPENDENCIES.json`, `MANUAL-TEST.md` ve `docs/` bulunur.
HEAD, kirli çalışma ağacından üretilen paketin tam kimliği değildir. GUI ve
helper raporları aynı manifest/executable SHA256 kimliğine bağlıdır; eski/eksik
raporla yeni arşiv üretilemez. Helper'lar da gerçek frozen subreaper/Windows Job
altında çalışır; nonce + üretici quiescence kanıtı olmadan PASS verilmez.

## Lisans/metin ve native provenance

Son Linux incelemesinde:

- 45 kurulu Python runtime dağıtımı; isteğe bağlı kurulu `typesafe-sdk` ve
  `tenacity` dahil. Sürüm/specifier ve platform/extras kontrolü.
- 190 frontend paket/metin kaydı; zorunlu peer bağımlılıkları, scoped/nested
  paketler ve dağıtılan Tailwind CSS dahil. Kurulu sürüm lockfile ile eşleşmeli.
- Kurulu wheel lisans/notice metinleri, font metinleri, sürüme bağlı tam Node
  metni, LGPL-3/GPL-3 ve PyInstaller bootloader lisans/istisnası yerleştirilir.
- İlk Linux TOC 477 native binary/extension kaydı içeriyordu. Kullanılmayan QML
  uygulama pluginleri, PDF/VirtualKeyboard image/input pluginleri ve Quick3D
  profiler çıkarıldı; QtQuick/Qml native ABI, platform/normal image/OS input
  pluginleri korundu. Güncel TOC: 258 native kayıt, 139 sistem kaydı, 112 sistem
  copyright/atıf metni; sistem metinleri eksik değil. GNU readline da çıkarıldı.
- Güncel paket manifesti 6895 dosya ve 30 güvenli iç link; önceki paketten 2561
  daha az dosya. Test arşivi yaklaşık 257.5 MiB. Qt scope preflight, kullanılmayan
  GPL-only add-onların sonraki build'lere sessizce geri gelmesini reddeder.

Konumlar: `_internal/licenses/inventory.json`, `python/`, `frontend/`, `fonts/`,
`qt/`, `bootloader/`, `native/inventory.json`. Genel Qt lisans metni tüm add-on
kütüphanelerini LGPL yapmaz; bu yüzden shell'in kullanmadığı GPL-only QML/add-on
payloadları yalnız belgelenmek yerine build kapsamından çıkarıldı. Qt/Chromium üçüncü taraf ve tam kaynak sağlama
şartları, Windows DLL/CRT ve yayın/signing kararları **yayın öncesi açık kapıdır**.
Bu envanter hukuki uygunluk sertifikası değildir. M4'te eksik kalan durable
shared-candidate conflict ve görev reassignment (prepare öncesi/apply öncesi)
negatifleri de `tests/test_shared_candidate_edges.py` ile doğrulandı: Source
byte/index/HEAD korunur, yanlış apply/checkpoint authority oluşmaz.

## İçerik ve source/geçmiş taraması

`packaging/audit.py` manifestin tüm dosya/hash/link kümesini, gerçek dosya
baytlarını, PyInstaller Python bytecode sabitlerini ve seçili credential
imzalarını kontrol eder. Uygulama kodu çalıştırılmaz. Bulgu çıktısı secret değeri
veya eşleşen içerik vermez. Kör dosya/path istisnası kullanılmaz:

- Bir RECORD alanındaki credential-benzeri parça ancak gerçek dosyanın SHA256
  alanı olduğu ve dosya hash'i eşleştiği doğrulanınca sınıflandırılır.
- GnuTLS binary'sindeki herkese açık self-test PEM verileri, tam dosya SHA256
  pin'iyle incelenmiştir. On PEM blokunun tamamı public GnuTLS 3.8.3 self-test
  kaynağıyla byte-for-byte eşleşti; farklı library baytları yeniden inceleme ister.
  Kaynak/pin: `packaging/audit-exceptions.json`. Son artifact taraması: 0 bloklayan,
  7 incelenmiş gösterge; frozen bytecode incelemesi kullanılabilir.

Gitleaks 8.30.1 official release checksum'u doğrulanarak yalnız `/tmp`'de
çalıştırıldı: mevcut 139 Git commit'inde bulgu yok. Güncel Git-visible source
snapshot'unda 10 gösterge elle incelendi; hepsi mevcut redaction/security
regression testlerinin sentetik token/oluşturulmuş geçersiz PEM fixture'ları.
Tam dosya SHA pin'leri `packaging/source-audit-fixtures.json` içinde; yeni veya
değişmiş gerçek secret'ı otomatik kabul eden path/regex muafiyeti değildir.
Checkpoint commit'inden sonra history'de de görünen tek geçersiz PEM test
fixture'ı ayrıca tam introduction commit ID + committed blob SHA ile pin'lendi.
Current dosya pin'i tek başına history muafiyeti vermez; yeni commit, değişen
blob, farklı kural veya Git replace-ref ile üretilen içerik kabul edilmez.
Bu sınırlar `tests/test_source_audit_history.py` içinde test edilir.

Kurulu Gitleaks ile tekrar:

```sh
.venv/bin/python packaging/source_audit.py --root . \
  --gitleaks /absolute/path/to/gitleaks --output dist/source-history-audit.json
```

`.pi/`, `PI_HANDOFF.md`, ignore edilmiş yerel veri/bağımlılıklar okunmaz veya
kopyalanmaz. Checkpoint öncesi: 0 bloklayan, 10 current fixture. Checkpoint
sonrası: 0 bloklayan, aynı 10 current + 1 exact-commit history fixture.
Seçili imza taraması, keyfi encoding/şifreli içerik veya her olası secret için
"secret-free" sertifika değildir. Gerçek kabul, son legal/redistribution review,
imzalama ve ayrı yayın izni hâlâ gereklidir; hiçbir commit/push/tag/dispatch veya
yayın bu çalışma sırasında yapılmadı.
