# Katkı ve Çalışma Kuralları

## 📌 Dokümantasyon Güncelleme Kuralı (ZORUNLU)

> **Her adımda ve her iterasyonda, o değişikliğin etkilediği dokümanlar AYNI turda
> güncellenir. Bir değişiklik, dokümantasyonu güncellenmeden "bitti" sayılmaz.**

Bu kural hem insan katkıcılar hem de yapay zeka ajanları (Claude dahil) için geçerlidir.
Amaç: kod ile doküman arasındaki farkın hiçbir zaman birikmemesi.

### Her iterasyonun sonunda yapılacaklar (checklist)

1. Kullanıcıyı veya katkıcıyı etkileyen değişikliklerde **`docs/CHANGELOG.md`'yi güncelle**.
2. Aşağıdaki tabloya göre **etkilenen public dokümanı güncelle**.
3. Yeni bir kullanıcı yüzeyi veya komut eklediysen README ya da kullanım rehberine ekle.
4. Anahtar, dosya yazma, komut çalıştırma veya dış veri aktarımı etkileniyorsa
   `PRIVACY.md` ve gerektiğinde `SECURITY.md` aynı değişiklikte güncellenir.

### Değişiklik türü → güncellenecek doküman

| Değişiklik | Güncellenecek |
|-----------|---------------|
| Yeni model/sağlayıcı davranışı | `README.md` + `docs/SETUP.md` |
| Yeni modül, katman, olay tipi, veri akışı | `docs/ARCHITECTURE.md` |
| Yeni köprü metodu/olayı (webhost ↔ web/ui) | `web/ui/src/bridge/protocol.ts` (tek kaynak) + `docs/ARCHITECTURE.md` |
| Yeni tasarım tokenı / değeri | `web/ui/src/styles/tokens.css` |
| Yeni arayüz özelliği / kullanım biçimi | `docs/USAGE.md` |
| Kurulum, bağımlılık, ortam tuzağı | `docs/SETUP.md` + `requirements.txt` |
| Yayınlanmış kullanıcı etkisi | `docs/CHANGELOG.md` |

### Faz tamamlama tanımı (Definition of Done)

Bir özellik şu üçü tamamlanınca biter:
1. Kod yazıldı ve **doğrulandı** (headless test ve/veya kullanıcı onayı).
2. İlgili dokümanlar güncellendi (yukarıdaki tablo).
3. Kullanıcıyı etkileyen değişiklikse `docs/CHANGELOG.md`'ye kayıt düşüldü.

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
