# Açık Beta Sürüm Rehberi

## Sürüm

**v0.3.0-beta.1** — Windows `onedir` dağıtımı.

## Son kullanıcı için hızlı başlangıç

1. `MultiAgentIDE.exe` dosyasını çalıştırın.
2. Karşılama ekranından **Klasör Aç** ile çalışacağınız projeyi seçin.
3. Ayarlar → **API Anahtarları** bölümüne DeepSeek ve/veya Gemini anahtarınızı girin.
4. Claude kullanmak için ayrıca Claude Code CLI'ı kurun ve PATH üzerinden erişilebilir
   olduğundan emin olun.
5. Sağdaki ekip paneline görevi yazın; öneriyi diff olarak inceleyip **Uygula** ile
   onaylayın.

Python ve Node, paketli uygulamayı kullanmak için gerekli değildir. Anahtarlar,
tercihler ve günlükler `%LOCALAPPDATA%/MultiAgentIDE` altında tutulur.

## Dağıtımcı kontrol listesi

- [ ] Windows'ta `packaging/build.ps1` ile paketi yeniden üret.
- [ ] `node packaging/smoke.mjs` çalıştır; QWebChannel, ayarlar/anahtarlar ve PTY
      write→read sonucunun temiz olduğunu doğrula.
- [ ] Paketli EXE ile kısa görünür tur: klasör aç, dosya düzenle/kaydet, terminale
      komut yaz, Ayarlar'da anahtar durumunu gör, kapatıp yeniden aç.
- [ ] `%125` ve `%150` Windows ölçeklemesinde titlebar, paneller, Monaco, terminal,
      diyalog ve toast taşmalarını kontrol et.
- [ ] Türkçe IME/girdi ile dosya adı, arama, commit mesajı ve ekip görevi yaz;
      `İ/i/ş/ğ/ü/ö/ç` karakterlerini kontrol et.
- [ ] Tüm maddeler geçerse `v0.3.0-beta.1` Git etiketi oluştur ve paketi yayınla.

## Bilinen sınırlar

- Bu beta yalnız Windows içindir; otomatik güncelleme yoktur.
- Paket büyüktür (QtWebEngine, Python dil sunucusu ve terminal yardımcıları içerir).
- Claude modeli Claude Code CLI ve uygun abonelik gerektirir; DeepSeek/Gemini ayrı API
  anahtarı ister.
- Tek koyu tema sunulur; açık tema, split editör, token maliyet panosu, inline AI düzenleme
  ve otonomi seviyeleri v1 kapsamındadır.
- Git yüzeyi yerel durum, stage/unstage, discard, diff ve commit içindir; remote
  push/pull/branch işlemleri sunmaz.
- Fiziksel frameless pencere sürükleme testi otomatik ortamda doğrulanamadı; dağıtım
  öncesi görünür Windows masaüstü turunda kontrol edilmelidir.

## Geliştirici doğrulaması

```bash
cd web/ui
npm ci
npm run typecheck
npm run build
cd ../..
python -m pytest -q
```

Paketleme yalnız Windows PowerShell'de çalışır:

```powershell
packaging/build.ps1
node packaging/smoke.mjs
```
