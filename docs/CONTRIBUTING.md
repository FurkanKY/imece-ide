# Katkı ve Çalışma Kuralları

## 📌 Dokümantasyon Güncelleme Kuralı (ZORUNLU)

> **Her adımda ve her iterasyonda, o değişikliğin etkilediği dokümanlar AYNI turda
> güncellenir. Bir değişiklik, dokümantasyonu güncellenmeden "bitti" sayılmaz.**

Bu kural hem insan katkıcılar hem de yapay zeka ajanları (Claude dahil) için geçerlidir.
Amaç: kod ile doküman arasındaki farkın hiçbir zaman birikmemesi.

### Her iterasyonun sonunda yapılacaklar (checklist)

1. **`docs/CHANGELOG.md`'ye bir satır ekle** — ne değişti, hangi tarihte.
2. Aşağıdaki tabloya göre **etkilenen dokümanı güncelle**.
3. Yeni bir modül/dosya eklediysen **README dosya haritasına** ekle.
4. Bir yol haritası maddesi tamamlandıysa **`docs/ROADMAP.md`'de durumunu güncelle**
   (⏳ → ✅) ve gerekiyorsa `docs/OPTIMIZATIONS.md` tablosundaki durumu değiştir.

### Değişiklik türü → güncellenecek doküman

| Değişiklik | Güncellenecek |
|-----------|---------------|
| Yeni model/sağlayıcı, fiyat, model adı | `docs/MODELS.md` |
| Yeni modül, katman, olay tipi, veri akışı | `docs/ARCHITECTURE.md` |
| Yeni köprü metodu/olayı (webhost ↔ web/ui) | `web/ui/src/bridge/protocol.ts` (tek kaynak) + `docs/ARCHITECTURE.md` |
| Yeni tasarım tokenı / değeri | `web/ui/src/styles/tokens.css` + `docs/DESIGN.md` |
| Yeni arayüz özelliği / kullanım biçimi | `docs/USAGE.md` |
| Yeni optimizasyon tekniği veya durumu | `docs/OPTIMIZATIONS.md` |
| Kurulum, bağımlılık, ortam tuzağı | `docs/SETUP.md` + `requirements.txt` |
| Web-shell faz ilerlemesi | `docs/HANDOFF.md` + `.claude/plans/web-shell-ui.md` |
| Yol haritası ilerlemesi / faz durumu | `docs/ROADMAP.md` |
| **Her değişiklik (istisnasız)** | `docs/CHANGELOG.md` |

### Faz tamamlama tanımı (Definition of Done)

Bir faz/özellik şu üçü tamamlanınca biter:
1. Kod yazıldı ve **doğrulandı** (headless test ve/veya kullanıcı onayı).
2. İlgili dokümanlar güncellendi (yukarıdaki tablo).
3. `docs/CHANGELOG.md`'ye kayıt düşüldü.

---

## 🎨 Tasarım Özgürlüğü Kuralı (KALICI)

> **Arayüz/deneyim kalitesini artırmak için gereken her türlü dış içeriği indirip
> projeye gömmekte serbestsin:** kütüphaneler (pip), fontlar (Inter, JetBrains Mono,
> Geist vb.), ikon setleri, görsel stiller, UI/UX bileşenleri, referans temalar.**

Kural gerekçesi: kullanıcı, mümkün olan en **premium ve profesyonel** masaüstü deneyimini
istiyor; "yeterince iyi" ile yetinme. Uygulama:

- Gerekli paketi/varlığı indir (ör. `pip install`, font TTF'i `assets/fonts/`'a), gömülü
  ve **offline** çalışacak şekilde projeye dahil et; lisansı uygun olsun (tercihen açık
  kaynak — SIL OFL, MIT, Apache).
- Yeni bir bağımlılık/varlık eklediğinde `docs/SETUP.md` + (pip ise) `requirements.txt`
  güncellenir (bkz. Dokümantasyon Güncelleme Kuralı).
- Görsel değişiklikler **canlı ekran görüntüsüyle** doğrulanır; kod yollarında mümkünse
  headless testle regresyon önlenir.
- Python 3.14 gibi çok yeni ortamlarda wheel bulunmayan paketler için elle/native yol
  uygulanabilir (ör. frameless pencere: HTML titlebar → köprü `startSystemMove/Resize`).

## Doğrulama alışkanlığı

- **Web-shell UI görseli:** `node tools/webshot.mjs` mock-bridge'li UI'ı gerçek Chromium'da
  açıp `.uishots/*.png` üretir (Monaco/xterm dahil — her şey görünür). Ekran görüntüsü
  incelenmeden UI işi "bitti" sayılmaz. Gerçek uygulama: `python shell.py --dev` +
  `QTWEBENGINE_REMOTE_DEBUGGING` → CDP.
- **Köprü/motor:** `pytest tests/test_bridge.py` (webview'suz sözleşme testleri) +
  motor headless script.
- Alt-süreçlerde daima `encoding="utf-8", errors="replace"` ve gerektiğinde
  `PYTHONUTF8=1` (bkz. `docs/SETUP.md` — cp1254 tuzağı).
- Python komutları için gerçek yorumlayıcı yolu / PowerShell (bkz. `docs/SETUP.md`).
