# Imece

[![Doğrulama — main](https://github.com/FurkanKY/imece-ide/actions/workflows/verify.yml/badge.svg?branch=main)](https://github.com/FurkanKY/imece-ide/actions/workflows/verify.yml?query=branch%3Amain)
[![Gizli bilgi taraması — main](https://github.com/FurkanKY/imece-ide/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/FurkanKY/imece-ide/actions/workflows/security.yml?query=branch%3Amain)
[English](README.md) · [Belgeler](docs/README.md) · [Destek](SUPPORT.md) · [Gizlilik](PRIVACY.md)

Imece, yerel-öncelikli bir masaüstü kodlama ortamıdır: bir görev tanımlayın,
tek bir kodlama ajanı seçin, ajanın ayrı kopyada oluşturduğu değişikliği ve
doğrulama kanıtını inceleyin; uygulama ya da vazgeçme kararını siz verin.
Zorunlu planlayıcı/inceleyici rol zinciri yoktur. Uygulama öncesi checkpoint
alınır. Imece telemetri veya barındırılan bir hizmet sunmaz.

## Bugünkü durum

- **En son etiket: `v0.4.0-beta.1`** — en son kaynak kod sürümü; daha yeni
  geliştirme özellikleri bu etikete dahil değildir.
- **`main`** — yayımlanmamış geliştirme; depodaki uygulama IDE biçimlidir ve aynı
  anda tek çalışma yürütür. Rol bağımsız tek ajan akışını içerir. Yerel fixture doğrulaması,
  canlı sağlayıcı veya platform kabulü değildir.
- **M2 çalışması** — görev öncelikli arayüz ve eşzamanlı koşular ayrı
  [`m2-run-manager` dalında](https://github.com/FurkanKY/imece-ide/tree/m2-run-manager)
  uygulanmıştır; `main` ile birleştirilmemiştir. Bu dal fixture/mock ile
  doğrulanmıştır; birleştirmeden önce [güncel CI sonuçlarını](https://github.com/FurkanKY/imece-ide/actions/workflows/verify.yml?query=branch%3Am2-run-manager)
  kontrol edin. Bu doğrulama, gerçek sağlayıcı veya native masaüstü kabulü değildir.

M1'in gerçek sağlayıcı ve desteklenen platform kabulü ertelenmiş doğrulama
borcudur ve M5 öncesi yayın eşiğidir. M3–M5 başlamamıştır. Ayrıntılar için
[esas ürün planına](docs/PRODUCT-PLAN.md) bakın.

## Kaynaktan başlama

Şimdilik yalnızca kaynak kod dağıtılır. Masaüstü kabuğu Windows 10/11'i hedefler
ve Linux'ta da kaynaktan çalışır. Python 3.14 ve Node.js 22 LTS (22.12 veya
yenisi önerilir; Vite 7, Node 20.19+ ya da 22.12+ gerektirir) kullanın.
Ayrıntılar: [Kurulum](docs/SETUP.md).

**Windows PowerShell** (depo kök dizininde):

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Push-Location web/ui; npm ci; npm run build; Pop-Location
.\.venv\Scripts\python.exe shell.py
```

**Linux Bash** (depo kök dizininde):

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
(cd web/ui && npm ci && npm run build)
.venv/bin/python shell.py
```

Arayüz şu an Türkçedir. İzole ajan çalışması için Git'in `PATH` üzerinde olması,
projenin Git deposu olması ve uyumlu bir sağlayıcının Ayarlar'da yapılandırılması
gerekir; kurulum ve isteğe bağlı
sağlayıcı ayrıntıları [Kurulum](docs/SETUP.md) belgesindedir.

## Güven ve gizlilik

Ajan değişiklikleri ayrı bir Git worktree'sinde oluşturulur ve uygulamadan önce
incelenebilir. Bu bir **güvenlik sandbox'ı değildir**: proje doğrulama komutları
ve kabuk/proje kodu kullanıcının işletim sistemi yetkileriyle çalışır. Kaynak
çalıştırmada sağlayıcı anahtarları git-ignored `.env` dosyasında, paketlenmiş
Windows sürümünde DPAPI ile yerel saklanır. Seçilen sağlayıcı, koşu için
gönderilen bağlamı alır. İşbirliği kimlik bilgileri yalnızca bellekte tutulur;
açıkça paylaşılan öneri kodu otomatik sansür olmadan gönderilir. Paylaşmadan
önce inceleyin. [Gizlilik](PRIVACY.md) ve [güvenlik](SECURITY.md).

## Belgeler ve topluluk

- [Belgeler dizini](docs/README.md) · [Kullanım](docs/USAGE.md) · [Ürün planı](docs/PRODUCT-PLAN.md)
- [Katkı](docs/CONTRIBUTING.md) · [Destek](SUPPORT.md) · [Davranış kuralları](CODE_OF_CONDUCT.md)
- [Tartışmalar](https://github.com/FurkanKY/imece-ide/discussions) · [Güvenlik bildirimi](https://github.com/FurkanKY/imece-ide/security/advisories/new)

Lisans: [Apache-2.0](LICENSE).
