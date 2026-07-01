# PNRR AI Opportunity Monitor — How It Works (Plain-Language Guide)

This guide explains, end to end, what this tool does and how each part works. It is written so you can read it and explain it to your team, with every piece of jargon defined.

---

## 1. The problem we're solving

Italy funds public schools, under a programme called **PNRR – D.M. 219/2025** (more on that below), to run **AI / digital-skills training**. To actually deliver that training, each funded school must publicly **post a call** ("we're looking for an external AI trainer / expert / tutor") and let companies or individuals apply.

Your firm wants to win those contracts. The manual way is to visit each funded school's website every morning and look for a new call. There are **~4,300 funded schools**, and each call is only open for a short window (often 7–15 days). Checking them by hand doesn't scale, and you miss things.

**This tool automates that watch.** It knows the funded schools, checks where each one publishes its notices, and emails you the moment a real, biddable AI-training call appears.

---

## 2. Jargon glossary (read this first)

**PNRR** — *Piano Nazionale di Ripresa e Resilienza*, Italy's EU-funded national recovery plan. A big pot of money with many funding lines.

**D.M. 219/2025 / Avviso 73226/2026 / "Snodi formativi sull'intelligenza artificiale"** — the specific funding decree and notice under which these schools got money to run **AI training hubs** ("snodi formativi" = training hubs/nodes). When you see these strings in a document, it strongly signals the notice is about *this* programme.

**CUP** — *Codice Unico di Progetto*. A unique national project ID (15 characters, e.g. `C34D25003300006`). Every funded project has one. If a school's notice contains *its* CUP, the notice is almost certainly about that exact funded project — a very strong match signal.

**CLP** — the project's code in the PNRR system (e.g. `M4C1I2.1-2026-1745-P-64381`). Another exact identifier, like the CUP.

**Codice meccanografico (mechanographic code / school code)** — the Ministry's unique ID for each school (10 characters, e.g. `CSIS049007`). It's how the official registries identify a school.

**Albo Pretorio (online)** — the **legal public noticeboard** every Italian public body (including schools) must maintain online. By law, official notices — calls for tender, selection notices, determinations — are published here. This is the authoritative place our tool watches.

**Amministrazione Trasparente** — a second mandated section of every school site ("Transparent Administration"), including a "Bandi di gara e contratti" (tenders & contracts) area. Sometimes calls live here too.

**Bando / Avviso / Selezione / Determina / Manifestazione di interesse** — Italian for the kinds of acts a school posts:
- *Avviso / Bando di selezione* = a call inviting people to apply.
- *Manifestazione di interesse* = a market consultation inviting companies to express interest.
- *Determina (a contrarre)* = an administrative decision to start a procurement.
- *Affidamento diretto* = a direct award (often already decided — usually **not** biddable).

**Esperto / Formatore / Tutor** — the roles schools recruit: expert / trainer / tutor. We care about ones open to **external** people ("esperto esterno"), not internal-staff-only ones ("personale interno", "docenti interni").

**Argo / Spaggiari / Nuvola (Madisoft) / Axios** — these are **software companies** that provide the albo/registry systems Italian schools use. A school doesn't usually build its own noticeboard; it rents one from one of these vendors. So "the school uses Argo" means "its public noticeboard is hosted on Argo's platform." Knowing the vendor tells us *how* to read that school's notices.

**Scuola in Chiaro** — the Ministry's official public school directory (`cercalatuascuola.istruzione.it`). Given a school code, it tells us the school's official website. We use it to look up websites reliably.

**Adapter** — a small piece of our code that knows how to read **one** vendor's platform (one adapter for Argo, one for Spaggiari, etc.). The rest of the system doesn't care which vendor a school uses; it just asks the right adapter.

**RSS feed** — a standardized, machine-readable list of "latest items" that many websites publish (originally for news). Argo publishes each school's notices as an RSS feed, which is the cleanest possible source for us.

