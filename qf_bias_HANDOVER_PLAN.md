# QF_BIAS — HANDOVER PLAN untuk Claude Berikutnya
*Disusun 2026-06-04. Deploy: `biasmodel.streamlit.app`. Repo GitHub, deploy GUI-only (tanpa CLI lokal).*

Pemilik: Nathanael (Jakarta, WIB/UTC+7). Gaya: langsung, tantang asumsinya, jangan setuju
default, jangan basa-basi. Disiplin proyek yang DIKUNCI:
> **Bias = confluence, bukan sinyal arah. Engine deterministik yang MENGHITUNG skor.
> Sumber/AI hanya MENGUKUR, tidak pernah menentukan poin bias. Semua bobot = PLACEHOLDER
> sampai backtest.** Hindari mengulang "metavulus" (confidence tinggi tak tervalidasi).

---

## 1. STATUS SAAT INI (yang sudah jalan & yang baru dibuang)

Jalan di deploy:
- **Bias Board / Pair Scanner / Detail Skor / News Feed / Risk Events** semua hidup.
- **Prices** (yfinance, 11 simbol), **FRED rates** (8, untuk R_hard rate_diff — *ini tetap dipakai untuk RATES, bukan actual*), **COT** (CFTC, 9 ccy; XAU missing diterima), **Calendar** (faireconomy `ff_calendar_thisweek.json`, ~115 event; `lastweek`/`nextweek` = 404, diterima).
- **News multi-source**: FinancialJuice + Fed (`federalreserve.gov/feeds/press_monetary.xml`) + ForexLive. Engine `news_overlay` SUDAH mengklasifikasi arah untuk USD/EUR/GBP/JPY/AUD/NZD/CAD/CHF + bank sentral (boj→JPY, rba→AUD, dst). Cap overlay ±30.
- **Risk Events**: mode Hari Ini / Minggu Ini (released ditampilkan jelas, tidak terkubur expander). Window-logic benar (diuji vs data faireconomy asli). Diagnostik filter lengkap.
- **News Feed**: filter aset, sembunyikan netral (default ON), ringkasan net-Δ per aset, sort magnitude/terbaru, status sumber.

**BARU DIBUANG (2026-06-04): seluruh enrichment ACTUAL via FRED + DBnomics + FMP.**
Alasan: **menyesatkan.**
- DBnomics: salinan Eurostat-nya BASI berbulan-bulan → mengembalikan 1.9% untuk flash HICP Mei yang nilai benarnya 3.2% (dikonfirmasi dari rilis resmi Eurostat). Telat = tidak bisa untuk surprise real-time.
- FRED (untuk actual): mirror + lag/vintage, plus sempat salah-atribusi (ADP → PAYEMS).
- FMP economic calendar: BERBAYAR (free tier mati, v3 deprecated + /stable/ paywalled).

Jadi `app.py` sekarang **TIDAK** mengisi `actual` sama sekali → tampil "–" (jujur) sampai
layer API-resmi-langsung dibangun (Modul A di bawah). File `collectors/indicators_us.py`,
`collectors/indicators_world.py`, `collectors/actuals_fmp.py` **sudah dilepas dari alur app.py**
dan TIDAK disertakan di build ini — boleh dihapus dari repo. (Ambil ide bagusnya: alignment-guard
& σ-from-history & polarity & time-decay — lihat §3.)

> Catatan jujur untuk next Claude: FRED-US sebenarnya cukup andal & timely (FRED menyerap BLS
> cepat); yang benar-benar menyesatkan adalah DBnomics (basi). User memilih membuang KEDUANYA demi
> satu pendekatan konsisten = **API resmi langsung per negara**. Saat membangun Modul A, boleh
> diskusikan ulang: pakai BLS-direct untuk US, atau pertahankan FRED khusus US (andal) + direct-API
> untuk non-US. Jangan asumsikan sendiri — tanyakan.

---

## 2. PELAJARAN MAHAL (jangan diulang)

