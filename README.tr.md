# Multi-Agent IDE

> Windows açık beta · `v0.3.0-beta.1` · [English](README.md)

Planner, Coder ve Reviewer modellerini, kullanıcı onaylı diff ve checkpoint
akışında birlikte çalıştıran yerel-önce masaüstü kodlama ortamı. Dosya gezgini,
Monaco editör, terminal, Git görünümü, Python LSP/debug, değişiklik makbuzu ve
AI ekip panelini tek pencerede birleştirir.

## Hızlı başlangıç

1. GitHub Releases sayfasından `MultiAgentIDE-windows.zip` indirin.
2. `SHA256SUMS.txt` ile hash doğrulayın, zip'i çıkarın ve `MultiAgentIDE.exe`
   çalıştırın.
3. Yerel klasör açın; Ayarlar'dan DeepSeek/Gemini anahtarınızı ekleyin. Claude
   için Claude Code CLI ayrıca kurulu olmalıdır.
4. Görevi yazın, diff'i inceleyin, ardından Uygula veya Vazgeç seçin.

Bu sürüm yalnız Windows içindir ve paket imzasızdır; SmartScreen uyarısı
görülebilir. Telemetri yoktur. Paketli sürümde anahtarlar Windows DPAPI ile
korunur; her AI koşusu yerel değişiklik makbuzu üretir.

Kurulum ve kullanım ayrıntıları: [SETUP](docs/SETUP.md), [USAGE](docs/USAGE.md),
[RELEASE](docs/RELEASE.md), [gizlilik](PRIVACY.md) ve [güvenlik](SECURITY.md).

Lisans: [Apache-2.0](LICENSE).
