from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Literal

from .models import AlboItem, CandidateDocument
from .state import StateStore
from .verifier import AiVerifier, normalize_text

# Role/position vocabulary for comparing whether two documents are plausibly
# about the SAME selection process. Narrower and more specific than
# CALL_TERMS/CALL_CORE_TERMS in verifier.py — those decide "is this an open
# call at all"; this decides "is it the same call as another one we've seen".
ROLE_TERMS = (
    "esperto", "esperti", "tutor", "formatore", "formatori", "docente", "docenti",
    "ata", "assistente amministrativo", "collaboratore", "collaboratori",
    "referente", "coordinatore", "project manager", "rup", "dirigente scolastico",
)

_PROTOCOL_RE = re.compile(r"prot(?:ocollo)?\.?\s*n?\.?\s*(\d+)\s*del\s*(\d{1,2}[/.]\d{1,2}[/.]\d{2,4})")

# Concrete apply-instructions vs. pure authorization language — the signal
# used to distinguish an actionable Avviso from a Decreto that only
# authorizes a process without telling a candidate how to apply.
_APPLY_INSTRUCTION_RE = re.compile(
    r"presentare\s+(?:la\s+)?(?:propria\s+)?candidatura|inviare\s+(?:la\s+)?(?:propria\s+)?istanza|"
    r"domanda\s+di\s+partecipazione|modalita\s+di\s+partecipazione|come\s+presentare|"
    r"indirizzo\s+(?:email|mail|pec)|entro\s+(?:e\s+non\s+oltre\s+)?le\s+ore|entro\s+il\s+\d"
)
_AUTHORIZING_ONLY_RE = re.compile(
    r"si\s+autorizza\s+l.avvio|autorizza\s+l.avvio\s+della\s+procedura|"
    r"decreta\s+di\s+autorizzare|si\s+decreta\s+l.avvio"
)
# Confirmed real case: a 53KB pure-authorization Decreto ("VISTO...VISTA...
# DECRETA...") that doesn't match _AUTHORIZING_ONLY_RE's specific phrasings
# has ZERO occurrences of domanda/candidatura/presentare/istanza/entro
# anywhere in the whole document — a much more reliable negative signal than
# trying to enumerate every authorization phrasing. If none of this weak
# vocabulary appears at all, the document isn't describing an application
# process, full stop; no need to guess or spend an AI call on it.
_WEAK_APPLICATION_VOCAB_RE = re.compile(
    r"domanda|candidatura|istanza|presentare|inviare|iscrizione|modalita\s+di\s+partecipazione"
)

TIME_PROXIMITY_DAYS = 5
AI_ESCALATION_MAX_DAYS = 30


def _parse_date(value: str) -> date | None:
    value = " ".join((value or "").strip().split())
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            pass
    try:
        return parsedate_to_datetime(value).date()
    except Exception:
        return None


@dataclass(frozen=True)
class ThreadSignals:
    protocol_ref: str | None
    role_signature: frozenset[str]
    published: date | None


def extract_signals(candidate: CandidateDocument, item: AlboItem) -> ThreadSignals:
    text = normalize_text(f"{candidate.title} {candidate.text}")
    match = _PROTOCOL_RE.search(text)
    protocol_ref = f"{match.group(1)}|{match.group(2)}" if match else None
    roles = frozenset(term for term in ROLE_TERMS if term in text)
    return ThreadSignals(protocol_ref=protocol_ref, role_signature=roles, published=_parse_date(item.published))


def is_actionable(candidate: CandidateDocument, ai_verifier: AiVerifier) -> bool:
    """Does this document give a candidate concrete steps to apply, or is it
    primarily an authorization act that references the process without
    restating how to apply? Cheap rule pass first; AI only for the narrow
    genuinely-ambiguous middle ground."""
    text = normalize_text(f"{candidate.title} {candidate.text}")
    if _APPLY_INSTRUCTION_RE.search(text):
        return True
    if _AUTHORIZING_ONLY_RE.search(text) or not _WEAK_APPLICATION_VOCAB_RE.search(text):
        # Confidently not actionable: either explicit authorization-only
        # language, or (confirmed real case) NO application-related word at
        # all anywhere in the document — a document genuinely describing an
        # application process always mentions at least one of these.
        return False
    # Some weak application vocabulary present, but not the concrete
    # instruction phrase — genuinely ambiguous. Escalate to AI, budget-gated
    # the same way regular verification calls already are.
    verdict = ai_verifier.is_actionable_content(candidate.text)
    return verdict if verdict is not None else True  # default to sending rather than silently dropping


@dataclass(frozen=True)
class ThreadDecision:
    action: Literal["send_new", "send_reply", "hold", "suppress"]
    thread_id: str | None
    reason: str