1. **Kalender ber-`actual` = produk BERBAYAR** hampir di mana-mana (FMP paid, Finnhub econ-cal premium, TradingEconomics guest kosong). Jangan habiskan waktu mencari free calendar siap-pakai.
2. **Agregator (DBnomics / FRED-mirror) TELAT** untuk rilis terbaru → tidak cocok untuk surprise real-time. Mereka bagus untuk DATA HISTORIS (grafik), bukan live.
3. **Flash ≠ Final.** Event kalender (mis. EUR HICP) itu *flash* (akhir bulan); dataset resmi sering memuat *final* (pertengahan bulan berikutnya). Pastikan ambil nilai flash/estimate, bukan final lama.
4. **Unit-matching = landmine.** Forecast faireconomy di-parse oleh `_parse_number` (mis. "118K"→118000, "0.3%"→0.3). Actual dari sumber HARUS unit yang sama. Selalu uji per indikator.
5. **Selalu pasang ALIGNMENT GUARD** (lihat §3): terima actual hanya bila `previous` sumber ≈ `previous` kalender. Ini yang menangkap DBnomics-basi. WAJIB dipertahankan di layer baru.
6. **Verifikasi-dulu sebelum bangun.** Sumber tak bisa dites dari sandbox Claude (domain tak di-whitelist) → minta user paste 1 URL contoh di browser, atau gunakan web_search/web_fetch untuk konfirmasi kode seri SEBELUM menulis mapping besar.
7. **Penanda versi**: baris-1 `app.py` ada `# QF_BIAS_BUILD: ... (tanggal)`. User verifikasi di GitHub raw bahwa versi baru benar ter-deploy (sering ada cache/skew → reboot manual).
8. **Selalu kirim output 2 bentuk**: file individual (pecahan) + ZIP. Itu permintaan tetap user.

---

## 3. ARSITEKTUR SURPRISE YANG SUDAH ADA (REUSE — jangan bangun ulang)

Plumbing surprise UTUH dan menunggu diisi `actual`:

```
calendar_evt.get_calendar() → events[ {name, currency, ts_utc, status, impact, forecast, previous, actual=None} ]
   ↓  (LAYER BARU mengisi: actual, historical_std (σ), surprise_polarity)
app.main(): released_events = [e for e in events if status=="released" and actual is not None and historical_std is not None]
   ↓
macro.build_surprises(released_events) → surprises[ccy] = [{event, actual, forecast, z, ts_utc}]
        z = (actual − forecast)/σ , lalu × surprise_polarity   (polarity −1 utk unemployment/claims)
        kalau σ None → z = raw delta (directional saja, ditandai)
   ↓
scoring.score_R_hard(macro, asset): ambil surprise TERBARU, z_norm = clamp(z,±3)/3,
        × time-decay = exp(−umur_hari / _SURPRISE_DECAY_DAYS(=2.0))   ← "besok/lusa/3 hari beda"
        → blend dgn rate_diff → R_hard (bobot 0.60 di compute_asset_bias). Tertelusur di Detail Skor.
```

**Yang harus di-set oleh layer actual baru, per event:**
- `ev["actual"]` — nilai rilis, **unit sama dgn forecast faireconomy**.
- `ev["historical_std"]` — σ (volatilitas rilis historis). Tanpa ini → tidak menggerakkan skor (display-only). DENGAN ini → scored.
- `ev["surprise_polarity"]` — +1 normal, −1 terbalik (Unemployment Rate/Claims).
- `ev["actual_source"]` — untuk badge (mis. "Eurostat", "ONS").

**ALIGNMENT GUARD (WAJIB, sudah terbukti menyelamatkan):** sebelum menerima actual, cek
`previous` sumber ≈ `previous` kalender (toleransi relatif ~5%). Logikanya ada di
`indicators_us.py` lama: `implied_previous()` + `previous_aligned()` — SALIN ke layer baru.
Kalau tak align → tolak (jangan isi), tandai "misaligned" di diagnostik.

**Helper transform σ** (dari `indicators_us.py` lama, layak disalin): `compute_actual_and_sigma(vals, transform, scale)` dengan transform `level` / `mom_pct` / `diff`. `vals` = observasi DESC (terbaru dulu).

**Display di Risk Events** (`_render_row` di app.py) SUDAH siap: kalau event punya
`surprise_polarity`+`actual`+`forecast`, baris menampilkan "▲/▼ surprise → {ccy} bullish/bearish {source}".
Tidak perlu diubah — begitu layer baru men-set field itu, tag muncul otomatis.

---

## 4. MODUL A — ACTUAL via API RESMI LANGSUNG (gratis + real-time + aman IP datacenter)

Ganti FRED/DBnomics dengan API badan statistik resmi tiap negara. Semua `.gov`/`.europa.eu`/dll
→ tidak diblok IP datacenter, gratis, dan se-fresh penerbitnya (flash masuk cepat).
**Tiap negara = integrasi terpisah** (format beda). Bangun SATU dulu, buktikan, baru lanjut.

