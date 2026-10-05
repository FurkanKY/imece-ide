# Imece

> Açık beta · `v0.4.0-beta.1` · kaynak sürüm, Windows öncelikli, kaynaktan Linux'ta da çalışır · [English](README.md)

**Imece**, yerel-önce bir masaüstü kodlama ortamıdır. Bir **görev** yazarsın,
**tek** bir sağlayıcı seçersin ve bağımsız bir kodlama ajanı işi baştan sona
— projenin kendi ayrı, geçici kopyasında, sonucun arkasında deterministik
doğrulama ile — yürütür. Diff'i inceler, **uygulamayı** ya da **vazgeçmeyi**
sen karar verirsin. Sen söylemedikçe dosyalarına dokunulmaz; uygulama önce
checkpoint alır, dolayısıyla tek tıkla geri alınır.

Bu, **bugün gerçekten varsayılan olan akış** — bir yol haritası maddesi
değil. Varsayılan akışta planlama adımı, inceleme adımı veya rol zinciri
yoktur: görev girer, tek ajan çalışır, kanıt çıkar, kararı sen verirsin.
Eski Planner/Coder/Reviewer üçlüsü hâlâ bir **uyumluluk arka ucu ve tarihsel
kayıt** olarak durur — varsayılan değildir ve planlanan ürün de değildir.

Bu döngünün çevresinde Imece'da tanıdık bir kabuk var: dosya gezgini, Monaco
editör, entegre terminal, Git görünümü, Python dil zekâsı ve debug. Bunlar
**ikincil araçlar** olarak korunuyor; uygulamayı bir görev listesine göre
yeniden şekillendirmek şu an başlanan kilometre taşıdır ve **henüz yapılmadı**.

Ayrıca **deneysel bir işbirliği temeli** var: depo başına açık, çevrimdışı,
önce meta veri odaklı bir oturum; seçtiğiniz yollar için özel öneriler;
checkout'unuz dışında kurulan birleşik aday; ve `127.0.0.1` üzerinde
kapatılabilir bir döngü içi kontrol kanalı. Varsayılan akışa **bağlı değil**,
**LAN eşlemesi ya da TLS yok** ve **takım ürünü değildir** — bkz.
[İşbirliği](docs/COLLABORATION.md).

