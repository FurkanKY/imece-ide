# Manuel kabul — yerel M3/M4/M5 test paketi

Bu rehber geliştirmenin kalan **gerçek ortam** kontrolleri içindir. Geçmiş fixture,
mock, loopback TLS ve otomatik test sonuçları bu kutuları kendiliğinden kapatmaz.
Bu bir public release değildir; `0.4.0-beta.1` çalışma ağacından üretilmiş yerel
Linux test paketidir. M1, M3 Windows/native-desktop ve M4 iki makine kabulü açık.
Commit/push/tag/yayın yapılmadı. Sonuçları en alttaki şablona kaydedin.

## 1. Ne çalıştıracağım?

Depo kökünden, bu makinedeki gerçek paket:

```sh
./dist/ImeceIDE/ImeceIDE
```

Taşınabilir test arşivi: `dist/ImeceIDE-linux-manual.tar.gz`.
Checksum: `dist/SHA256SUMS.txt`. Arşivi ayrı bir dizine taşıyıp açabilirsiniz:

```sh
sha256sum -c SHA256SUMS.txt
tar -xzf ImeceIDE-linux-manual.tar.gz
./ImeceIDE/ImeceIDE
```

**`ImeceIDE` dizininin tamamını taşıyın; yalnız yürütülebilir dosyayı değil.**
Paket Python ve Node içerir. Git, seçtiğiniz sağlayıcı CLI'si ve projenin test/
derleme araçları harici gereksinimdir. Test için proje test komutlarının kullandığı
Python da gerekiyorsa ayrıca kurulu olmalıdır; paket Python'u bir genel amaçlı
`python` komutu olarak sağlamaz.

Linux: Ubuntu 24.04 x86_64 / glibc 2.39 üzerinde üretildi. Bu makinedeki Wayland
smoke geçti; diğer dağıtım, GPU ve temiz-makine uyumluluğu kabul edilmiş değildir.
Linux frozen paket Chromium GPU render’ını varsayılan olarak kapatır. Bu
makinedeki GPU’lu Wayland başlangıcı aralıklı SIGSEGV/hazır olamama gösterdi;
yazılımsal varsayılanla altı ardışık native smoke ve normal pencere kapanması
geçti. Bu bir uyumluluk önlemidir, donanımsal yolun düzeldiği iddiası değildir.
Kaynak modunun ve Windows’un varsayılanı değişmez. `--software-rendering` açık
seçeneği vardır; `--hardware-rendering` yalnız deneysel donanım testi içindir ve
bu makinedeki bilinen açılış sorunu tekrar edebilir. Sandbox kapatılmaz.

X11/xcb kullanıyorsanız sistemde `libxcb-cursor0` ve Qt/X11 sistem bağımlılıkları
bulunmalıdır; bu makinede `libxcb-cursor0` yok. Wayland çalışıyor. X11 için örnek:

```sh
sudo apt-get install libxcb-cursor0
QT_QPA_PLATFORM=xcb ./ImeceIDE/ImeceIDE
```

Bu komutu yalnız kendi sisteminizde gerekli olduğunda çalıştırın. Smoke için
`offscreen` kullanmak görüntü/klavye/IME kabulü sayılmaz.

Veriler: mutlak `XDG_DATA_HOME/ImeceIDE`, yoksa `~/.local/share/ImeceIDE`.
Linux API anahtarları bu dizindeki `.env` içinde **şifrelenmemiş**, 0600 izinli
saklanır. Gerçek anahtarları Git'e, davete, ekran görüntüsüne veya hata raporuna
koymayın. Windows paketinde DPAPI kullanılır. Source-mode eski verileri otomatik
taşınmaz; kaynak geliştirme kurulumu ile paket veri dizini farklıdır.

### Windows

Bu Linux ortamında Windows EXE üretilmedi ve native Windows testleri yürütülmedi.
Windows üzerinde güncel çalışma ağacını kullanarak [SETUP.md](SETUP.md)'deki build
ortamını hazırlayın; PowerShell'de:

```powershell
packaging/build.ps1
$env:IMECE_PACKAGE_SMOKE_REPORT = "$PWD\dist\package-smoke.json"
node packaging/smoke.mjs
.\dist\ImeceIDE\ImeceIDE.exe
```

Workflow kaynakta hazırdır; burada çalıştırılmadı. Windows'u yalnız Linux paketi
ile test edemezsiniz. Windows sonuçlarını ayrı kaydedin.