### Urutan disarankan & catatan per sumber
1. **EUR — Eurostat API** (paling tinggi dampak, mulai dari sini).
   - Base: `https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}?format=JSON&...`
   - Dataset HICP YoY: `prc_hicp_manr` (sudah dalam %, cocok "CPI Flash y/y"). Dimensi: `freq=M`, `unit=RCH_A`, `coicop=CP00` (all-items) / `TOT_X_NRG_FOOD` (core), `geo=EA20` (PERHATIAN: pakai `EA20` komposisi tetap, BUKAN `EA` chain — itu salah satu kandidat sumber selisih kemarin).
   - Format balikan = JSON-stat (perlu parsing `value` + `dimension` index — bukan list biasa). Ini kerja parsing tersendiri.
   - Fakta terverifikasi (rilis Eurostat): HICP YoY → Des25 2.0 · Jan26 1.7 · Feb26 1.9 · Mar26 2.6 · Apr26 3.0 · **Mei26 3.2 (flash)**. Pakai ini untuk MENGUJI parser-mu benar (harus keluar 3.2 untuk Mei, bukan 1.9).
   - Verifikasi cepat tanpa key: buka URL dataset di browser, cek observasi terbaru = 3.2.
   - Indikator EUR lain via Eurostat: `une_rt_m` (unemployment rate, polarity −1), retail `sts_trtu_m`, GDP `namq_10_gdp`.
2. **GBP — ONS** (`api.ons.gov.uk` / ONS Beta API). CPIH/CPI YoY, unemployment (LFS), retail sales.
3. **AUD — ABS** (Australian Bureau of Statistics Data API, SDMX-JSON). CPI (kuartalan!), unemployment, retail. Catatan: CPI Australia KUARTALAN, sesuaikan timing.
4. **JPY — e-Stat** (`api.e-stat.go.jp`, perlu app-id gratis) atau Stats Bureau. CPI, unemployment. Bahasa Jepang → hati-hati kode tabel.
5. **CAD — StatCan** (Web Data Service, `https://www150.statcan.gc.ca/t1/wds/rest/...`). CPI, employment.
6. **NZD — Stats NZ** / RBNZ. CHF — BFS (Swiss). Prioritas rendah (event lebih jarang).
7. **US (opsional, diskusikan dgn user)** — **BLS API** (`api.bls.gov/publicAPI/v2/`, key gratis) untuk CPI/NFP/unemployment/claims langsung dari sumber; ATAU pertahankan FRED khusus US (andal & timely). Jangan pakai DBnomics.

### Pola implementasi (mirror `indicators_us` lama, sumber diganti)
```
collectors/actuals_eurostat.py   ← BARU (mulai EUR)
  - EU_INDICATOR_MAP: {match_nama_event → dataset/dimensi/transform/polarity}
  - fetch_eurostat(dataset, dims) → observasi DESC (parse JSON-stat)
  - reuse compute_actual_and_sigma + previous_aligned + implied_previous (salin dari indicators_us lama)
  - enrich_eu_actuals(events): set actual+historical_std+polarity+actual_source, GUARD wajib
app.py main(): panggil enrich_eu_actuals SEBELUM membangun released_events
  - tampilkan status: berapa terisi / misaligned / gagal-resolve (jangan biarkan user menebak)
```

### Definition of done (per negara)
- Event high/med-impact ccy itu yang sudah released → `actual` terisi unit benar, lolos alignment guard, masuk surprise→R_hard, tertelusur di Detail Skor.
- Flash (bukan final lama) yang terambil. σ terhitung dari histori seri.
- Gagal/timeout = graceful (actual None, tidak crash). Status terlihat di UI.
- PMI (ISM/S&P Global) & sentimen (ZEW/Ifo) TETAP bolong — berlisensi, gratis tidak ada. Tandai jujur, jangan dipaksa.

---

## 5. MODUL B — INTEGRASI GROQ (setelah actual non-US berjalan)

Tujuan: menilai **dampak & arah news/event** dengan nuansa yang keyword-matching tak tangkap
(mis. "BoJ should slow bond buying" = hawkish JPY). **Juga membuka nilai RSS bank sentral primer**:
judul rilis resmi (Fed/ECB/BOJ/RBA) itu kering ("Monetary Policy Decision") → keyword classifier
menghasilkan nol; Groq baca body → ekstrak arah. (Sudah diverifikasi: engine SUDAH memetakan
boj→JPY/rba→AUD dst; yang kurang cuma ekstraksi arah dari teks kering → tugas Groq.)

