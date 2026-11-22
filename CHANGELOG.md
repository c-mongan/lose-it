# Changelog

## Unreleased

### Fixed

- Lose It! web redeploys no longer break the client. On `IncompatibleRemoteServiceException` the HTTP layer reads the live strong name + policy hash from the public GWT bundles, switches to them and resends once; unknown type signatures are filled from the live build's schemas (cached under `~/.cache/lose_it/`). `LOSEIT_AUTO_REFRESH=0` disables it. Types the parsers read by field position (FoodLogEntry, SearchResultFood, …) are pinned: if a redeploy reshapes one, decoding raises `SchemaChangedError` instead of returning silently empty data.
- Schema extraction no longer depends on per-build minified names and now recognizes both `Number(...)` readers (double and float), so types with float fields (e.g. `DailyLogEntryWithMeals`) carry all their fields. Bundled `_schemas.json` regenerated from build `11A17D1D`.
- `loseit delete` / `LoseIt.delete_entry` returned HTTP 500 for entries whose diary response carries no `DoXxxx` food identifier code — the empty string serialized as an empty token in the `deleteFoodLogEntry` envelope. Blank codes now fall back to `"AAAAAA"` (base64 zero-long), same placeholder trick as the DayDate fallback key. ([#78](https://github.com/phitoduck/lose-it/pull/78))
- Diary parser dropped entries past the first Timestamp-bearing one — `java.sql.Timestamp` consumes 2 tokens, not 1. `loseit diary --date 2025-07-15` → 0 entries despite 14 KB of wire data. Year-mining: 17 → 1,211 entries. ([#33](https://github.com/phitoduck/lose-it/pull/33))
- `loseit delete` always returned HTTP 500 — `entry_day_key` was scraped as `'Honey'`/`'Tomato'` instead of the base64 epoch-long from `Date.raw`. ([#33](https://github.com/phitoduck/lose-it/pull/33))
- Historical-date diary fetches returned HTTP 500 — `loseit diary --date 2025-06-15` sent `day_key=""` when not in init RPC's recent-day window. Fallback placeholder unblocks all dates. ([#33](https://github.com/phitoduck/lose-it/pull/33))

### Added

- `loseit build [--schemas-out FILE]` prints the live build IDs and optionally extracts its schemas; replaces `tools/extract_gwt_schemas.py`.
- `--serving-unit` accepts `tsp`, `can`, `container` (ord 1/21/45). ([#33](https://github.com/phitoduck/lose-it/pull/33))
