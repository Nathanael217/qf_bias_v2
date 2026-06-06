# qf_bias — FIX yang SUDAH diterapkan (JANGAN di-undo)

Tanggal: 2026-06. Tiga fix struktural sudah masuk ke kode. Siapa pun yang
mengedit berikutnya WAJIB mempertahankan ini.

## FIX 1 — R_hard = carry murni (hapus double-count surprise)
- File: `engine/scoring.py`, fungsi `score_R_hard`.
- SEBELUM: R_hard = blend `0.60×z_surprise + 0.40×rate_diff`. Komponen z-surprise
  berasal dari rilis ekonomi yang SAMA yang juga men-drive faktor F → satu kejutan
  dihitung dua kali (di R_hard dan di F).
- SESUDAH: R_hard = `_RHARD_CARRY_SCALE (0.40) × diff_norm` — rate differential MURNI.
  Komponen z-surprise DIHAPUS total dari R_hard. Magnitude carry standalone TIDAK
  berubah (mis. EUR carry-only tetap ≈ −18, bukan melonjak).
- Konstanta lama (`_RHARD_Z_WEIGHT`, `_RHARD_Z_CLAMP`, `_SURPRISE_DECAY_DAYS`,
  `_RHARD_DIFF_WEIGHT`) DIHAPUS; diganti `_RHARD_CARRY_SCALE = 0.40`.
- `macro["surprises"]` kini TIDAK dipakai scoring (boleh tetap dibangun utk display).

## FIX 2 — Agregasi F: max-per-timestamp (bukan sum)
- File: `engine/ff_surprise.py`.
- SEBELUM: `compute_ff_surprise` menjumlah points semua event per currency
  (`agg[ccy] += points`) → rilis berkorelasi di jam yang SAMA (NFP+AHE+Unemployment)
  dihitung 2–3×.
- SESUDAH: helper `_aggregate_scored` mengambil |points| TERBESAR per (currency,
  timestamp), lalu menjumlah antar timestamp BERBEDA. Konsisten dgn konvensi max-|z|
  di score_R_hard lama. Berlaku untuk kedua jalur (scrape FF & kalender).

## FIX 3 — Sumber F: FF-scrape PRIMARY, kalender FALLBACK
- File: `app.py` (blok build ff_scores) + `engine/ff_surprise.py`.
- Realita: feed faireconomy TIDAK mengirim `actual` (hanya forecast/previous).
  Actual datang dari: scrape ForexFactory (pb_ff_data), Eurostat (EUR), manual/Groq vision.
- Wiring final:
  - PRIMARY  = `compute_ff_surprise(pb_ff_data)` — sumber actual utama (scrape FF).
  - FALLBACK = `compute_ff_surprise_from_calendar(released_events)` — HANYA bila
    pb_ff_data kosong. Either/or, tidak digabung → tak ada double-count antar sumber.
- Fungsi BARU di ff_surprise.py: `_aggregate_scored`, `_days_ago_from_iso`,
  `_score_calendar_event`, `compute_ff_surprise_from_calendar`.
- Komentar lama yang KELIRU ("calendar_evt sudah parse actual dari faireconomy")
  sudah dikoreksi di app.py.

## Invarian yang harus dijaga
- R_hard TIDAK boleh berisi komponen surprise/z lagi.
- F TIDAK boleh kembali ke agregasi sum.
- Surprise hidup HANYA di F (satu rumah). Carry hidup HANYA di R_hard.
- Tidak ada double-count: satu rilis ekonomi → satu faktor (F).
