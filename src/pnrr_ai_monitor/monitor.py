from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace

from .adapters.base import AdapterRegistry
from .adapters.search import SEARCH_WINDOW_DAYS
from .alerts import AlertService
from .config import Settings
from .deadline_parser import extract_application_deadline, is_deadline_passed
from .logging_utils import log_message
from .models import Alert, AlboItem, CandidateDocument, Confidence, Opportunity, ProjectRecord
from .repository import ProjectRepository
from .state import StateStore
from .thread_matching import ThreadDecision, ThreadMatcher, ThreadSignals, extract_signals, is_actionable
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
    ai_verified: int = 0
    rule_only: int = 0

    def add(self, other: "RunStats") -> None:
        self.schools += other.schools
        self.unsupported += other.unsupported
        self.new_items += other.new_items
        self.prefiltered += other.prefiltered
        self.confirmed += other.confirmed
        self.alerts += other.alerts
        self.ai_failures += other.ai_failures
        self.ai_verified += other.ai_verified
        self.rule_only += other.rule_only

    def summary(self) -> str:
        return (
            f"Schools: {self.schools}; unsupported: {self.unsupported}; "
            f"new items: {self.new_items}; passed prefilter: {self.prefiltered}; "
            f"confirmed: {self.confirmed}; alerts: {self.alerts}; ai_failures: {self.ai_failures}; "
            f"ai_verified: {self.ai_verified}; rule_only (AI not consulted): {self.rule_only}"
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
        thread_matcher: ThreadMatcher | None = None,
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
        self.thread_matcher = thread_matcher or ThreadMatcher(state, ai_verifier)
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
        cooldown_note = ""
        if self.ai_verifier.skipped_in_cooldown:
            cooldown_note = (
                f"; ai_cooldown_skips: {self.ai_verifier.skipped_in_cooldown} "
                f"(last rate-limit error: {self.ai_verifier.last_rate_limit_error})"
            )
        log_message(f"Run ended. {stats.summary()}{cooldown_note}", self.settings.log_file)
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

        pending: list[tuple[Alert, AlboItem, ThreadDecision, ThreadSignals]] = []
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
                error_text = str(exc)
                log_message(f"ERROR enriching {item.key}: {exc}", self.settings.log_file)
                decisions.append((item.key, project.school_code, item.title, item.url,
                                  "enrich_error", error_text[:300]))
                if "404" in error_text:
                    # A 404 means the document is permanently gone, not a
                    # transient blip - without this, an item that never gets
                    # marked processed gets re-fetched and re-parsed forever,
                    # every single run. Other errors (timeouts, 5xx) stay
                    # unmarked so they still get retried, since those can
                    # resolve on their own.
                    processed.append(item)
                continue
            if self._outside_search_window(item):
                processed.append(item)
                decisions.append((item.key, project.school_code, item.title, item.url,
                                  "outside_search_window",
                                  f"Published {item.published}, older than the rolling "
                                  f"{SEARCH_WINDOW_DAYS}-day search window as of today."))
                continue
            if self._is_expired(item):
                processed.append(item)
                decisions.append((item.key, project.school_code, item.title, item.url,
                                  "expired", f"Platform-reported archive/expiry date {item.expires} has passed."))
                continue
            try:
                text = adapter.hydrate(item)
            except Exception as exc:
                error_text = str(exc)
                log_message(f"ERROR hydrating {item.key}: {exc}", self.settings.log_file)
                decisions.append((item.key, project.school_code, item.title, item.url,
                                  "hydrate_error", error_text[:300]))
                if "404" in error_text:
                    processed.append(item)
                continue
            # The platform's expiry date is visibility, not necessarily the
            # real application cutoff — confirmed real mismatch (CNIC83100E:
            # platform said 15/07/2026, the document itself said 14/07/2026
            # 12:00). Check the document's own stated deadline too, after
            # the cheap platform-only check above already filtered out the
            # obviously-expired majority.
            doc_deadline = extract_application_deadline(text)
            if doc_deadline is not None and is_deadline_passed(doc_deadline):
                processed.append(item)
                deadline_str = doc_deadline.date.strftime("%d/%m/%Y")
                if doc_deadline.time is not None:
                    deadline_str += f" {doc_deadline.time.strftime('%H:%M')}"
                decisions.append((item.key, project.school_code, item.title, item.url, "expired",
                                  f"Document-stated application deadline {deadline_str} has passed "
                                  f"(platform-reported expiry was {item.expires or 'unspecified'})."))
                continue
            context = f"Categoria: {item.category} | Pubblicato: {item.published} | Scadenza: {item.expires}".strip()
            candidate = CandidateDocument(project, item.url, item.title, item.url, text=f"{context}\n{text}")
            alert, can_mark_processed = self._evaluate(candidate, item, dry_run, stats, decisions)
            if alert is not None:
                signals = extract_signals(candidate, item)
                actionable = is_actionable(candidate, self.ai_verifier)
                decision = self.thread_matcher.resolve(candidate, signals, actionable)
                pending.append((alert, item, decision, signals))
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
    def _outside_search_window(item: AlboItem) -> bool:
        # Only the web-search fallback has this ambiguity — every other
        # adapter reads real platform data, not a DuckDuckGo result with no
        # date of its own. date.today() is evaluated fresh each time this
        # runs, so the window is always "the last N days as of today," not a
        # fixed range left over from an earlier day. An item whose published
        # date we couldn't determine at all is NOT rejected here — absence
        # of a signal isn't evidence it's stale, so it proceeds normally.
        if item.vendor.lower() != "search":
            return False
        published = Monitor._parse_date(item.published)
        if published is None:
            return False
        return published < date.today() - timedelta(days=SEARCH_WINDOW_DAYS)

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
        if final.ai_used:
            stats.ai_verified += 1
        else:
            stats.rule_only += 1
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
        # Deadline shown to the user, priority order: (1) the document's own
        # stated application deadline — deterministic regex, not an AI guess,
        # so it doesn't have the run-to-run inconsistency problem an AI-
        # extracted date would; (2) the platform's structured archive/expiry
        # date (Argo's dataArchiviazioneEffettiva, Spaggiari's data_scadenza)
        # — visibility, not necessarily the true cutoff, but better than
        # nothing; (3) whatever the AI/rule verifier already put in
        # final.deadline, left untouched, as a last resort.
        doc_deadline = extract_application_deadline(candidate.text)
        if doc_deadline is not None:
            deadline_str = doc_deadline.date.strftime("%d/%m/%Y")
            if doc_deadline.time is not None:
                deadline_str += f" {doc_deadline.time.strftime('%H:%M')}"
            final = replace(final, deadline=deadline_str)
        else:
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
        pending: list[tuple[Alert, AlboItem, ThreadDecision, ThreadSignals]],
        dry_run: bool,
        stats: RunStats,
        decisions: list[tuple[str, str, str, str, str, str]],
    ) -> list[AlboItem]:
        # Items already resolved to the SAME existing thread are grouped by
        # that thread_id — unambiguous. Brand-new items (thread_id is None)
        # still need clustering by role/protocol overlap even when they
        # share a CUP: a school publishing separate Tutor and Esperto
        # decreti under one project CUP is two different calls, not one,
        # even though both are "new" in the same run (confirmed real case:
        # SAIS07600R has exactly this pair).
        threaded: dict[str, list[tuple[Alert, AlboItem, ThreadDecision, ThreadSignals]]] = {}
        new_by_cup: dict[tuple[str, str], list[tuple[Alert, AlboItem, ThreadDecision, ThreadSignals]]] = {}
        for entry in pending:
            decision = entry[2]
            project = entry[0].candidate.project
            if decision.thread_id:
                threaded.setdefault(decision.thread_id, []).append(entry)
            else:
                new_by_cup.setdefault((project.school_code, project.cup or project.school_code), []).append(entry)

        groups: list[list[tuple[Alert, AlboItem, ThreadDecision, ThreadSignals]]] = list(threaded.values())
        for cup_entries in new_by_cup.values():
            groups.extend(self._cluster_new_entries(cup_entries))

        sent_items: list[AlboItem] = []
        for entries in groups:
            project = entries[0][0].candidate.project
            cup_label = project.cup or project.school_code
            actions = {e[2].action for e in entries}
            merged_signals = ThreadMatcher.merge_signals([e[3] for e in entries])
            last_item, last_title = entries[-1][1], entries[-1][0].candidate.title

            if actions & {"send_new", "send_reply"}:
                sent_items.extend(self._send_thread_group(
                    entries, cup_label, merged_signals, last_item, last_title,
                    is_reply="send_reply" in actions, dry_run=dry_run, stats=stats, decisions=decisions,
                ))
            elif "hold" in actions:
                thread_id = next((e[2].thread_id for e in entries if e[2].thread_id), None)
                if not dry_run:
                    if thread_id:
                        self.thread_matcher.update_thread(
                            project.school_code, cup_label, thread_id,
                            role_signature=" ".join(sorted(merged_signals.role_signature)),
                            protocol_ref=merged_signals.protocol_ref,
                            last_item_key=last_item.key, last_title=last_title,
                        )
                    else:
                        self.thread_matcher.create_thread(
                            project.school_code, cup_label, project.clp, merged_signals,
                            "held", None, None, last_item.key, last_title,
                        )
                for alert, item, decision, _ in entries:
                    decisions.append((item.key, project.school_code, item.title, item.url, "held",
                                      "Confirmed opportunity, but no actionable application details yet; "
                                      "waiting for a companion document (e.g. the Avviso) before emailing."))
                sent_items.extend(item for _, item, _, _ in entries)
            else:
                # all "suppress" — a companion document that adds nothing new
                # beyond what was already alerted for this process.
                for alert, item, decision, _ in entries:
                    decisions.append((item.key, project.school_code, item.title, item.url,
                                      "suppressed_duplicate", decision.reason))
                sent_items.extend(item for _, item, _, _ in entries)
        return sent_items

    @staticmethod
    def _cluster_new_entries(
        entries: list[tuple[Alert, AlboItem, ThreadDecision, ThreadSignals]],
    ) -> list[list[tuple[Alert, AlboItem, ThreadDecision, ThreadSignals]]]:
        """Greedily group same-CUP, not-yet-threaded items by role/protocol
        overlap — same linking rule ThreadMatcher uses against persisted
        threads, applied here among items discovered together in one run so
        two genuinely distinct new processes never get merged just because
        they share a CUP."""
        clusters: list[tuple[frozenset[str], str | None, list]] = []
        for entry in entries:
            signals = entry[3]
            for i, (roles, protocol_ref, bucket) in enumerate(clusters):
                same_protocol = bool(signals.protocol_ref and protocol_ref and signals.protocol_ref == protocol_ref)
                if same_protocol or (signals.role_signature & roles):
                    bucket.append(entry)
                    clusters[i] = (roles | signals.role_signature, protocol_ref or signals.protocol_ref, bucket)
                    break
            else:
                clusters.append((signals.role_signature, signals.protocol_ref, [entry]))
        return [bucket for _, _, bucket in clusters]

    def _send_thread_group(
        self,
        entries: list[tuple[Alert, AlboItem, ThreadDecision, ThreadSignals]],
        cup_label: str,
        merged_signals: ThreadSignals,
        last_item: AlboItem,
        last_title: str,
        is_reply: bool,
        dry_run: bool,
        stats: RunStats,
        decisions: list[tuple[str, str, str, str, str, str]],
    ) -> list[AlboItem]:
        project = entries[0][0].candidate.project
        alerts = [e[0] for e in entries]
        thread_id = next((e[2].thread_id for e in entries if e[2].thread_id), None)

        in_reply_to = references = subject_override = None
        if is_reply and thread_id:
            root = self.thread_matcher.get_thread(project.school_code, cup_label, thread_id)
            if root:
                in_reply_to = references = root.get("message_id")
                subject_override = root.get("subject")

        success, message_id, subject = self.alerts.send(
            alerts, dry_run=dry_run, in_reply_to=in_reply_to, references=references, subject_override=subject_override,
        )
        if not success:
            for alert, item, decision, _ in entries:
                decisions.append((item.key, project.school_code, item.title, item.url,
                                  "send_failed", "Confirmed and grouped, but AlertService.send() did not succeed."))
            return []

        stats.alerts += 1
        sent_items: list[AlboItem] = []
        if not dry_run:
            for alert, item, decision, _ in entries:
                self.state.mark_alerted(alert.unique_id, project.school_code, item.url)
                sent_items.append(item)
            # A reply threads onto the existing root — don't overwrite its
            # stored message_id/subject. A fresh send establishes a new
            # thread (or upgrades a held one) and becomes the root itself.
            if is_reply and thread_id:
                self.thread_matcher.update_thread(project.school_code, cup_label, thread_id,
                                                   last_item_key=last_item.key, last_title=last_title)
            elif thread_id:
                self.thread_matcher.update_thread(
                    project.school_code, cup_label, thread_id,
                    status="sent", subject=subject, message_id=message_id,
                    last_item_key=last_item.key, last_title=last_title,
                )
            else:
                self.thread_matcher.create_thread(
                    project.school_code, cup_label, project.clp, merged_signals,
                    "sent", subject, message_id, last_item.key, last_title,
                )
        else:
            sent_items.extend(item for _, item, _, _ in entries)

        for alert, item, decision, _ in entries:
            decisions.append((item.key, project.school_code, item.title, item.url,
                              "alerted", alert.verification.reason))
        log_message(
            f"ALERT: {project.school_name} | CUP {cup_label} | {len(alerts)} avviso/i: "
            + "; ".join(a.candidate.title[:50] for a in alerts),
            self.settings.log_file,
        )
        return sent_items
