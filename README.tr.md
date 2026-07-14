# Imece IDE

> Windows açık beta · `v0.3.0-beta.1` · [English](README.md)

**Imece**, seçtiğin modellerle çalışan bir AI ekibinin — Planner, Coder ve
Reviewer — bir değişiklik üzerinde birlikte çalıştığı ve sonucu tek bir dosyaya
dokunmadan önce incelenebilir bir diff olarak sana sunduğu, yerel-önce bir
masaüstü kodlama ortamıdır. Adını, bir topluluğun ortak bir iş için emeğini
birleştirdiği imece geleneğinden alır.

Bu akışın etrafında Imece gerçek bir IDE'dir: dosya gezgini, Monaco editör,
entegre terminal, Git görünümü, Python dil zekâsı ve debug, geri alınabilir
checkpoint'ler ve her AI koşusu için kalıcı değişiklik makbuzu.

![Imece IDE'de AI önerisini inceleme](docs/assets/review.png)

## Hızlı başlangıç

1. Son [GitHub Release](../../releases) sayfasından `ImeceIDE-windows.zip`
   indirin.
2. `SHA256SUMS.txt` ile hash doğrulayın, zip'i çıkarın ve `ImeceIDE.exe`
   çalıştırın.
3. Yerel bir klasör açın; Ayarlar'dan DeepSeek/Gemini anahtarınızı ekleyin.
   Claude için [Claude Code CLI](https://claude.com/claude-code) ayrıca kurulu
   olmalıdır.
4. Görevi yazın, önerilen diff'i inceleyin, ardından Uygula veya Vazgeç seçin.

Bu sürüm yalnız Windows içindir ve paket imzasızdır; SmartScreen uyarısı
görülebilir. Telemetri yoktur. Paketli sürümde anahtarlar Windows DPAPI ile
korunur; her AI koşusu yerel değişiklik makbuzu üretir.

Kurulum ve kullanım ayrıntıları (İngilizce): [SETUP](docs/SETUP.md),
[USAGE](docs/USAGE.md), [RELEASE](docs/RELEASE.md), ayrıca
[gizlilik](PRIVACY.md) ve [güvenlik](SECURITY.md).

Lisans: [Apache-2.0](LICENSE).