class ThreadMatcher:
    """Decides whether a confirmed candidate belongs to an already-known
    selection process (a `call_threads` row) or starts a new one, and what
    to do about it: send a fresh email, thread a reply onto an existing one,
    hold it (no actionable content yet), or suppress it (adds nothing new).

    Deliberately conservative about linking: two documents are only treated
    as the same process on a matching protocol citation, or overlapping role
    language plus tight publish-date proximity — "same CUP" alone is proven
    (from real production data — TOIC82100C has 2-3 distinct roles under one
    CUP) to sometimes span multiple genuinely separate selection processes.
    """

    def __init__(self, state: StateStore, ai_verifier: AiVerifier) -> None:
        self.state = state
        self.ai_verifier = ai_verifier
        self._cache: dict[tuple[str, str], list[dict]] = {}

    def _open_threads(self, school_code: str, cup: str) -> list[dict]:
        key = (school_code, cup)
        if key not in self._cache:
            self._cache[key] = self.state.find_open_threads(school_code, cup)
        return self._cache[key]

    def _invalidate(self, school_code: str, cup: str) -> None:
        self._cache.pop((school_code, cup), None)

    def create_thread(
        self, school_code: str, cup: str, clp: str, signals: ThreadSignals,
        status: str, subject: str | None, message_id: str | None, last_item_key: str, last_title: str,
    ) -> str:
        """Wraps StateStore.create_thread so the matcher's own cache never
        goes stale relative to threads it creates — callers must go through
        this (not the state store directly) for cache consistency."""
        thread_id = self.state.create_thread(
            school_code, cup, clp, " ".join(sorted(signals.role_signature)), signals.protocol_ref,
            status, subject, message_id, last_item_key, last_title,
        )
        self._invalidate(school_code, cup)
        return thread_id

    def update_thread(self, school_code: str, cup: str, thread_id: str, **fields: str | None) -> None:
        self.state.update_thread(thread_id, **fields)
        self._invalidate(school_code, cup)

    def get_thread(self, school_code: str, cup: str, thread_id: str) -> dict | None:
        """Look up one specific thread's current row (e.g. to read its root
        message_id/subject before sending a reply)."""
        return next((t for t in self._open_threads(school_code, cup) if t["thread_id"] == thread_id), None)

    @staticmethod
    def merge_signals(signals_list: list[ThreadSignals]) -> ThreadSignals:
        """Union role signatures and take the first non-None protocol_ref
        across a batch of documents resolved to the same thread/group."""
        roles: frozenset[str] = frozenset()
        protocol_ref = None
        published = None
        for s in signals_list:
            roles |= s.role_signature
            protocol_ref = protocol_ref or s.protocol_ref
            published = published or s.published
        return ThreadSignals(protocol_ref=protocol_ref, role_signature=roles, published=published)

    def resolve(
        self, candidate: CandidateDocument, signals: ThreadSignals, actionable: bool,
    ) -> ThreadDecision:
        project = candidate.project
        cup = project.cup
        if not cup:
            # Nothing to group by — behaves like today (one document, one
            # standalone decision), just gated by the actionable check.
            return ThreadDecision("send_new" if actionable else "hold", None, "No CUP to link against.")

        open_threads = self._open_threads(project.school_code, cup)
        matched, why = self._match(candidate, signals, open_threads)

        if matched is None:
            reason = f"New process for this CUP ({why})." if open_threads else "First document seen for this CUP."
            return ThreadDecision("send_new" if actionable else "hold", None, reason)

        # A decision is about to change this thread's state; the cached copy
        # will be stale until whoever calls create/update refreshes it, so
        # drop it rather than risk resolving a second alert in the same run
        # against out-of-date status.
        self._invalidate(project.school_code, cup)

        if matched["status"] == "held":
            if actionable:
                return ThreadDecision("send_new", matched["thread_id"], f"Held thread now has actionable content ({why}).")
            return ThreadDecision("hold", matched["thread_id"], f"Still no actionable content ({why}).")
        # status == "sent"
        if actionable:
            return ThreadDecision("send_reply", matched["thread_id"], f"Companion document for an already-sent process ({why}).")
        return ThreadDecision("suppress", matched["thread_id"], f"No new information beyond what was already sent ({why}).")

    def _match(
        self, candidate: CandidateDocument, signals: ThreadSignals, open_threads: list[dict],
    ) -> tuple[dict | None, str]:
        if not open_threads:
            return None, "no open threads for this CUP"

        if signals.protocol_ref:
            for t in open_threads:
                if t["protocol_ref"] and t["protocol_ref"] == signals.protocol_ref:
                    return t, "matching protocol reference"

        for t in open_threads:
            existing_roles = frozenset((t["role_signature"] or "").split())
            if signals.role_signature & existing_roles and self._days_apart(t, signals) <= TIME_PROXIMITY_DAYS:
                return t, "overlapping role + close publish date"

        # Ambiguous: only escalate to AI for the single closest candidate by
        # publish-date proximity (not every open thread) — keeps this rare,
        # and only within a bounded window past which two documents are very
        # unlikely to be the same process regardless of what AI says.
        closest = min(open_threads, key=lambda t: self._days_apart(t, signals))
        if self._days_apart(closest, signals) <= AI_ESCALATION_MAX_DAYS:
            verdict = self.ai_verifier.same_process(candidate.text, closest.get("last_title", ""))
            if verdict:
                return closest, "AI judged same process"
        return None, "no matching thread"

    @staticmethod
    def _days_apart(thread: dict, signals: ThreadSignals) -> int:
        # Proximity is judged off updated_at (set whenever the thread is
        # touched) rather than the original document's publish date, which
        # isn't stored on the thread row.
        updated = thread.get("updated_at")
        if isinstance(updated, str):
            updated_date = _parse_date(updated.split(" ")[0])  # "YYYY-MM-DD HH:MM:SS" -> "YYYY-MM-DD"
        else:
            updated_date = updated.date() if updated else None
        if signals.published is None or updated_date is None:
            return AI_ESCALATION_MAX_DAYS + 1  # unknown — treat as far apart, don't auto-link
        return abs((signals.published - updated_date).days)