**JSON / JSON API** — a standard text format for structured data, and a web address that returns it. Easier and more reliable to read than scraping a human web page.

**Scraping / crawling** — reading a normal web page (HTML) and extracting information from it, because there's no clean feed.

**Hydrate** — our term for "download the full document text of a notice" (e.g. open the attached PDF and read it), as opposed to just having its title.

**Prefilter** — a cheap, fast first check (on the title only) to skip obviously-irrelevant notices before doing expensive work.

**False positive (FP) / False negative (FN)** — FP = we alert on something that isn't a real opportunity (noise). FN = we miss a real opportunity. The whole design fights both.

**Dedup (deduplication)** — making sure we never alert twice for the same notice.

---

## 3. The big picture: two phases

The system runs in **two separate phases**. This separation is the key design idea.

### Phase A — Mapping (run occasionally: once, then maybe weekly)
**Goal:** for every school, figure out *which vendor platform* it uses and *what its ID is on that platform*, and record it.

Think of it as building an address book: "School X → its noticeboard is on Argo, ID `SG29221`." This is slow-ish (it visits each school's site once) but you only do it occasionally.

### Phase B — Monitoring (run often: e.g. hourly)
**Goal:** using the address book, quickly check each school's noticeboard for **new** notices, judge them, and email you the real opportunities.

This is fast per school (usually one web request) because the mapping already told it exactly where to look.

Why split them? Because checking 4,300 noticeboards every hour is only feasible if each check is one quick request. The slow "figure out where to look" work is done once, up front, not every hour.

---

## 4. Where the school list comes from (and how to monitor all ~4,300)

The monitor reads its to-do list from one file: **`data/projects_sample.csv`**. Whatever rows are in that file are the schools it checks — no more, no less.

- Today that file holds a **small curated sample (13 schools)** for testing.
- The **full funded list (~4,311 schools)** lives in the ministry PDF. The script **`scripts/extract_ministry_pdf.py`** reads that PDF and produces a CSV of every funded school.

There are **two CSVs**, on purpose:
- `data/projects_sample.csv` — a small example set (kept for tests/demos).
- `data/projects_funded.csv` — the **real** full list, produced from the PDF.

So to monitor everyone, the steps are:
1. `python scripts/extract_ministry_pdf.py` → writes all ~4,311 schools to `data/projects_funded.csv`.
2. `python run_monitor.py --map --projects data/projects_funded.csv` → fills in vendor + albo ID for each (Phase A).
3. `python run_monitor.py --projects data/projects_funded.csv` → checks them all (Phase B).

The `--projects` flag chooses which file to run; with no flag it uses the sample. So examples stay on the sample, and the real thing runs on the funded list — no manual file shuffling.

**The monitor itself does not "discover" the 4,311 — it simply loops over the rows of whichever CSV you point it at.** The CSV is the single source of truth.

---

## 5. What each file does, and how

All application code is in `src/pnrr_ai_monitor/`. Here's each piece in plain terms.

### `run_monitor.py` (entry point)
The script you actually run. It just hands off to the command-line interface in `cli.py`. (`python run_monitor.py …`)

### `cli.py` — the control panel
Reads your command-line options (`--map`, `--dry-run`, `--ai-mode`, etc.), builds all the components, and starts the right phase. It's where the four adapters are "registered" (listed) so the monitor knows about them. To add a new vendor later, you add one line here.

### `config.py` — settings
Loads settings from the `.env` file (your email login, optional AI key, etc.) into one tidy `Settings` object. Also parses the recipient lists (To/Cc/Bcc, comma- or semicolon-separated).

### `models.py` — the shared vocabulary
Defines the simple data structures everything passes around:
- `ProjectRecord` = one school row from the CSV (code, name, CUP, CLP, vendor, albo_id…).
- `AlboItem` = one published notice, in a **standard shape** regardless of which vendor it came from (title, link, date, category, PDF link). This is what lets the rest of the system ignore vendor differences.
- `VerificationResult` = the verdict on a notice (match? confidence? reason? deadline?).
- `Alert` = a confirmed opportunity ready to email.

### `http_client.py` — the polite web requester
A single shared object for making web requests. It reuses connections (fast), identifies itself honestly, and **automatically retries with backoff** if a site is briefly busy or rate-limits us. Centralizing this means we never accidentally hammer a server (a lesson learned the hard way during testing).

### `mapping.py` (`SchoolMapper`) — Phase A engine
Builds the address book. For each school:
1. **Find the website:** asks *Scuola in Chiaro* (`cercalatuascuola.istruzione.it/.../{code}/scheda`) for the school's official site. This is a reliable, official source — better than guessing or using a search engine.
2. **Detect the vendor:** downloads the school's homepage and looks for tell-tale signatures:
   - a link to `albipretorionline.com/...` → **Argo** (and the ID is in that link),
   - a Spaggiari `custcode` embedded in the page → **Spaggiari**,
   - a `nuvola.madisoft.it` link → **Nuvola** (its ID is just the school code),
   - otherwise it looks for the school's own "albo-online" page and treats it as a **Generic** (own-site) case.
   - It even follows the school's own "/albo-online" page one level deeper, because some schools (like Fauser) embed a vendor's system inside their own site.
3. **Write it down:** saves `vendor`, `albo_id`, `albo_url` back into the CSV.

Run via `python run_monitor.py --map`.

### `adapters/` — the per-vendor readers (Phase B data sources)
Each adapter knows how to (a) **list** a school's current notices and (b) **fetch the full text** of one. They all return the same `AlboItem` shape.

- **`base.py`** — defines the common `AlboAdapter` interface every adapter follows, plus the `AdapterRegistry` (a simple lookup: vendor name → the adapter that handles it).
- **`argo.py` (`ArgoAdapter`)** — the easiest, cleanest source. Argo publishes a **public RSS feed** per school at a fixed web address. The adapter downloads that feed, reads the list of notices from the XML, and (when needed) calls Argo's JSON detail endpoint to get the notice's attached PDFs and read them. *(Argo = the vendor; RSS = the machine-readable list it hands us.)*
- **`spaggiari.py` (`SpaggiariAdapter`)** — Spaggiari's page is a modern web app that **embeds all its data as JSON inside the page** (in a `data-page` attribute, an "Inertia.js" pattern). The adapter pulls that JSON out, reads the list of documents (each with its publish date, category like "Bandi di gara", and a direct download link), and reads the PDF.
- **`nuvola.py` (`NuvolaAdapter`)** — Nuvola's noticeboard ("bacheca") has no clean feed, but its web address is keyed directly by the school's mechanographic code, so we always know the URL. The adapter drives the generic crawler over it.
- **`generic.py` (`GenericCrawlerAdapter`)** — the fallback for schools whose notices live on their own website (often a standard WordPress "school" theme) rather than a vendor platform. It crawls the school's albo page, follows links to the actual notice PDFs, and reads them.
- **`search.py` + `search/SearchFallbackAdapter`** — the *last* resort, for schools the mapper couldn't place anywhere (no resolvable website / undetectable albo, ~1 in 5). Instead of polling a feed, it asks a search engine for the school's calls, **bounded between the school's funding date (from the PDF) and today** (a call can't predate funding), and feeds whatever it finds into the normal verify/AI pipeline. It uses a **pluggable `SearchProvider`** (DuckDuckGo today; built so Google Custom Search — which supports true date ranges and is more reliable — can be dropped in), and is **budget-capped** (`--search-budget`, default 30 searches/run) because web search rate-limits and doesn't scale to thousands of schools hourly. Best suited to a watchlist.