## 2. Güvenli test projesi

Üzerinde çalıştığınız gerçek depoyu ilk denemede kullanmayın. Yeni, temiz,
commit edilmiş bir Git deposu ve küçük bir test takımı kullanın. Örnek dosyalar:

`calc.py`:

```python
def add(a, b):
    return a + b
```

`tests/test_calc.py`:

```python
import unittest
from calc import add

class CalcTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
```

```sh
git init
printf '__pycache__/\n*.pyc\n' > .gitignore
git add calc.py tests/test_calc.py .gitignore
git -c user.name=ImeceTest -c user.email=imece-test@example.invalid commit -m baseline
python3 -m unittest discover -s tests
```

Windows PowerShell'de `.gitignore` oluşturma satırını
`Set-Content .gitignore @('__pycache__/', '*.pyc')` olarak kullanın.
Windows'ta test komutunu kurulu Python'a göre `python -m unittest discover -s tests`
olarak değiştirin. Uygulamada **Klasör Aç** ile bu depoyu seçin. Proje doğrulama
komutunu bu test komutuna ayarlayın. Git görünür değişiklikleri ve test çıktısını
her adımda kontrol edin. Durdurma/geri alma denemeleri için yalnız test verisi
kullanın; dış süreçler ve harici editörlere karşı küresel transaction garantisi yok.

## 3. Temel masaüstü / gerçek sağlayıcı (M1 + paket)

- [ ] Paket repo, `.venv`, Python/npm PATH'i olmadan açılıyor; UI mock bridge değil.
- [ ] Türkçe yazma/IME, pencere boyutu, ekran ölçeklemesi, dosya aç/kaydet, dirty
  sekmeler ve terminal komutları çalışıyor. Python dosyasında LSP tamamlaması ve
  tanılama görünür; gerekiyorsa debugger ayrıca sınanır.
- [ ] Ayarlar'da seçtiğiniz gerçek API/CLI sağlayıcısını **kendiniz** yapılandırın.
  Anahtar durumunun maskeli kaldığını kontrol edin. Bir küçük gerçek görev verin;
  model/araç/doğrulama hatası veya timeout olursa rapora yazın.
- [ ] Ajan çıktısı izole çalışma alanında oluşuyor; açık onaydan önce Source'da
  dosya değişikliği yok. Başarısız/çalıştırılmamış doğrulama apply yetkisi vermiyor.
- [ ] Açık onayla değişiklik uygulayın, testleri çalıştırın, checkpoint/geri alma
  davranışını sınayın. Sonradan elle düzenlenen dosya korunmalı veya işlem güvenli
  şekilde reddedilmeli; sessizce üstüne yazılmamalı.

## 4. Görev geçmişi / süreç güvenliği / birleşik aday (M3)

- [ ] İki bağımsız küçük görevden review-ready sonuç alın. Temiz, aynı baseline
  üzerinde **Sonuçları birleştir → Birleştir ve doğrula** ile yalnız bu iki sonucu
  seçin. Source, onay verilene kadar değişmemeli.
- [ ] PASS adayı açık onayla uygulayın. Temiz açık sekme yeni içeriği göstermeli;
  dirty sekmedeki kaydedilmemiş metin korunmalı. Uygulamayı kapatıp açın ve kalıcı
  kayıttan **Birleşik adayı geri al** deneyin.
- [ ] Apply sonrası dosyayı elle değiştirip rollback deneyin: eski checkpoint'in
  güncel düzenlemeyi ezmesi değil, güvenli ret beklenir. Source HEAD/branch/proje
  değiştirme ve çakışan iki sonuçta eski apply yetkisi kullanılmamalı.
- [ ] Çalışan görev/doğrulama sırasında normal pencere kapatmayı sınayın. Süreçler
  sonlandırılmalı/drain edilmeli. Açınca hiçbir görev kendiliğinden başlamamalı.
- [ ] Uygun mühürlenmiş kayıtta **Çalışma alanından devam et**: aynı TaskID/RunID,
  yeni execution ve yeni doğrulama beklenir. Eski öneri otomatik uygulanmamalı.
  Source/çalışma alanı değişmişse veya başka canlı sahip varsa devam reddedilmeli.
- [ ] **Yeniden dene** ayrı davranış: aynı TaskID, yeni RunID/attempt ve güncel
  Source'dan yeni çalışma alanı. Çift tıklama/kapasite/açık proje değişimleri güvenli
  olmalı. Eski tarih kaydı tek başına yetki sağlamaz.
