# PNRR AI Opportunity Monitor

Monitors schools funded under **D.M. 219/2025 – Avviso 73226/2026 – Snodi formativi sull'intelligenza artificiale** and alerts when one publishes a real, open call that an external AI-training firm could bid on (e.g. *avviso di selezione esperti/formatori*, *manifestazione di interesse*, *determina a contrarre*).

Instead of crawling every school's website, it identifies which **albo platform** each school uses and polls that platform's feed, diffing for new notices — so it scales to the full funded list and catches notices the day they appear.

## How it works

The process is split into two distinct phases:

#### 1. Mapping Pass (One-time / Weekly)
- **Input:** PDF list of funded schools
- **Resolve:** Finds the school's website (via *Scuola in Chiaro*)
- **Detect:** Identifies the platform and *albo ID*
- **Output:** Writes the `vendor` and `albo_id` to a CSV

#### 2. Monitor Run (e.g., Daily / Scheduled)
*For each mapped school:*
1. **Fetch:** `adapter.fetch_items()` *(1 light request)*
2. **Deduplicate:** Diff vs already-seen notices (`StateStore`)
3. **Prefilter:** Fast keyword check *(cheap)*
4. **Download:** Retrieve notice PDF(s)
5. **Rule Verification:** `OpportunityVerifier` *(checks rules)*
6. **AI Verification:** `AiVerifier` *(Gemini, optional; rule-based fallback when unavailable)*
7. **Alert & Save:** `AlertService` emails matches, reserves send state, and marks them as sent

### Platform coverage (adapters)
Schools cluster onto a few albo platforms; one adapter covers thousands of schools each.

| Adapter | Platform | Source |
|---|---|---|
| `ArgoAdapter` | Argo Albo Pretorio | public per-school RSS + JSON detail API |
| `SpaggiariAdapter` | Spaggiari Albo Online | Inertia `data-page` JSON embedded in the page |
| `NuvolaAdapter` | Nuvola / Madisoft | bacheca (MIUR-code-keyed), via crawler |
| `GenericCrawlerAdapter` | own-site (WordPress/AgID theme) | crawls the albo page, follows to the PDF |

Adding a platform = implement `AlboAdapter` and register it in `cli.build_registry` — nothing else changes.

## Project Structure

```text
pnrr-ai-monitor-poc/
├── data/
│   └── projects_sample.csv       # Seed dataset (populated by mapping pass)
├── scripts/
│   └── extract_ministry_pdf.py   # One-off script to parse the initial ministry PDF
├── src/
│   └── pnrr_ai_monitor/
│       ├── adapters/             # Platform-specific scraping logic
│       ├── alerts.py             # Email notification service
│       ├── cli.py                # Command-line interface definitions
│       ├── config.py             # Environment variables and settings
│       ├── crawler.py            # Generic crawler utilities
│       ├── document_reader.py    # Text extraction from PDF/HTML
│       ├── http_client.py        # Centralized HTTP request management
│       ├── mapping.py            # Discovers albo platform & ID for each school
│       ├── models.py             # Core data classes (Pydantic/Dataclasses)
│       ├── monitor.py            # Main orchestration loop
│       ├── repository.py         # Database/Persistence abstraction
│       ├── state.py              # SQLite/Postgres state store and dedup memory
│       └── verifier.py           # Validates notices (rules + AI)
├── pyproject.toml / uv.lock      # Python dependencies
├── run_monitor.py                # Main entrypoint script
└── README.md                     # You are here!
```

## Components

All core logic resides in `src/pnrr_ai_monitor/`:

- **`mapping.py` (`SchoolMapper`)** — resolves each school to `(vendor, albo_id, albo_url)`.
- **`adapters/`** — `AlboAdapter` interface + `AdapterRegistry` + the four adapters.
- **`document_reader.py`** — extracts text from HTML/PDF (reads full PDFs; OCR hook for scans).
- **`verifier.py`** — `OpportunityPrefilter` (cheap title gate), `OpportunityVerifier` (rules: exact CUP/CLP, call language, rejects closed/internal acts), `AiVerifier` (Gemini judge: open-vs-closed, internal-vs-external).
- **`state.py`** — `StateStore` (dedup of processed notices + sent alerts); SQLite locally, Postgres via `DATABASE_URL` in CI/production. Postgres operations reconnect and retry on dropped connections.
- **`alerts.py`** — sends Italian email for not-yet-alerted opportunities, including clearly marked unconfirmed rule-based alerts when AI is unavailable.
- **`monitor.py`** — orchestrates one polling pass and reports `RunStats`.