### `crawler.py` — generic crawling utilities
The reusable web-crawling logic the Generic and Nuvola adapters lean on: visit a page, find the promising links (ones mentioning "bandi", "albo", "avvisi"…), and collect candidate notices. It stays on the same website and avoids junk links.

### `document_reader.py` — text extraction
Turns a downloaded file into plain text the system can analyze. Handles HTML pages and PDFs, reads the **whole** PDF (not just the first page, so deep details like the CUP aren't missed), and has a hook for OCR (reading scanned/image PDFs) for the future.

### `verifier.py` — the judgment funnel (the brains)
This decides whether a notice is a real, biddable opportunity. It has three layers, cheapest first:

1. **`OpportunityPrefilter`** — a fast keyword check on the **title only**. If the title doesn't even hint at a selection/expert/PNRR/AI theme, we skip it — no download, no AI. This is purely to save time and cost.
2. **`OpportunityVerifier`** — rule-based check on the full text:
   - Does it contain the school's **exact CUP/CLP**? → very strong match.
   - Does it contain real **call language** ("avviso", "selezione", "manifestazione di interesse"…)?
   - It **rejects** acts that are clearly *not* opportunities even if they mention the project: already-awarded ("aggiudicazione", "graduatoria"), administrative ("nomina commissione"), publicity ("disseminazione"), or **internal-staff-only** ("personale interno"). This rule layer alone removes most noise.
3. **`AiVerifier`** — an optional second opinion from Google's **Gemini** AI model. It reads the full notice and decides the subtle things rules can't: is the call **currently open vs already closed**, and is it **open to external** firms vs internal staff only? It replies in Italian with the opportunity type, the deadline, and a one-sentence reason. This is the biggest lever for cutting both false positives and false negatives — but it needs an API key/quota.

### `state.py` (`StateStore`) — the memory
Remembers what we've already handled so we don't repeat ourselves:
- **processed** notices → don't re-download/re-judge them every hour,
- **alerted** notices → never email the same opportunity twice,
- the **opportunities archive** → a permanent, browsable record of every confirmed opportunity (school, CUP/CLP, title, **link**, published/deadline, type, confidence, when first/last seen). This is what lets you keep a log of past/expired calls. Export it any time with `python run_monitor.py --export-opportunities data/opportunities.csv` — each row is tagged **aperta / scaduta / scadenza sconosciuta** (open / expired / unknown), computed from its deadline.

Locally this is a small SQLite database file (`monitor_history.sqlite3`). It's built behind an interface so it can later be swapped for a cloud database (Postgres) without changing anything else.

### `repository.py` (`ProjectRepository`) — reads the CSV
Loads the school rows from `data/projects_sample.csv` into `ProjectRecord` objects for the monitor to iterate.

### `alerts.py` (`AlertService`) — the email
Builds and sends the alert email. It produces a proper **Italian** message in two formats (a nicely formatted HTML version with a "Vedi avviso" button, plus a plain-text fallback), and supports multiple recipients via To/Cc/Bcc. It only sends for confirmed, not-yet-alerted opportunities. **Notices are grouped by CUP**: if a single run finds several new notices for the same project, they go out as **one email** (the school/CUP header once, then each notice listed with its own link/deadline/reason) — so one project never floods you with separate messages.

### `monitor.py` (`Monitor`) — the conductor (Phase B engine)
Ties it all together for one run. For each mapped school it:
1. picks the right adapter,
2. fetches the current notices,
3. drops ones we've already processed (dedup),
4. prefilters the rest by title,
5. downloads ("hydrates") the survivors,
6. runs the rule verifier, then the AI verifier,
7. emails confirmed opportunities and records them,
and prints a summary line (`RunStats`) at the end. It's built so that one broken school can never crash the whole run.

### `scripts/extract_ministry_pdf.py` — the one-off list builder
Separate from the live system. It opens the ministry PDF, uses pattern-matching to pull out each funded school's row (progressive number, area, school code, name, region, type, CUP, CLP, amount), and writes all ~4,311 of them to **`data/projects_funded.csv`** in the exact columns the monitor expects (with `vendor`/`albo_id`/`albo_url` left blank for the mapping pass to fill). You run this once to create the master list; the monitor then runs against it via `--projects data/projects_funded.csv`.

---

## 6. The verification funnel, visualized

A notice has to survive every stage to trigger an email:

```
All notices on a school's board
        │  (dedup: drop ones already seen)
        ▼
   New notices
        │  (prefilter: title hints at expert/AI/PNRR?)        ← cheap, no download
        ▼
   Plausible notices
        │  (download full PDF text — "hydrate")
        ▼
   Rule check: exact CUP/CLP or call language?               ← removes closed/internal/admin acts
        ▼
   AI check (optional): open & external opportunity?         ← removes subtle false positives
        ▼
   Confirmed → EMAIL (once) → recorded as alerted
```

Each stage is cheaper than the next, so we spend expensive effort (downloading, AI) only on the few notices that deserve it.

---

## 7. The three AI modes (cost vs coverage)

Chosen at runtime with `--ai-mode`:

- **`off`** — no AI; rules only. Free, always works, still removes most noise.
- **`capped`** — AI on up to `--ai-budget` notices per run (default 50), then rules for the rest. Stays within a free API quota. *(Default.)*
- **`full`** — AI on every candidate. Best quality; needs a paid Gemini key for large runs.

The **AI key** goes in `.env` as `GEMINI_API_KEY`. Without it, the tool runs in rules-only mode automatically.

---

## 8. What an alert looks like

When an opportunity is confirmed, recipients get an **Italian** email:
- **Subject:** `Opportunità formatore IA PNRR: {scuola} (scade {data})`
- **Body:** school name, code, region, CUP, CLP, amount; the notice title; a clickable **Vedi avviso** button to the official notice; the opportunity type, deadline, confidence, and a one-line "Perché è stato segnalato" (why it was flagged).

You can send to several people at once via `RECEIVER_EMAIL`, `CC_EMAIL`, `BCC_EMAIL` (Bcc recipients are hidden from the others).

---

## 9. Running it

```bash
uv sync                              # install dependencies (one time)
cp .env.example .env                 # then fill in email + optional AI key

# --- Examples / testing (small sample CSV, used by default) ---
python run_monitor.py --map          # Phase A: build the address book (vendor + albo_id)
python run_monitor.py --dry-run      # Phase B test run: finds matches, sends nothing
python run_monitor.py                # Phase B live: emails matches, remembers them

# --- The real thing (full funded list) ---
python scripts/extract_ministry_pdf.py                                 # PDF -> data/projects_funded.csv (~4,311)
python run_monitor.py --map     --projects data/projects_funded.csv    # map them all (slow, one-time)
python run_monitor.py           --projects data/projects_funded.csv    # monitor them all
```

```bash
# --- Archive of opportunities (incl. expired) ---
python run_monitor.py --export-opportunities data/opportunities.csv
```

Useful flags: `--projects FILE` (which list to run), `--limit N` (only first N schools), `--ai-mode {off,capped,full}`, `--ai-budget N`, `--search-budget N` (web-search cap for the fallback), `--export-opportunities FILE`.

**Scheduling:** `.github/workflows/hourly-monitor.yml` can run it automatically every hour in the cloud (GitHub Actions), so it works even when your laptop is off. It needs the same settings stored as repository "secrets."

---

## 10. Honest limits (be transparent with your team)

- **Mapping isn't 100%:** ~1 in 5 schools won't auto-resolve (no registered website, or the site is down). Those need a manual albo ID, or they're skipped.
- **Timing is everything:** calls are open for short windows. The tool's value is *catching them early* — so it should run frequently (hourly), and the funding wave it watches will eventually close.
- **AI needs quota:** the free Gemini tier is small; for whole-list coverage you need a paid key (it's cheap per check, but it adds up over thousands of notices).
- **Generic/own-site schools are read less precisely** than the clean vendor feeds (their pages are messier), so they lean more on the AI judge.
- **It reads public data politely**, but vendors can change their page formats; if that happens, the matching adapter needs a small update (the design isolates that to one file).
```
