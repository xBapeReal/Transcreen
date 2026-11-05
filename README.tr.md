# Transcreen

**Windows için canlı ekran çevirmeni.** Transcreen ekranı izler ve her yabancı yazının Türkçesini
**tam üzerine** çizer: aynı konumda, aynı boyutta, aynı renklerde. Google Lens gibi düşünebilirsin,
ama canlı ve tüm sistemde çalışır.

[English README](README.md)

Pencere yok, düğme yok. Uygulamayı açtığında ekrandaki yabancı yazılar yerlerinde Türkçeleşir.
Kaplama tıklamaları engellemez, altındaki her şeyi normal kullanmaya devam edersin. Oyunlarda,
videolarda, tarayıcıda ve masaüstü uygulamalarında aynı şekilde çalışır.

## Özellikler

- **Canlı, tüm ekran çeviri.** Değişmeyen ekranda bir döngünün maliyeti tek bir ucuz yakalamadır.
  Bir şey değiştiğinde yalnızca değişen bölge yeniden okunur.
- **Orijinal yazıyla eşleşir.** Transcreen kaynak yazının konumunu, boyutunu, kalınlığını (kalın
  yazıyı algılar) ve renklerini korur, ±30° civarına kadar eğik yazıları da destekler.
- **Parçaları değil cümleleri çevirir.** Satırlar paragraf bloklarında birleştirilip birlikte
  çevrilir, sonra özgün kutuya sarılarak yerleştirilir. Geniş kelime boşluklarıyla bölünmüş oyun
  altyazıları önce yeniden birleştirilir.
- **İsteğe bağlı çevrimdışı ilk boya.** Makinedeki CTranslate2 modeli (Opus-MT en→tr) yeni metni
  internet olmadan yaklaşık 0,1 sn'de çevirir. Bulut çevirisi gelince sessizce onun yerini alır.
- **Oyun ve video için tasarlandı.** Animasyonlu arka planlardaki OCR titremesini sönümler ve bir
  altyazı modu vardır. HUD sayaçları ("200 / 200") ve ikon gürültüsü ayıklanır, metin seçmek
  çeviriyi silmez.
- **Başka uygulamalara karışmaz.**
  - Kaplama ekran yakalamadan dışlanır, bu yüzden uygulama kendi çıktısını hiç okumaz.
  - Çizecek bir şey yokken gizlenir.
  - DRM korumalı bir pencere (Netflix vb.) öndeyken devre dışı kalır.
- **Kalıcı önbellek.** Daha önce çevrilmiş metin, yeniden başlatmadan sonra bile anında görünür.

## Gereksinimler