## Setup

Requires Python ≥ 3.10. Uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # install dependencies from pyproject.toml / uv.lock
cp .env.example .env     # then fill in real values (see below)
```

### `.env`
```
SENDER_EMAIL=...            # Gmail address
EMAIL_PASSWORD=...          # Gmail APP PASSWORD (not your login password)
RECEIVER_EMAIL=...          # where alerts go
GEMINI_API_KEY=...          # optional; enables the AI judge
AI_VERIFICATION_REQUIRED=true
DATABASE_URL=               # empty = local SQLite; set for Postgres in GitHub Actions/production
```
`.env` is gitignored — never commit real keys. (`.env.example` holds placeholders only.)

## Usage

```bash
# Examples / testing — uses the small data/projects_sample.csv by default
python run_monitor.py --map               # fill vendor/albo_id (add --refresh to re-map)
python run_monitor.py --dry-run           # poll everything; no emails, no state writes
python run_monitor.py                      # live: sends email + persists state

# The real thing — full funded list
python scripts/extract_ministry_pdf.py                              # PDF -> data/projects_funded.csv (~4,311)
python run_monitor.py --map --projects data/projects_funded.csv     # map them all (one-time)
python run_monitor.py       --projects data/projects_funded.csv     # monitor them all
```

`--projects FILE` chooses which schools CSV to run; omit it to use the sample.

```bash
# Archive of opportunities (incl. expired), tagged aperta/scaduta/sconosciuta
python run_monitor.py --export-opportunities data/opportunities.csv
```

### Options
| Flag | Meaning |
|---|---|
| `--dry-run` | Do not send email or persist state (repeatable for testing). |
| `--limit N` | Only process the first N schools. |
| `--map` / `--refresh` | Run the mapping pass (and re-map already-mapped rows). |
| `--ai-mode {off,capped,full}` | `off`: rules only (free). `capped`: AI on up to `--ai-budget` notices (free-tier safe). `full`: AI on every candidate (needs paid Gemini quota). Default: `capped`. |
| `--ai-budget N` | Max AI calls per run in `capped` mode (default 500). |

Each run logs to `run_log.txt`; the summary line reports
`schools / new items / passed prefilter / confirmed / alerts / ai_failures`.

## Data

There are two schools CSVs: `data/projects_sample.csv` (small example set, used by default) and `data/projects_funded.csv` (the real ~4,311-school list, generated from the ministry PDF by `scripts/extract_ministry_pdf.py`). Both share the same columns:

**Base Information (from PDF):**
* `progressive`, `area`, `school_code`, `school_name`, `region`, `school_type`, `cup`, `clp`, `amount`, `website`

**Generated Data (filled by mapping pass):**
* `vendor`: The identified albo platform provider
* `albo_id`: The unique identifier for the school on that platform
* `albo_url`: The direct URL to the school's albo page

## Notes & limits

- **AI quota:** the free Gemini tier is small. Use `--ai-mode off` (rules only, still FP-guarded) or `capped` to stay free; use a paid key + `full` for whole-list coverage. If AI is unavailable, non-high rule matches can still be emailed as clearly marked "NON VERIFICATO DA AI" alerts so the POC does not fail silently.
- **Mapping gaps:** ~20% of schools may not auto-resolve (no registered website / site down) and need manual `albo_id` entry.
- **Persistence:** local SQLite is fine for development. In GitHub Actions, use Postgres through `DATABASE_URL`; committing SQLite/CSV state back to Git is fragile and can create push conflicts. The Postgres store retries short dropped-connection failures before surfacing a real outage.
- **Email dedup reliability:** alerts are reserved before SMTP send (`sending`), marked after Gmail accepts them (`sent`), and released on send failure (`failed`). This reduces duplicate emails if a run dies around the send step.
- **Scheduling:** a GitHub Actions workflow can run it in the cloud (set secrets there). For the current POC, daily scheduling is usually enough; run more frequently only if you need faster discovery.
