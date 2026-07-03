from __future__ import annotations

import time
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace

from .adapters.base import AdapterRegistry
from .alerts import AlertService
from .config import Settings
from .logging_utils import log_message
from .models import Alert, AlboItem, CandidateDocument, Confidence, Opportunity, ProjectRecord
from .repository import ProjectRepository
from .state import StateStore
from .verifier import AiVerifier, OpportunityPrefilter, OpportunityVerifier


@dataclass
class RunStats:
    schools: int = 0
    unsupported: int = 0
    new_items: int = 0
    prefiltered: int = 0
    confirmed: int = 0
    alerts: int = 0
    ai_failures: int = 0

    def add(self, other: "RunStats") -> None:
        self.schools += other.schools
        self.unsupported += other.unsupported
        self.new_items += other.new_items
        self.prefiltered += other.prefiltered
        self.confirmed += other.confirmed
        self.alerts += other.alerts
        self.ai_failures += other.ai_failures

    def summary(self) -> str:
        return (
            f"Schools: {self.schools}; unsupported: {self.unsupported}; "
            f"new items: {self.new_items}; passed prefilter: {self.prefiltered}; "
            f"confirmed: {self.confirmed}; alerts: {self.alerts}; ai_failures: {self.ai_failures}"
        )


class Monitor:
    """Orchestrates one polling pass."""

    def __init__(
        self,
        settings: Settings,
        repository: ProjectRepository,
        registry: AdapterRegistry,
        prefilter: OpportunityPrefilter,
        rule_verifier: OpportunityVerifier,
        ai_verifier: AiVerifier,
        state: StateStore,
        alerts: AlertService,
        per_school_delay: float = 0.0,
        workers: int = 1,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.registry = registry
        self.prefilter = prefilter
        self.rule_verifier = rule_verifier
        self.ai_verifier = ai_verifier
        self.state = state
        self.alerts = alerts
        self.per_school_delay = per_school_delay
        self.workers = max(1, workers)

    def run(self, dry_run: bool = False, limit: int | None = None) -> RunStats:
        projects = [p for p in self.repository.load() if p.is_mapped]
        if limit:
            projects = projects[:limit]
        elif projects and not dry_run:
            # Rotate the start of the list so a run that gets cut off partway
            # (timeout, manual cancel) doesn't leave the tail of the CSV
            # permanently unreached while the head gets re-polled every day.
            start = self.state.get_checkpoint() % len(projects)
            projects = projects[start:] + projects[:start]
        ai_desc = self.ai_verifier.mode
        if ai_desc == "capped":
            ai_desc += f" (budget {self.ai_verifier.budget_left})"
        log_message(
            f"Starting monitor. Mapped schools: {len(projects)}; "
            f"adapters: {', '.join(sorted(self.registry.supported())) or 'none'}; "
            f"ai-mode: {ai_desc}; workers: {self.workers}",
            self.settings.log_file,
        )
        stats = RunStats()
        total = len(projects)
        if self.workers == 1:
            for project in projects:
                stats.add(self._process_school(project, dry_run))
                if not dry_run:
                    self.state.advance_checkpoint(1, total)
                if self.per_school_delay:
                    time.sleep(self.per_school_delay)
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [pool.submit(self._process_school, project, dry_run) for project in projects]
                for future in as_completed(futures):
                    try:
                        stats.add(future.result())
                    except Exception as exc:
                        log_message(f"ERROR worker failed: {exc}", self.settings.log_file)
                    if not dry_run:
                        self.state.advance_checkpoint(1, total)
        log_message(f"Run ended. {stats.summary()}", self.settings.log_file)
        return stats

    def _process_school(self, project: ProjectRecord, dry_run: bool) -> RunStats:
        stats = RunStats()
        adapter = self.registry.get(project.vendor)
        if adapter is None:
            stats.unsupported += 1
            return stats
        stats.schools += 1
        try:
            items = adapter.fetch_items(project)
        except Exception as exc:
            log_message(f"ERROR fetching {project.school_code} ({project.vendor}): {exc}", self.settings.log_file)
            return stats

        new_keys = self.state.unprocessed_keys(item.key for item in items)
        new_items = [item for item in items if item.key in new_keys]
        stats.new_items += len(new_items)

        # A notice can look open on its own but already be closed in practice: the
        # SAME selection (sharing the project's CUP/CLP) later got a graduatoria /
        # decreto di affidamento / esito. Check the full fetched history (not just
        # the new items) so this catches results that appeared before this run
        # even started.
        superseded = self._superseded_keys(project, items)

        pending: list[tuple[Alert, AlboItem]] = []
        processed: list[AlboItem] = []
        # Every item that reaches a verdict — including "never got past the cheap
        # keyword gate" — gets one row here. This is what makes a future miss
        # (of ANY kind, not just the ones we already know about) diagnosable
        # after the fact instead of invisible: `--why SCHOOL_CODE` reads it back.
        decisions: list[tuple[str, str, str, str, str, str]] = []
        for item in new_items:
            if item.key in superseded:
                processed.append(item)
                decisions.append((item.key, project.school_code, item.title, item.url,
                                  "superseded", "A later result act (graduatoria/decreto di "
                                  "affidamento/aggiudicazione) sharing this project's CUP/CLP was found."))
                continue
            if not self.prefilter.is_relevant(item):
                processed.append(item)
                decisions.append((item.key, project.school_code, item.title, item.url,
                                  "prefilter_rejected",
                                  "Title/category matched no known call-language or D.M. 219 signal."))
                continue
            stats.prefiltered += 1
            try:
                item = adapter.enrich_item(item)
            except Exception as exc:
                log_message(f"ERROR enriching {item.key}: {exc}", self.settings.log_file)
                decisions.append((item.key, project.school_code, item.title, item.url,
                                  "enrich_error", str(exc)[:300]))
                continue
            if self._is_expired(item):
                processed.append(item)
                decisions.append((item.key, project.school_code, item.title, item.url,
                                  "expired", f"Platform-reported archive/expiry date {item.expires} has passed."))
                continue
            try:
                text = adapter.hydrate(item)
            except Exception as exc:
                log_message(f"ERROR hydrating {item.key}: {exc}", self.settings.log_file)
                decisions.append((item.key, project.school_code, item.title, item.url,
                                  "hydrate_error", str(exc)[:300]))
                continue
            context = f"Categoria: {item.category} | Pubblicato: {item.published} | Scadenza: {item.expires}".strip()
            candidate = CandidateDocument(project, item.url, item.title, item.url, text=f"{context}\n{text}")
            alert, can_mark_processed = self._evaluate(candidate, item, dry_run, stats, decisions)
            if alert is not None:
                pending.append((alert, item))
            elif can_mark_processed:
                processed.append(item)

        processed.extend(self._send_grouped(pending, dry_run, stats, decisions))
        self.state.record_decisions(decisions)

        if not dry_run and processed:
            self.state.mark_processed(processed)
        return stats

    _RESULT_MARKERS = ("graduat", "decreto affidamento", "decreto di affidamento",
                       "aggiudicazione", "esito procedura", "esito selezione",
                       "esito della selezione")

    def _superseded_keys(self, project: ProjectRecord, items: list[AlboItem]) -> set[str]:
        """Keys of notices whose selection (same CUP or CLP appearing in the
        title) was later followed by a result act published on or after them —
        i.e. the window has closed even though the notice itself doesn't say so."""
        codes = [c.lower() for c in (project.cup, project.clp) if c]
        if not codes:
            return set()
        results = [
            it for it in items
            if any(marker in it.title.lower() for marker in self._RESULT_MARKERS)
            and any(code in it.title.lower() for code in codes)
        ]
        if not results:
            return set()
        superseded: set[str] = set()
        for item in items:
            if item in results:
                continue
            low = item.title.lower()
            if not any(code in low for code in codes):
                continue
            item_date = self._parse_date(item.published)
            for result in results:
                result_date = self._parse_date(result.published)
                if item_date is None or result_date is None or result_date >= item_date:
                    superseded.add(item.key)
                    break
        return superseded

    @staticmethod
    def _is_expired(item: AlboItem) -> bool:
        expires = (item.expires or "").strip()
        if not expires:
            return False
        parsed = Monitor._parse_date(expires)
        return bool(parsed and parsed <= date.today())

    @staticmethod
    def _parse_date(value: str) -> date | None:
        value = " ".join(value.strip().split())
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(value[:10], fmt).date()
            except ValueError:
                pass
        try:
            return parsedate_to_datetime(value).date()
        except Exception:
            return None

    def _evaluate(
        self,
        candidate: CandidateDocument,
        item: AlboItem,
        dry_run: bool,
        stats: RunStats,
        decisions: list[tuple[str, str, str, str, str, str]],
    ) -> tuple[Alert | None, bool]:
        def record(stage: str, reason: str) -> None:
            decisions.append((item.key, candidate.project.school_code, item.title, item.url, stage, reason[:400]))

        rule_result = self.rule_verifier.verify(candidate)
        if not rule_result.is_match:
            record("rule_rejected", rule_result.reason)
            return None, True
        final = self.ai_verifier.verify(candidate, rule_result)
        if final.ai_error:
            stats.ai_failures += 1
            log_message(
                f"WARNING: AI verification unavailable ({final.ai_error}); "
                f"falling back to rules for {candidate.project.school_code} | {candidate.title[:60]}",
                self.settings.log_file,
            )
        if self.settings.ai_verification_required and final.ai_error and final.confidence != Confidence.HIGH:
            record("ai_unavailable_deferred",
                   f"AI call failed ({final.ai_error}) and rule confidence was not HIGH; "
                   f"will retry on a future run rather than dropping it.")
            return None, False
        if self.settings.ai_verification_required and not final.ai_used and final.confidence != Confidence.HIGH:
            record("rule_only_insufficient",
                   f"Rule confidence '{final.confidence.value}' without an AI confirmation "
                   f"(AI not consulted — mode/budget/key). Rule reason: {rule_result.reason}")
            return None, True
        if not final.is_match:
            record("ai_rejected", final.reason)
            return None, True
        # Prefer the platform's own archive/expiry date over the AI's free-text
        # guess for the deadline shown to the user — it's structured data (Argo's
        # dataArchiviazioneEffettiva, Spaggiari's data_scadenza), not a parse of
        # the document body, and we've seen the AI extract inconsistent dates for
        # the same notice across runs.
        expiry = self._parse_date(item.expires)
        if expiry is not None:
            final = replace(final, deadline=expiry.strftime("%d/%m/%Y"))
        stats.confirmed += 1
        alert = Alert(candidate, final)
        if not dry_run:
            self.state.record_opportunity(Opportunity.from_alert(alert))
        if self.state.has_alerted(alert.unique_id):
            record("already_alerted", "Confirmed again, but this exact alert was already sent previously.")
            return None, True
        record("confirmed", final.reason)
        return alert, False

    def _send_grouped(
        self,
        pending: list[tuple[Alert, AlboItem]],
        dry_run: bool,
        stats: RunStats,
        decisions: list[tuple[str, str, str, str, str, str]],
    ) -> list[AlboItem]:
        groups: dict[str, list[tuple[Alert, AlboItem]]] = {}
        for alert, item in pending:
            project = alert.candidate.project
            key = project.cup or project.school_code
            groups.setdefault(key, []).append((alert, item))

        sent_items: list[AlboItem] = []
        for key, entries in groups.items():
            alerts = [a for a, _ in entries]
            if not self.alerts.send(alerts, dry_run=dry_run):
                for alert, item in entries:
                    decisions.append((item.key, alert.candidate.project.school_code, item.title, item.url,
                                      "send_failed", "Confirmed and grouped, but AlertService.send() returned False."))
                continue
            stats.alerts += 1
            project = alerts[0].candidate.project
            if not dry_run:
                for alert, item in entries:
                    self.state.mark_alerted(alert.unique_id, project.school_code, item.url)
                    sent_items.append(item)
            else:
                sent_items.extend(item for _, item in entries)
            for alert, item in entries:
                decisions.append((item.key, project.school_code, item.title, item.url,
                                  "alerted", alert.verification.reason))
            log_message(
                f"ALERT: {project.school_name} | CUP {key} | {len(alerts)} avviso/i: "
                + "; ".join(a.candidate.title[:50] for a in alerts),
                self.settings.log_file,
            )
        return sent_items
