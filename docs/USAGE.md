# Kullanım

Üç arayüz de aynı motoru kullanır; hangisini seçeceğin işine bağlı.

| Arayüz | En iyi olduğu iş |
|--------|------------------|
| **Masaüstü** (`desktop.py`) | Var olan bir projede çalışmak (diff öner → uygula) |
| **Web** (`app.py`) | Sıfırdan tek dosya üretmek, akışı canlı izlemek |
| **Terminal** (`orchestrator.py`) | Hızlı, otomasyona uygun tek dosya üretimi |

---

## Masaüstü uygulaması (mini-IDE) — lokal projede çalışma

```bash
python desktop.py
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
