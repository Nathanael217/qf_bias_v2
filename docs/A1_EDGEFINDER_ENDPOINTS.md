# A1 Trading EdgeFinder — parse.bot scraper (9 endpoints)

Scraper ID: `57dca8fa-f379-42fd-9230-109de0cac79b` (public fork of canonical a1trading.com API)
Base: `https://api.parse.bot/scraper/57dca8fa-f379-42fd-9230-109de0cac79b/<endpoint>` · header `X-API-Key`
Source: https://www.a1trading.com/free-data-dashboards/ · ~1 credit/call (verifikasi; antibot bisa lebih)

## Catalog + status wiring ke engine qf_bias

| # | Endpoint | Param | Bentuk | Peta engine | Status |
|---|----------|-------|--------|-------------|--------|
| 1 | get_retail_sentiment | category (opsional) | data.pairs[] {pair,long_percentage,short_percentage,signal} | **faktor D** (retail kontrarian) | ✅ WIRE — pakai long% MENTAH; ada entri per-CURRENCY (US-DOLLAR/EURO/JP-YEN/GB-POUND/CH-FRANC/AU-DOLLAR/CA-DOLLAR/NZ-DOLLAR) → langsung per-mata-uang. JANGAN impor "signal" (itu verdict A1). |
| 2 | get_cot_report | — | data.assets[] {asset,non_commercial_long_pct,non_commercial_short_pct,net_position_pct} | **faktor C** (COT smart money) | ⚠ OVERLAP dgn collector CFTC yg sudah ada. PILIH SATU sumber, jangan double-count. |
| 3 | get_currency_heatmap | — | data.pairs[] {pair,base_currency,quote_currency,change_pct,direction} | **lensa price-strength TERPISAH** | ❌ BUKAN faktor bias (ini price/TA). Pakai utk divergensi bias-vs-harga. |
| 4 | get_interest_rates | — | data.rates[] {currency,current_rate,previous_rate,...} | R_hard rate_diff | 🚨 RUSAK — nilai IDENTIK dgn CPI (lihat #6). Endpoint salah scrape tabel CPI. JANGAN WIRE sampai diperbaiki. Pakai FRED dulu. |
| 5 | get_economic_growth | — | data.countries[] {currency,latest_gdp_growth,previous_gdp_growth,change,trend} | fundamental konteks | 🔶 Jangan auto-wire. Calon sub-driver/real-growth → backtest dulu. |
| 6 | get_inflation_data | — | data.countries[] {currency,current_cpi,previous_cpi,change,trend} | fundamental; real-rate (rate−CPI) | 🔶 Solid. Real-rate butuh rates asli (#4 rusak) → tunda. |
| 7 | get_aaii_sentiment | — | data.surveys[]+latest {bullish_pct,neutral_pct,bearish_pct,bull_bear_spread} | risk-on/off EKUITAS AS | ❌ BUKAN forex per-pair. Sentimen ritel saham AS. Overlay risk makro opsional, bukan faktor bias forex. |
| 8 | get_labor_market | — | data.countries[] {currency,unemployment_rate,previous_rate,change} | fundamental konteks | 🔶 Display/konteks. Tidak auto-wire. |
| 9 | get_housing_data | — | data.indicators[] (HPI, inventory, mortgage apps, housing prices QoQ/1y/5y/...) | konteks makro | 🔶 Relevansi bias rendah. Display saja. |

## Disiplin (LOCKED)
- Sumber MENGUKUR, engine MENGHITUNG. Jangan impor verdict A1 (signal/EdgeFinder score) sebagai bias.
- Bobot D/C tetap PLACEHOLDER sampai backtest.
- Heatmap = price/TA → tidak masuk skor bias (langgar "confluence di atas TA").
- aaii = ekuitas AS, bukan forex.
- get_interest_rates RUSAK (=CPI) → guard otomatis `rates_look_like_cpi()` menolaknya.

## Mekanisme koneksi (hemat credit)
Click-to-run → simpan session_state → engine baca dari session_state kalau ada (else fallback FRED/CFTC).
Credit hanya kebakar saat klik. Cache TTL: sentiment 1j, COT/macro 6-24j.

## Snapshot data (2026-06-04, utk referensi)
- Retail per-ccy long%: USD 82.4 (Bearish), JPY 78 (Bearish), CAD 75, NZD 71, GBP 52, EUR 45, AUD 42, CHF 27 (Bullish).
- COT non-comm net%: CAD -14.8, DOW -9.3, GBP -6.7, AUD -4.4, ZAR -4.3; CHF +2.6, NASDAQ +3.3.
- Rates (RUSAK=CPI): USD 3.8 EUR 3.2 GBP 2.8 JPY 1.4 AUD 4.2 CAD 2.8 CHF 0.6 NZD 3.1.
- GDP: USD 1.6↓ EUR 0.1 GBP 0.6↑ JPY 0.5 AUD 0.3↓ CAD 0 CHF 0.7↑ NZD 0.2.
- AAII latest: bull 36.3 / bear 37.0 / spread -0.7.