- [ ] Ani kill/güç kaybından sonra mühürlenmemiş işin devamının reddedilmesi
  beklenen güvenlik sınırıdır. Bunu normal pencere kapatma ile aynı kabul etmeyin.
- [ ] Windows'ta ayrıca Job/ACP detached-child, junction/reparse, PATH ve boşluk/
  Unicode yol testlerini [M3-ACCEPTANCE.md](M3-ACCEPTANCE.md) doğrultusunda yürütün.

## 5. İki gerçek makine / özel LAN (M4)

Aynı temiz commit'in iki bağımsız checkout'u ve aynı uygulama sürümü gereklidir.
İki process veya loopback bu maddeyi kapatmaz. Sertifika/private key dosyalarını
Source dışında tutun. Sertifika üretmek için örnek (OpenSSL kurulu olmalı):

```sh
openssl req -x509 -newkey rsa:2048 -nodes -days 7 \
  -keyout tls-key.pem -out tls-cert.pem -subj '/CN=Imece-Manual-Test'
```

- [ ] Sahip oturumunda sahip/üye kimlikleri, görev atamaları ve bağlamı açıkça
  tanımlayın. **LAN TLS sunucusunu açıkça yapılandır** bölümünde gerçek özel IPv4,
  PEM sertifika/key yollarını verin. İki ayrı TLS endpoint görünmeli; startup
  listener'ı otomatik açmamalı. Gerekirse yalnız özel ağ için firewall izni verin.
- [ ] **Tek kullanımlık davet üret**; daveti güvenli kanaldan katılımcıya verin.
  SHA-256 parmak izini ayrı kanalda doğrulayın. Katılımcı **Katılımcı oturumuna
  katıl → Eşleştir** kullanmalı; yanlış PIN, süresi dolmuş ve yeniden kullanılan
  davetler reddedilmeli. Davet/credential log veya browser storage'a yazılmamalı.
- [ ] Katılımcı **Durumu yenile** ile hedef/karar/interface/görevleri alır. Kendisine
  atanmış görevde yalnız açıkça seçilmiş dosyaları önizler ve onayla yayınlar.
  Seçilmeyen dosya ve anahtarlar sahibin teklifinde bulunmamalı.
- [ ] Sahip **Ürün** içinde yalnız açıkça seçtiği tekliflerden aday oluşturup
  doğrular, ayrıca onayla uygular ve geri alır. Paket aktarımı, pairing veya seçim
  tek başına Source yazmamalı; başka üyeye atanmış/stale teklifler reddedilmeli.
- [ ] Publish yanıtı kaybolduğunda aynı proposal ID/hash ile uzlaştırın; kör yeni
  ID/yayın retry'si değil. Pair/leave belirsizse sahip daveti/üyeyi iptal edip
  yeniden düzenlemeli; başarısızlık “hiç veri gitmedi” diye sunulmamalı.
- [ ] Üye iptali/ayrılma, listener stop, sekme remount, proje değişimi ve iki
  makinede işlem ortasında kapatma sınanmalı. Eski root/epoch/handle işlemi yeni
  oturumu temizlememeli; gizli veriler geçici bellekte kalmalı.

Ayrıntılı sözleşme: [M4-DELIVERY.md](M4-DELIVERY.md),
[M4-OWNER-LAN.md](M4-OWNER-LAN.md), [M5-PACKAGING.md](M5-PACKAGING.md).

## 6. Sonuç şablonu

Her ortam için ayrı doldurun; henüz çalıştırılmayan maddeye PASS yazmayın:

```text
Tarih / test eden:
İşletim sistemi / mimari / ekran (Wayland/X11/Windows):
Paket sürümü / SHA-256 / BUILD-INFO.json kaynak durumu:
Git ve sağlayıcı/CLI sürümü (anahtar yok):
Kontrol maddesi:
Beklenen:
Gerçek sonuç: PASS / FAIL / NOT RUN
Tekrarlama adımları:
İlgili TaskID / RunID / candidateId / proposalId (davet veya bearer yok):
Redakte hata / ekran görüntüsü / log:
```

FAIL sonuçlarını bildirin; mühendislik düzeltmeleri assistant'ın sorumluluğunda
kalır. Paket imzası, kapsamlı lisans/secret incelemesi ve public release onayı
ayrıdır. Manuel test izni yayın izni değildir.
