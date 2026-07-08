# Kullanım

Arayüzler aynı motoru kullanır; hangisini seçeceğin işine bağlı.

| Arayüz | En iyi olduğu iş |
|--------|------------------|
| **Web-shell** (`shell.py`) ★ | Yeni masaüstü mini-IDE (web-shell); var olan projede çalışmak |
| **Masaüstü classic** (`desktop.py`) | Eski Qt arayüzü — cutover'a dek yedek (`shell.py --classic`) |
| **Web** (`app.py`) | Sıfırdan tek dosya üretmek, akışı canlı izlemek |
| **Terminal** (`orchestrator.py`) | Hızlı, otomasyona uygun tek dosya üretimi |

---

## Web-shell mini-IDE (`shell.py`) ★ yeni

```bash
# önce bir kez derle (bkz. SETUP.md 2b):
cd web/ui && npm ci && npm run build && cd ../..

python shell.py            # web/ui/dist'i app:// üzerinden yükler
python shell.py --dev      # Vite dev sunucusu (HMR) + F12 DevTools — geliştirme
python shell.py --classic  # eski Qt arayüzü (desktop.py)
```

Düzen: üstte **web titlebar** (frameless — sürükle/çift-tık maximize, kendi min/max/kapat),
solda **aktivite çubuğu** + **dosya gezgini** (lazy ağaç), ortada **Monaco editör** (çok
sekmeli, dirty noktası), altta **durum çubuğu**.

**Şu an çalışan (P0+P1 tamam):**
- **Klasör Aç** (karşılama ekranı veya son projeler) → gezgin ağacı gelir.
- Klasöre tıkla → genişler; **dosyaya tıkla** → Monaco'da sekmede açılır (sözdizimi
  renklendirmeli). **Ctrl+S** kaydet (sekmede • = kaydedilmemiş), **Ctrl+W** kapat.
- **Sağ-tık** (gezgin): Yeni Dosya/Klasör, Yeniden Adlandır, Sil, Yolu Kopyala, Sistemde
  Göster — hepsi koyu temalı diyaloglarla (native beyaz pencere yok).
- **Ctrl+P** dosyaya git (fuzzy), **Ctrl+K** komut paleti, **Ctrl+B** gezgini aç/kapat.
- İşlem sonuçları sağ altta **toast** olarak görünür.
- **Kapatma koruması:** kaydedilmemiş sekme varken pencereyi kapatınca onay sorulur.
- **Oturum:** açık sekmeler + aktif sekme + **kabuk düzeni** (panel görünürlük/boyutları,
  aktif kenar görünümü) proje-içi `.magent/session.json`'da; yeniden açılışta geri gelir.
  Pencere geometrisi ve son projeler de hatırlanır.
- **Panel boyutlandırma:** gezgin/AI paneli/terminal kenarlarından **sürüklenerek**
  boyutlanır (üzerine gelince accent çizgisi belirir); ayırıcıya Tab ile odaklanıp
  ok tuşlarıyla da ayarlanabilir. Boyutlar oturumla saklanır.
- Dosyalar dışarıdan değişirse (başka editör/git) gezgin kendini tazeler.

**Ajanlarla değişiklik (P2 — sağdaki AI EKİBİ paneli):**
1. Görev kutusuna yaz (ör. *"utils.py'deki tarih biçimini ISO 8601 yap"*), istersen
   rol→model seçimlerini değiştir, **▶** (veya Enter).
2. **EKİP hattı** canlı işler: aktif ajan nefes alır, biten aşamada model·süre·token·
   maliyet görünür. **Akış** sekmesinde aşama kartları + çıktılar; hata olursa kırmızı kart.
3. Öneri gelince **Değişiklikler** sekmesi açılır + **merkez inline diff** kendiliğinden
   gelir (⇄ sekmesi). Satırlara tıklayıp dosya dosya incele; istemediklerinin işaretini kaldır.
4. **Uygula (n)** → dosyalar yazılır (.bak yedekli), açık sekmeler + ağaç tazelenir.
   **Vazgeç** → hiçbir şey yazılmaz. Koşuyu **■** ile durdurabilirsin.
5. Alt çubukta toplam token/maliyet sayaçla akar; ⏱ (saat) ikonu geçmiş koşuları açar —
   tıkla → görev composer'a geri gelir. Ctrl+J paneli gizler/gösterir.

**Projede arama (P4):**
- **Ctrl+Shift+F** → kenar çubuğunda ARA görünümü. Aa (büyük/küçük) ve .* (regex)
  toggle'ları; sonuçlar dosyaya gruplu; satıra tıkla → editörde o satıra gider.
  ripgrep kuruluysa onunla (çok hızlı), değilse Python taramasıyla çalışır.

**Ayarlar (P4):**
- Aktivite çubuğu dişli ikonu veya Ctrl+K → "Ayarlar". Vurgu rengi (6 seçenek, anında
  uygulanır), yoğunluk (Rahat/Sıkı), Enter davranışı, animasyonlar.