![Imece'de AI önerisini inceleme](docs/assets/review.png)

> **Ekran görüntüsü: eski arayüz.** Bu görüntü tek görevli varsayılan akıştan
> önce çekildi ve önceki üç rol düzenini gösteriyor.

> **Durum, açıkça söyleyerek.** Rol bağımsız dikey akış — görev → tek ajan →
> doğrulama kanıtı → devam ya da açık uygulama — **yerel fixture uçtan uca
> koşularıyla doğrulanmış olarak uygulanmıştır**. Ancak **bitmiş değildir**:
> gerçek sağlayıcı ve desteklenen platform kabulü, gereken ortamlar
> kullanılamadığı için **ertelenmiş** ve izlenen bir doğrulama borcu olarak
> duran **açık bir eşiktir**; bu yüzden hiçbir canlı kalite garantisi
> iddia edilmez. Kilometre taşı 2 (görev öncelikli kabuk ve gerçek eşzamanlılık)
> **yetkilendirildi ve başlanıyor, uygulanmış değil**; kilometre taşı 3–5
> (yeniden başlatma dayanıklılığı, iki makine eşleşmesi, paketlenmiş sürüm)
> **başlanmadı**. Kilometre taşı 1'in ertelenmiş eşiği, kilometre taşı 5'ten
> önce bir yayın eşiği olarak durur ve kilometre taşı 2'yi **engellemez**.
> Bkz. izlenen [ürün planı](docs/PRODUCT-PLAN.md).

## Nasıl çalışır

1. Bir yerel proje klasörü aç ve görevini yaz.
2. Yerleşik katalogdan **tek** bir sağlayıcı seç (DeepSeek, Gemini, OpenAI,
   Mistral, Groq, xAI, Qwen, Moonshot, OpenRouter, Ollama, özel OpenAI-uyumlu
   uçlar ya da Claude Code gibi bir ajan CLI) ve **▶**'a bas. Ajan, ayrı bir
   Git worktree'sinde çalışır — asla gerçek dosyalarında değil — ve sağlayıcı
   bildirdiğinde koşu başına gecikme, jeton ve maliyet metrikleriyle.
3. Doğrulama aynı izole kopya içinde deterministik olarak çalışır ve kanıtı
   görürsün: ne kontrol edildi, ne geçti, ne başarısız oldu ya da
   çalıştırılmadı.
4. Memnun değilsen bir **takip isteği** gönder — aynı koşu aynı worktree'den
   ve aynı özgün görevden devam eder.
5. Memnunsan diff'i dosya dosya incele, sonra **Uygula** ya da **Vazgeç**.
   Uygulama önce checkpoint alır. **Vazgeç** hiçbir şey yazmaz.
6. Her koşu bir değişiklik makbuzu bırakır: görev, kapsam, diff, doğrulama
   durumu, maliyet ve uygulama/checkpoint durumu.

## Hızlı başlangıç

Imece IDE şimdilik yalnız **kaynak kod** olarak dağıtılıyor; hazır bir
`.exe` paketi henüz yayınlanmıyor. Masaüstü kabuk önce Windows 10/11 hedefler,
kaynaktan çalıştırıldığında Linux'ta da kullanılabilir; Python 3.14 ve
Node ≥ 20 gerekir (Linux notları dahil ayrıntı: [SETUP](docs/SETUP.md)).

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
cd web/ui && npm ci && npm run build && cd ../..
python shell.py
```

Ayarlar → Model sağlayıcıları'ndan bir sağlayıcı seçip API anahtarını
yapıştırın — katalog DeepSeek, Gemini, OpenAI, Mistral, Groq, xAI, Qwen,
Moonshot, OpenRouter, Ollama ve özel OpenAI-uyumlu uçları kapsar.
[Claude Code](https://claude.com/claude-code) gibi ajan CLI'ları anahtar
istemez; kuruluysa otomatik algılanır. Sonra bir proje klasörü açın, görevi
yazın, besteci'de tek bir sağlayıcı seçin, önerilen diff'i inceleyin, ardından
Uygula veya Vazgeç seçin.

> Katalogdaki her girdi tek ajan akışını süremez. Bir sağlayıcı desteklenmiyorsa
> arayüz bunu söyler ve koşuyu sessizce başka bir şeyle değiştirmeden reddeder.
> Ajan CLI (ACP) sağlayıcıları kullanımı her zaman bildirmediği için maliyet ve
> jeton sayaçları `—` gösterebilir; bu "bildirilmedi" demektir, "sıfır" değil.

Telemetri yoktur ve Imece'ye ait sunucu bir hizmet bulunmaz. Anahtarlar
makinede saklanır; arayüze dönmez, isteme konmaz ve hata metnine yazılmaz —
ancak **seçtiğiniz sağlayıcıya**, o sağlayıcının kimlik doğrulama protokolü
gerektirdiği şekilde iletilir. Her AI koşusu yerel değişiklik makbuzu
üretir. Windows paketi PyInstaller ile derlenebilir (`packaging/build.ps1`),
ancak resmî binary yayını beta olgunlaşana kadar ertelendi.

Kurulum ve kullanım ayrıntıları (İngilizce): [ürün planı](docs/PRODUCT-PLAN.md),
[SETUP](docs/SETUP.md), [USAGE](docs/USAGE.md), [RELEASE](docs/RELEASE.md),
deneysel [işbirliği temeli](docs/COLLABORATION.md),
deneysel ve isteğe bağlı [karar katmanı](docs/DECISION-LAYER.md), ayrıca
[gizlilik](PRIVACY.md) ve [güvenlik](SECURITY.md).

Lisans: [Apache-2.0](LICENSE).
