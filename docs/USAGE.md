# Kullanım

## Değişiklik makbuzu

Her AI koşusu bittikten sonra sağ paneldeki geçmişten **Makbuz** seçilebilir.
Makbuz, görev, plan/kapsam, önerilen diff, reviewer kararı, maliyet, uygulama ve
checkpoint durumunu saklar. Test veya komut çalıştırılmadıysa bunu kanıt gibi
göstermek yerine açıkça bildirir. Kullanıcı isterse makbuzu seçtiği klasöre
Markdown olarak dışa aktarabilir.

## Proje komutu güveni

F5/Ctrl+F5 ile proje komutu ilk kez çalıştırılmadan önce uygulama komutu, kaynağını
ve çalışma klasörünü gösterir. Onay aynı proje ve aynı komut için hatırlanır;
`.magent/run.json` ya da sezgi sonucu değişirse tekrar sorulur.

Arayüzler aynı motoru kullanır; hangisini seçeceğin işine bağlı.

| Arayüz | En iyi olduğu iş |
|--------|------------------|
| **Masaüstü mini-IDE** (`shell.py`) ★ | Var olan projede çalışmak — tam IDE deneyimi |
| **Web** (`app.py`) | Sıfırdan tek dosya üretmek, akışı canlı izlemek |
| **Terminal** (`orchestrator.py`) | Hızlı, otomasyona uygun tek dosya üretimi |

---

## Masaüstü mini-IDE (`shell.py`) ★

```bash
# önce bir kez derle (bkz. SETUP.md 2b):
cd web/ui && npm ci && npm run build && cd ../..

python shell.py            # web/ui/dist'i app:// üzerinden yükler
python shell.py --dev      # Vite dev sunucusu (HMR) + F12 DevTools — geliştirme
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
- **Sürükle-taşı** (gezgin): dosya/klasörü başka klasöre sürükle → taşınır (hedef accent
  çerçeveyle vurgulanır; boş alana bırak → köke). Sekmeler de sürüklenerek sıralanır;
  sekmeye **sağ-tık** → Diğerlerini/Sağdakileri/Tümünü Kapat (kaydedilmemişler korunur).
- **Zoom & kaydırma:** Ctrl+= / Ctrl+- / Ctrl+0 arayüzü büyütür-küçültür (kalıcı);
  **Alt+Z** editörde satır kaydırmayı açar. Diff sekmesindeki düğmeyle **yan-yana ↔
  inline** görünüm değişir.
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

**IntelliSense / dil zekâsı (P7):**
- **Python** — basedpyright dil sunucusu proje açılınca otomatik başlar (statusbar'da
  sağ altta "dil sunucusu hazırlanıyor…" → yeşil **⏻ Py**). Yazarken **otomatik
  tamamlama** (Ctrl+Space ile elle de tetiklenir), hatalı kodda **kırmızı alt çizgi**
  (üzerine gel → mesaj), **F12 / Ctrl+tık** tanıma git (başka dosyaya da sekmede
  açarak gider), hover'da **imza + docstring**, `(` yazınca **parametre yardımı**.
- **TS/JS** — aynı dörtlü Monaco'nun kendi dil servisinden; tanıma-git açık sekmeler
  arasında çalışır.

**Çalıştır (P8.1) & Debug (P8.2):**
- **F5** — VS Code düzeni: debug oturumu durmuşsa **devam eder**; aktif dosya
  `.py` ise **debug başlatır**; değilse dosyayı debugsuz koşar. **Ctrl+F5**
  projeyi debugsuz koşar (npm dev/start, cargo, go veya main/app.py — sezgisel;
  paletten "Çalıştırma Komutunu Değiştir…" → `.magent/run.json`). Titlebar'daki
  **▶** aktif dosyayı debugsuz koşar. **Shift+F5** veya ■ durdurur.
- Çıktı (koşu da debug de) alt paneldeki **ÇIKTI** sekmesine akar (renkli);
  bitince yeşil/kırmızı **çıkış kodu rozeti** + süre.
- **Debug** — satır numarasının soluna tıkla (veya **F9**) → kırmızı breakpoint
  (proje başına kalıcı). Aktivite çubuğundaki 🐞 → **ÇALIŞTIR VE DEBUG** görünümü:
  Debug Başlat düğmesi; durunca kontrol şeridi (devam · **F10** üzerinden ·
  **F11** içine · **Shift+F11** dışına · durdur), **çağrı yığını** (tık →
  satıra git), **değişkenler** (tembel ağaç — genişlet), breakpoint listesi.
  Durulan satır amber vurgulanır ve editör oraya kayar.

**Ajanlarla değişiklik (P2 — sağdaki AI EKİBİ paneli):**
1. Görev kutusuna yaz (ör. *"utils.py'deki tarih biçimini ISO 8601 yap"*), istersen
   rol→model seçimlerini değiştir, **▶** (veya Enter).
2. **EKİP hattı** canlı işler: aktif ajan nefes alır, biten aşamada model·süre·token·
   maliyet görünür. **Akış** sekmesinde aşama kartları + çıktılar; hata olursa kırmızı
   kart. Aşama bitince çıktı **markdown** olarak işlenir — kod blokları editör temasında
   renklenir, üzerine gelince kopyala düğmesi çıkar.
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

**Kaynak denetimi / git (P4):**
- **Ctrl+Shift+G** veya aktivite çubuğunda dal ikonu (veya Ctrl+K → "Kaynak Denetimi"). Dal + ileri/geri
  sayaçları, değişiklik listeleri (durum harfi renkli: M/A/D/R/U). Satıra tıkla →
  **merkez diff**; hover'da **+** hazırla, **−** çıkar, **↩** değişikliği at (onaylı).
  Mesaj yaz + **Commit** (veya Ctrl+Enter). Dosya kaydettikçe görünüm kendini tazeler.
- Git durumu her yerde: **gezginde** değişen dosya adları renklenir (M sarı, yeni yeşil)
  ve değişiklik içeren klasörlerde nokta belirir; **durum çubuğunda** dal adı + ileri
  sayacı + değişiklik sayısı görünür (tıkla → kaynak denetimi).

**Ayarlar (P4):**
- Aktivite çubuğu dişli ikonu veya Ctrl+K → "Ayarlar". Vurgu rengi (6 seçenek, anında
  uygulanır), yoğunluk (Rahat/Sıkı), Enter davranışı, animasyonlar.

**Entegre terminal (gerçek PTY):**
- **Ctrl+`** panel aç/kapat, **Ctrl+Shift+`** yeni terminal (sekmeli). Gerçek ConPTY
  PowerShell: ok tuşları, renkler, `python` REPL, interaktif programlar — hepsi çalışır.
  Proje köküne açılır; UTF-8.

Güvenlik: ajanlar seçtiğin klasörün **dışına çıkamaz** (yol güvenliği `project.py`).

> **Geliştirici notu — görsel doğrulama:** `node tools/webshot.mjs` mock-bridge'li UI'ı
> gerçek Chromium'da açıp `.uishots/*.png` üretir (Monaco/xterm dahil). `--dev` +
> `QTWEBENGINE_REMOTE_DEBUGGING` ile gerçek uygulamanın webview'i de CDP'den görüntülenebilir.

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
