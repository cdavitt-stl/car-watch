# Car Watch

Twice-daily used-car monitor for a Honda Civic / Toyota Corolla within 30 miles
of ZIP 63125, budget up to $24,000, optimizing for lowest miles / best deal.
Runs on GitHub Actions; output lands in a Google Sheet.

## Architecture
- `car_watch.py` — single script: fetch → dedupe (SQLite by VIN) → deal-score →
  write two tabs of a Google Sheet. No CSV, no HTML, no email.
- `config.yaml` — all search parameters. No secrets in this file.
- `seen.db` — SQLite history of every VIN ever seen, with first/last price.
  **Committed to the repo on purpose** (see below).
- `.github/workflows/car-watch.yml` — schedule + run + commit `seen.db`.
- Data source: MarketCheck `/v2/search/car/active`.

### Secrets (GitHub repo secrets → env vars)
| Env var | Purpose |
|---|---|
| `MARKETCHECK_API_KEY` | MarketCheck API key, sent as the `api_key` query param |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Entire service-account JSON file, as a raw string |
| `SHEET_ID` | Spreadsheet key of the target Google Sheet |

The service account has no access to your Drive by default — **share the Sheet
with the service account's `client_email` as an Editor**, or every run fails
with a 403.

### Google Sheet layout
- **`Current`** — cleared and fully rewritten each run, sorted by deal score
  (highest first). Columns: `Score | Year | Model | Trim | Price | Miles |
  Days Listed | Dealer | Distance | Status | Link | Flags`.
- **`Log`** — append-only, one row per run:
  `Timestamp (UTC) | Listings | New | Price Drops`.

Both tabs are created automatically if missing. Numbers are written as numbers
(not preformatted strings) so the Sheet can sort and filter on them.

### Status column
`NEW`, `PRICE DROP`, or `—`. Cars priced under `min_price_flag` still appear and
are additionally marked `CHEAP — CHECK HISTORY`. A car can be both, in which
case the marks are joined — e.g. `NEW · CHEAP — CHECK HISTORY` — so a cheap car
never hides the fact that it's also brand new to the list.

### Dealer column
Dealer names are matched case-insensitively as substrings against
`preferred_dealers` in config.yaml — `"Suntrup"` matches
`"Suntrup Ford Westport"`. Matching rows get a `★ ` prefix on the cell.

### Flags column
Listings are matched against the `watchlist` in config.yaml (make, model,
year range, optional `trim_keywords`, `severity`, `reason`) and annotated
`AVOID: reason` or `CAUTION: reason`. Flagging is advisory only — flagged cars
are never removed and the sort is unaffected.

Omitting `trim_keywords` matches all trims. A listing with a missing trim or an
unparseable year fails any rule that depends on that field rather than raising,
so we never flag a car we can't actually identify. When several rules hit at
once (a 2016 Civic EX-T hits both Civic rules), the worst severity names the
cell and the reasons are joined with `; `.

### Scheduling and persistence
Runs at 13:00 and 21:30 UTC daily, plus `workflow_dispatch` for manual runs.
The workflow needs `permissions: contents: write` because Actions runners are
wiped after every run — `seen.db` is committed back so VIN dedupe history
survives. A `concurrency` group serializes runs, and the push retries with
`--rebase` if something else landed first.

Note: GitHub's scheduled triggers are best-effort and can be delayed by several
minutes (occasionally longer) under load. Nothing here depends on exact timing.

## Deal score
(median_price − price)/1000 + (median_miles − miles)/10000, computed against
the current result set. Transparent and explainable — do not replace with an
opaque model without asking.

Because the medians are computed over the fetched cohort, `fetch_listings()`
pages through results rather than taking a single 50-row slice; a truncated
cohort would bias both medians and therefore every score.

## MarketCheck schema verification
Checked against
[docs.marketcheck.com › Cars › Inventory Search](https://docs.marketcheck.com/docs/api/cars/inventory/inventory-search)
before finalizing `fetch_listings()`.

**Changed:**
- **Host** `mc-api.marketcheck.com` → `api.marketcheck.com`. The docs give the
  endpoint as `GET https://api.marketcheck.com/v2/search/car/active`. The docs
  make no mention of `mc-api.marketcheck.com`; I don't know whether it still
  works as a legacy alias, so this now matches what is documented.
- **Pagination added** (`num_found` is a documented response field; `rows` is
  documented as max 50, default 10). Not a field-name fix — see the deal-score
  note above for why it matters.

**Verified unchanged** — every request param and response field the script
already used appears in the docs for this endpoint, spelled as written:
- Request: `api_key`, `make`, `model`, `zip`, `radius`, `price_range`,
  `miles_range`, `car_type`, `sort_by`, `sort_order`, `rows`, `start`.
  `car_type: "used"` is a documented value (`new` / `used` / `certified`), and
  `sort_by: "miles"` is a documented sort key.
- Response: `listings`, `num_found`, `vin`, `price`, `miles`, `dom`, `dist`,
  `vdp_url`; `build.{year, make, model, trim}`; `dealer.{name, city}`.

**Not verified / unknown:**
- Whether `dist` is populated when no `zip`/`radius` is supplied. We always
  send both, so it doesn't affect us.
- `dom` vs. the also-documented `dom_active` / `dom_180`. The script keeps
  `dom`; the docs list all three but I did not confirm which best matches
  "days this listing has been up." If the Days Listed column looks wrong,
  that's the first thing to check.
- Whether `trim` is always present. The script already coerces a missing or
  null trim to `""`.

## Instructions for Claude Code
- Re-verify MarketCheck request/response field names against
  https://docs.marketcheck.com before changing `fetch_listings()`. If you don't
  know a field name, say so and check the docs; don't guess.
- Keep dependencies light: `requests` + `pyyaml` + `gspread` only.
- `seen.db` **is** committed here — it's the persistence layer. Never commit
  `.env`, service-account JSON, or anything else containing keys; `.gitignore`
  covers the common filenames.