- Windows 10 (2004 veya sonrası) ya da Windows 11
- Python 3.11+ ve [uv](https://docs.astral.sh/uv/)
- En az bir Windows OCR dil paketi. İngilizce önerilir. Windows genellikle ekran dilinin paketini
  kendisi kurar; başka bir dil eklemek için **Ayarlar → Zaman ve Dil → Dil ve bölge** yolunu izle.
- Bulut çevirisi için internet bağlantısı. Çevrimdışı model kuruluysa zorunlu değildir.

## Hızlı başlangıç

1. Bu repoyu indir veya klonla.
2. **`Transcreen.bat`** dosyasına çift tıkla. İlk çalıştırma eksik olan her şeyi kurar, bu bir
   kereye mahsus birkaç dakika sürer:
   - resmi kurulum betiğiyle [uv](https://docs.astral.sh/uv/);
   - Python bağımlılıkları;
   - çevrimdışı çeviri modeli (opus-mt-tc-big en→tr, yaklaşık 476 MB).

   Sonraki açılışlar yalnızca kısa bir kontrol yapar ve konsol olmadan doğrudan başlar. Açılışta
   kısa bir bilgi balonu kısayolları gösterir.

Model indirilemezse Transcreen bulut çevirisiyle açılır ve başlatıcı bir sonraki açılışta yeniden
dener. Çevrimdışı modeli hiç istemiyorsan ilk açılıştan önce
`%APPDATA%\Transcreen\config.json` içinde `"use_local_mt": false` yap.

Zayıf bir makinede bunun yerine daha hafif Argos modelini kurabilirsin (yaklaşık 125 MB, kalitesi
belirgin biçimde daha düşük). İlk açılıştan önce proje klasöründe PowerShell'de şunu çalıştır:

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA\uv-envs\transcreen"
$env:PYTHONPATH = "src"
uv run python -m transcreen --setup-local --small-model
```

| Kısayol      | İşlev |
| ------------ | ----- |
| `Ctrl+Alt+T` | Duraklat / devam ettir (kaplama gizlenir) |
| `Ctrl+Alt+S` | Ekran görüntüsü modu: 8 sn boyunca çeviriler senin ekran görüntülerine de girer |
| `Ctrl+Alt+Q` | Çıkış |

Uygulamayı PowerShell'den elle başlatmak için proje klasöründe şunu çalıştır:

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA\uv-envs\transcreen"
$env:PYTHONPATH = "src"
uv run python -m transcreen
```

## Tek dosya sürümü (taşınabilir)

`tools/build_onefile.py` uygulamanın tamamını tek bir Python dosyasında toplar:

```powershell
uv run python tools/build_onefile.py    # dist/Transcreen.py dosyasını üretir
```

`Transcreen.py` dosyasını istediğin klasöre veya başka bir Windows bilgisayara kopyala; çift
tıkla ya da `python Transcreen.py` ile çalıştır. Hedef bilgisayarda yalnızca python.org'dan
Python 3.10–3.13 gerekir. Dosya ilk çalıştırmada paketlerini bir kez `%LOCALAPPDATA%\Transcreen`
içine kurar (1–3 dk sürer ve internet ister), sonra kendini oradan yeniden başlatır. `--selftest`,
`--setup-local` ve `--smoke` bayrakları aynı şekilde çalışır.

## Ayarlar

Ayar arayüzü yoktur. `%APPDATA%\Transcreen\config.json` dosyasını elle düzenle:

```json
{
  "target_lang": "tr",
  "ocr_lang": "auto",
  "idle_poll": 0.012,
  "min_text_height": 10,
  "use_local_mt": true
}
```

| Anahtar | Anlamı |
| ------- | ------ |
| `target_lang` | Google dil kodu olarak hedef dil. Uygulama Türkçe için tasarlandı: çevrimdışı model, "zaten Türkçe" tespiti ve arayüz mesajları `tr` varsayar. |
| `ocr_lang` | `auto` (İngilizce paketi kuruluysa onu seçer) veya `en-US` gibi bir etiket. |
| `idle_poll` | Değişmeyen ekran kontrolleri arasındaki bekleme (sn). Daha düşük değer daha hızlı tepki verir ama daha çok CPU kullanır. |
| `min_text_height` | Bu kadar pikselden kısa satırlar gürültü sayılır. |
| `use_local_mt` | Kuruluysa ilk boya için çevrimdışı modeli kullanır. `false` yapılırsa başlatıcı modeli indirmez. |

## Gizlilik

- **Yerel adımlar.** Ekran yakalama ve OCR senin bilgisayarında yapılır. Transcreen yerleşik
  Windows OCR motorunu kullanır ve ekran görüntülerini hiçbir yere yüklemez.
- **Bulut çevirisi.** Yalnızca çevrilmesi gereken tanınmış metin satırları gönderilir:
  - Google Çeviri'ye, tarayıcı eklentilerinin de kullandığı herkese açık ama belgelenmemiş
    `translate.googleapis.com` ucu üzerinden;
  - Google hız sınırı koyarken yedek olarak [MyMemory](https://mymemory.translated.net/)'ye.

  Ekranda hassas içerik varken `Ctrl+Alt+T` ile duraklat.
- **Yerel dosyalar.** Çeviriler `%APPDATA%\Transcreen\cache.json` içinde önbelleğe alınır, günlük
  `%APPDATA%\Transcreen\app.log` dosyasına yazılır. Her şeyi silmek için bu klasörü sil.
- **İzleme yok.** Telemetri, hesap ya da API anahtarı yoktur.

## Nasıl çalışır

```
screen  →  vision  →  ocr  →  translator  →  overlay
 (mss)    (değişim     (Windows   (önbellek, yerel  (tıklama geçirgen,
          tespiti,      OCR,       model, bulut,     yakalamadan
          tipografi)    bantlar)   arka plan)        dışlanmış tuval)
                   app: canlı döngü, kararlılaştırma, kısayollar
```

1. **Yakalama.** Ekran `mss` ile yakalanır (yaklaşık 15–21 ms) ve küçük bir gri imzaya indirgenir.
   Değişmeyen ekranda döngünün tüm maliyeti budur ve OCR hiç çalışmaz.
2. **Değişim tespiti.** İmza 16×9 karoya bölünür ve yalnızca değişen bölge OCR'lanır. Büyük
   bölgeler yatay bantlara bölünüp dört OCR motorunda paralel okunur. 140 px'lik bir şerit
   yaklaşık 55 ms, tam kare yaklaşık 63 ms sürer.
3. **Hareket.** Ekran hareket halindeyken (kaydırma, geçiş) OCR en fazla 0,25 sn bekler, böylece
   bulanık kareler okunmaz. Video gibi sürekli harekette her geçiş canlı kareyi okur.
4. **Kararlılaştırma.** Animasyonlu arka planlar OCR'ı titretir. Okumalar kareler arasında
   oylanır:
   - yeni bir satır çizilmeden önce iki kez görülmelidir;
   - bozuk bir okuma temiz bir satırın yerini alamaz;
   - pikselleri boşalan satırlar anında kalkar.
5. **Çeviri.** Arka plandaki bir iş parçacığında çalışır. Çevirmen şu sırayla ilerler:
   1. Önbellekteki satırları anında sunar.
   2. Model kuruluysa eksikleri çevrimdışı modelle boyar.
   3. Sonucu yükseltmek için bulut çevirisini getirir (kalıcı bağlantı üzerinden Google, yaklaşık
      100–300 ms). Google hız sınırı koyarsa uygulama bir bekleme süresi boyunca MyMemory'ye
      geçer. Yedek bir servisin kaynak metni aynen geri döndürdüğü sonuçlar asla önbelleğe
      alınmaz.
6. **Çizim.** Font boyutu kaynak harflerin ölçülen mürekkep yüksekliğinden hesaplanır. Kapak ve
   yazı renkleri kaynak piksellerden örneklenir. Tüm kapaklar yazılardan önce çizilir, böylece
   üst üste binen bloklar birbirini örtmez.

Tipik gecikme: yerel bir değişiklik yaklaşık 0,08–0,1 sn'de, tam ekran değişiklik yaklaşık
0,1–0,15 sn'de görünür. İlk kez görülen metin, çeviri için 0,1–0,3 sn daha ister. Videodaki
altyazılar yaklaşık 0,1–0,3 sn içinde Türkçeleşir.

## Sorun giderme

- **Balon "Çeviri alınamadı" diyor.** Transcreen'in internet bağlantısı yok. Bağlantı gelince
  kendiliğinden toparlar.
- **Balon "Windows OCR dil paketi yok" diyor.** Hiç OCR dil paketi kurulu değil. Ayarlar → Zaman ve
  Dil → Dil ve bölge altından bir dil ekle.
- **Bir kısayol çalışmıyor.** Bu kısayolu başka bir uygulama almış olabilir; bu durumda bir balon
  seni uyarır. Kısayol olmadan çıkmak için Görev Yöneticisi'nden `pythonw` işlemini sonlandır.
- **Bilinen sınırlar:**
  - DRM korumalı pencereler yakalamada siyah görünür.
  - Hızlı kayan jenerik gibi çok kısa ömürlü yazılar kaçabilir.
  - Windows OCR bazen "x2" gibi çok küçük jetonları görmez.
  - Çevrimdışı modelin kalitesi bulutun altındadır.

## Geliştirme

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA\uv-envs\transcreen"   # venv'i eşitlenen klasörlerin dışında tutar
uv sync
uv run pytest
uv run ruff check src tests tools ; uv run ruff format src tests tools
uv run mypy src
$env:PYTHONPATH = "src"; uv run python -m transcreen --selftest   # OCR + çeviri + zamanlama raporu
$env:PYTHONPATH = "src"; uv run python -m transcreen --smoke out  # kaplamanın ekran görüntüsünü ./out içine kaydeder
uv run python tools/subtitle_bench.py full                         # video altyazı gecikme testi
```

**Neden `PYTHONPATH=src` ve `tool.uv.package = false`?** Python 3.11 `.pth` dosyalarını ANSI kod
sayfasıyla okur, bu yüzden proje yolunda ASCII dışı karakterler varsa düzenlenebilir (editable)
kurulum bozulur. Bu nedenle uygulama her zaman doğrudan `src/` içinden çalışır.

Proje düzeni: `src/transcreen/` uygulama modüllerini, `tests/` pytest takımını (Windows OCR
testleri motor yoksa kendini atlar), `tools/` ise tek dosya derleyicisini ve altyazı gecikme
testini içerir.

## Teşekkürler

- Helsinki-NLP'nin [Opus-MT tc-big en→tr](https://huggingface.co/Helsinki-NLP/opus-mt-tc-big-en-tr)
  modeli (CC BY 4.0). Transcreen bu modelin CTranslate2 dönüşümünü kullanır:
  [`ooeoeo/opus-mt-tc-big-en-tr-ct2-float16`](https://huggingface.co/ooeoeo/opus-mt-tc-big-en-tr-ct2-float16).
- `--small-model` seçeneği [Argos Translate](https://github.com/argosopentech/argos-translate)
  en→tr paketini kurar.
- Kütüphaneler: [Pillow](https://python-pillow.org/), [mss](https://github.com/BoboTiG/python-mss),
  [winsdk](https://github.com/pywinrt/python-winsdk),
  [deep-translator](https://github.com/nidhaloff/deep-translator),
  [CTranslate2](https://github.com/OpenNMT/CTranslate2),
  [SentencePiece](https://github.com/google/sentencepiece).

Çeviri modelleri kurulum sırasında indirilir, bu repoya dahil değildir. Transcreen'in Google veya
MyMemory ile bir bağı yoktur. Kullandığı Google ucu belgelenmemiştir ve her an değişebilir ya da
sınırlanabilir, bu yüzden lütfen sorumlu kullan.

## Lisans

[MIT](LICENSE)
