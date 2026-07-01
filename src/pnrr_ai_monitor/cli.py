from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .adapters import (
    AdapterRegistry,
    ArgoAdapter,
    GenericCrawlerAdapter,
    NuvolaAdapter,
    SearchFallbackAdapter,
    SpaggiariAdapter,
)
from .alerts import AlertService
from .config import load_settings
from .document_reader import DocumentReader
from .http_client import HttpClient
from .mapping import SchoolMapper
from .monitor import Monitor
from .repository import ProjectRepository
from .search import SearchProvider, build_search_provider
from .state import build_state_store, classify_status
from .verifier import AI_CAPPED, AI_MODES, AiVerifier, OpportunityPrefilter, OpportunityVerifier


def build_registry(http: HttpClient, reader: DocumentReader, max_pages: int,
                   search_provider: SearchProvider | None) -> AdapterRegistry:
    """Assemble the adapters the monitor knows about. New platforms are added
    here in one place; nothing else changes."""
    registry = AdapterRegistry()
    registry.register(ArgoAdapter(http, reader))
    registry.register(SpaggiariAdapter(http, reader))
    registry.register(NuvolaAdapter(http, reader, max_pages=max_pages))
    registry.register(GenericCrawlerAdapter(http, reader, max_pages=max_pages))
    registry.register(SearchFallbackAdapter(http, reader, search_provider))
    return registry


_EXPORT_COLUMNS = [
    "school_code", "school_name", "region", "cup", "clp", "title", "url",
    "deadline", "status", "opportunity_type", "confidence", "published",
    "first_seen", "last_seen",
]


def export_opportunities(state, out_path: Path) -> int:
    rows = state.iter_opportunities()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_EXPORT_COLUMNS)
        writer.writeheader()
        for r in rows:
            r["status"] = classify_status(r.get("deadline", ""))
            writer.writerow({k: r.get(k, "") for k in _EXPORT_COLUMNS})
    return len(rows)


def print_decision_trail(state, school_code: str) -> None:
    rows = state.iter_decisions(school_code)
    if not rows:
        print(f"No decision trail for {school_code} yet (it may not have been polled, "
              f"or every one of its notices was still 'new' as of the last run).")
        return
    print(f"Decision trail for {school_code} ({len(rows)} notices), most recent first:\n")
    for r in rows:
        print(f"[{r['stage']:22}] {r['updated_at']}  {r['title'][:70]}")
        if r.get("reason"):
            print(f"    -> {r['reason']}")
        print(f"    {r['url']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor funded PNRR AI schools for trainer opportunities.")
    parser.add_argument("--dry-run", action="store_true", help="Do not send email or persist state.")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of schools (POC testing).")
    parser.add_argument("--map", action="store_true", help="Run the mapping pass: fill vendor/albo_id in the CSV, then exit.")
    parser.add_argument("--refresh", action="store_true", help="With --map, re-map even already-mapped schools.")
    parser.add_argument("--workers", type=int, default=10, help="Parallel workers for --map (default 10).")
    parser.add_argument("--ai-mode", choices=AI_MODES, default=AI_CAPPED,
                        help="off: rules only. capped: AI on up to --ai-budget notices. full: AI on every candidate.")
    parser.add_argument("--ai-budget", type=int, default=50, help="Max AI calls per run in 'capped' mode (default 50).")
    parser.add_argument("--search-budget", type=int, default=30, help="Max web searches per run for the search fallback (default 30).")
    parser.add_argument("--monitor-workers", type=int, default=8, help="Parallel workers for monitor runs (default 8).")
    parser.add_argument("--projects", default=None,
                        help="Schools CSV to use. Defaults to data/projects_sample.csv (examples). "
                             "For the real run, point at data/projects_funded.csv.")
    parser.add_argument("--export-opportunities", nargs="?", const="data/opportunities.csv", default=None,
                        metavar="FILE", help="Export the archived opportunities (incl. expired) to CSV, then exit.")
    parser.add_argument("--why", metavar="SCHOOL_CODE", default=None,
                        help="Print the decision trail for one school (why each of its notices did/didn't "
                             "alert, most recent first), then exit. Diagnoses ANY school without re-running.")
    args = parser.parse_args()

    settings = load_settings()
    projects_csv = Path(args.projects).resolve() if args.projects else settings.projects_csv

    if args.export_opportunities is not None:
        state = build_state_store(settings.database_url, settings.history_db)
        try:
            n = export_opportunities(state, Path(args.export_opportunities))
            print(f"Exported {n} opportunities to {args.export_opportunities}")
        finally:
            state.close()
        return

    if args.why is not None:
        state = build_state_store(settings.database_url, settings.history_db)
        try:
            print_decision_trail(state, args.why)
        finally:
            state.close()
        return

    http = HttpClient(timeout=settings.request_timeout)
    reader = DocumentReader()

    if args.map:
        SchoolMapper(http).map_csv(projects_csv, settings.log_file, refresh=args.refresh, workers=args.workers)
        print("Mapping complete.")
        http.close()
        return

    search_provider = build_search_provider(http, budget=args.search_budget)
    state = build_state_store(settings.database_url, settings.history_db)
    if args.ai_mode != "off" and not settings.ai_enabled:
        print("WARNING: AI mode requested, but GEMINI_API_KEY is empty. Falling back to rule-only verification.")

    try:
        monitor = Monitor(
            settings=settings,
            repository=ProjectRepository(projects_csv),
            registry=build_registry(http, reader, settings.max_pages_per_school, search_provider),
            prefilter=OpportunityPrefilter(),
            rule_verifier=OpportunityVerifier(),
            ai_verifier=AiVerifier(mode=args.ai_mode, budget=args.ai_budget),
            state=state,
            alerts=AlertService(settings),
            per_school_delay=0.0,
            workers=args.monitor_workers,
        )
        monitor.run(dry_run=args.dry_run, limit=args.limit)
    finally:
        state.close()
        http.close()


if __name__ == "__main__":
    main()