### GARIS TEGAS (TIDAK BOLEH DILANGGAR)
- **Groq MENGUKUR** → klasifikasi arah (+/−/0), impact (low/med/high), magnitude, aset terdampak. Output **JSON terstruktur**, terukur, terverifikasi.
- **Engine MENGHITUNG** → ambil output Groq sebagai *input* (isi `direction`+`magnitude` di news_overlay, atau `z` di surprises), lalu kalikan bobot deterministik.
- Groq **TIDAK PERNAH**: menentukan poin bias langsung, meramal arah jangka pendek/menengah/panjang (= narasi; R_narrative korelasi ~nol per audit v3), atau jadi "hakim akhir".
- Melanggar = membangun ulang metavulus. Jangan.

### Prasyarat
- `GROQ_API_KEY` di Streamlit Secrets (user SUDAH punya). Model: `llama-3.3-70b-versatile` (function-calling, gratis, cukup pintar). Hindari model terlalu kecil.

### Kendala diketahui
- Free tier ~30 req/menit + kuota token harian. News ~80 headline/refresh → TIDAK BISA auto-classify semua. Harus selektif: (a) hanya headline lolos pra-filter (impact/currency match), atau (b) on-demand per-klik (tombol Groq sudah jadi placeholder di News Feed), atau (c) cache agresif + batch.

### Arsitektur
```
engine/groq_client.py   ← BARU
  - get_groq_client() pakai GROQ_API_KEY dari st.secrets
  - classify_news(headline) → {direction:{asset:+/−/0}, impact, magnitude, reasoning}
  - (tahap 2) read_cb_release(title+url|body) → arah hawkish/dovish utk RSS bank sentral primer
  - @st.cache_data(ttl) WAJIB; retry + graceful fallback ke keyword lama bila Groq down/limit
```
Integrasi:
1. **News classification** (utama): lengkapi `news_overlay` — Groq isi `direction`+`magnitude`, engine hitung `news_delta` (bobot tetap). Aktifkan tombol "🤖 Groq context" (sudah ada, disabled). Tambah filter News Feed by impact hasil Groq.
2. **Central bank primer**: setelah (1) oke, tambah RSS primer (ECB/BOE/BOJ/RBA) + Groq baca isinya (judul kering → arah). Ini titik di mana feed primer baru berguna.
3. **Surprise measurement** (opsional, tahap akhir): saat actual rilis, Groq ukur surprise kualitatif sebagai pelengkap z numerik — tapi z NUMERIK (dari Modul A) tetap yang menggerakkan skor.

### Definition of done
- News Feed: klasifikasi arah+impact dari Groq (bukan keyword), dengan filter impact. Engine tetap yang hitung poin (tertelusur di Detail Skor). Tidak crash saat limit/down (fallback keyword). Kuota tidak jebol (selektif/cache/on-demand).

---

## 6. PETA FILE (build ini)
```
app.py                       (main; actual-enrichment DIBUANG, penanda 2026-06-04)
engine/scoring.py            (R_hard + surprise z + time-decay + polarity — UTUH, siap diisi)
engine/news_overlay.py       (klasifikasi arah; peta currency+bank sentral lengkap)
engine/pairs.py, confidence.py, freshness.py
collectors/macro.py          (FRED RATES utk rate_diff + build_surprises dgn polarity — UTUH)
collectors/calendar_evt.py   (faireconomy; forecast+previous, actual selalu None)
collectors/cot.py            (CFTC; fix dedup-kolom)
collectors/news.py           (multi-source: FinancialJuice+Fed+ForexLive)
collectors/prices.py, retail.py(retail mati, diterima)
utils/timeutils.py, cache.py
config.py
```
**Tidak disertakan (sengaja dibuang):** `indicators_us.py`, `indicators_world.py`, `actuals_fmp.py`.
Salin logika guard/σ-nya bila perlu untuk Modul A, lalu bangun konektor API-resmi yang baru.

## 7. MASALAH TERBUKA LAIN
- News Feed kadang kosong bila feed diblok IP datacenter (FinancialJuice). Cek status sumber.
- Retail layer mati (Myfxbook/FXSSI/Dukascopy) — diterima v1.
- Semua bobot (R_hard 0.60/C 0.25/D 0.15, SCALE_FACTOR news=10, decay=2 hari) = PLACEHOLDER. Belum backtest. SCALE_FACTOR=10 bikin 2–3 news searah mentok cap ±30 (tumpul) — kalibrasi saat backtest.