**Entegre terminal (P3 — gerçek PTY):**
- **Ctrl+`** panel aç/kapat, **Ctrl+Shift+`** yeni terminal (sekmeli). Gerçek ConPTY
  PowerShell: ok tuşları, renkler, `python` REPL, interaktif programlar — hepsi çalışır
  (classic'in komut-başına terminalinde imkânsızdı). Proje köküne açılır; UTF-8.

**Yol haritası (sonraki fazlar):** global arama + ayarlar + cila (P4), cutover (P5).
Bkz. [WEB-SHELL-PLAN.md](WEB-SHELL-PLAN.md).

> **Geliştirici notu — görsel doğrulama:** `node tools/webshot.mjs` mock-bridge'li UI'ı
> gerçek Chromium'da açıp `.uishots/*.png` üretir (Monaco/xterm dahil). `--dev` +
> `QTWEBENGINE_REMOTE_DEBUGGING` ile gerçek uygulamanın webview'i de CDP'den görüntülenebilir.

---

## Masaüstü classic (`desktop.py`) — lokal projede çalışma (eski arayüz)

> Bu, web-shell'e taşınmakta olan **eski** Qt arayüzüdür; cutover'da kaldırılacak.
> Yeni arayüz için üstteki "Web-shell mini-IDE" bölümüne bakın.

```bash
python desktop.py          # veya: python shell.py --classic
```

Düzen (Cursor benzeri): üstte **özel başlık/komut çubuğu** (frameless — kendi min/max/
kapat düğmeleri), solda **aktivite çubuğu** + **dosya gezgini**, ortada **Monaco editör**
(tam boy, çok sekmeli), sağda **AI sohbet paneli**: dikey **EKİP timeline** (Planner→
Coder→Reviewer, canlı model·süre·maliyet), **Akış** / **Değişiklikler** sekmeleri ve altta
**composer** (rol-ikonlu 3 model seçici + görev kutusu + **Çalıştır**). En altta ince
**durum çubuğu** projeyi, ajan modellerini ve son çalıştırmanın toplam token/maliyetini
canlı gösterir.

**Editör kullanımı**
- Gezginden bir dosyaya **çift tıkla** → editörde yeni sekmede açılır (sözdizimi
  renklendirmeli).
- Düzenle; sekmede nokta (•) "kaydedilmemiş" demektir. **Ctrl+S** veya **Kaydet** ile yaz.

**Entegre terminal**
- **Ctrl+`** ile alt terminal panelini aç/kapat; **Ctrl+Shift+`** yeni sekme. Terminal
  proje kökünde çalışır; komut yaz + Enter, çıktı canlı akar. `cd` ile dizin değiştir,
  ↑/↓ ile geçmiş, `clear` ile temizle, çalışan komutu **Ctrl+C** / Durdur ile kes.
- Command palette (**Ctrl+K**) → "Terminal aç/kapat" / "Yeni terminal".

**Ajanlarla değişiklik**
1. **Proje Seç** — ajanlar dosya ağacını görür.
2. **Görev yaz** (alt kutu) — ör. *"config.py'ye loglama seviyesi ayarı ekle"*.
3. (İsteğe bağlı) her role model seç (Planner/Coder/Reviewer).
4. **Çalıştır** — Planner dosyaları seçer, Coder değişiklikleri üretir, Reviewer inceler;
   sağ paneldeki **Değişiklikler** sekmesinde gösterilir (verdict rozeti + dosya bazında
   kabul/ret kutuları + renkli diff).
5. **Uygula** — dosyalara yazılır, açık sekmeler tazelenir, var olanlar `.bak` yedeklenir.
   **Vazgeç** — hiçbir şey yazılmaz.

Güvenlik: ajanlar seçtiğin klasörün **dışına çıkamaz**.

> Konsol (çalıştır/derle) ve debugger sekmeleri yol haritasının 2. ve 3. fazında gelecek
> (bkz. [ROADMAP.md](ROADMAP.md)).

Tek `.exe` yapmak:

```bash
pyinstaller --noconsole --onefile desktop.py
```

---

## Web arayüzü — sıfırdan üretim

```bash
python app.py        # -> http://127.0.0.1:5000
```

- Görev gir, her role model ata, tur sayısı ve "kodu çalıştır" seçeneğini ayarla.
- Her adım kart olarak: **model · süre · token · maliyet**.
- Sonuç `output/result.py`'a yazılır; üstte toplam metrikler görünür.
- "Kodu çalıştır" (execution grounding) açıksa üretilen kod gerçekten çalıştırılıp
  çıktısı/hata varsa Coder'a geri beslenir.

---

## Terminal

```bash
python orchestrator.py "1'den 10'a kadar asal sayıları yazan kod"
python orchestrator.py "..." --run      # üretilen kodu gerçekten çalıştır
```

Akış terminale yazılır (PLAN → CODE → çalıştırma → REVIEW → düzeltme), sonuç
`output/result.py`'a kaydedilir ve toplam süre/token/maliyet gösterilir.

> **Not:** git-bash'te `python` çalışmazsa PowerShell kullan (bkz. [SETUP.md](SETUP.md)
> "Ortam tuzakları").

---

## Rolleri / modelleri değiştirme

- Arayüzden: Planner/Coder/Reviewer açılır menüleri.
- Koddan kalıcı olarak: `agents.py` içindeki `DEFAULT_ROUTING`.
- Bir rolün talimatını değiştirmek: `agents.py` içindeki `ROLE_PROMPTS`.

## Maliyeti düşürme

Tipik olarak maliyetin çoğu **Planner (Claude)**'dan gelir. Denemek istersen Planner'ı da
DeepSeek'e alıp (routing) sonucu karşılaştır. Ölçümler her çalıştırmada arayüzde görünür.
