from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    progressive: str
    area: str
    school_code: str
    school_name: str
    region: str
    school_type: str
    cup: str
    clp: str
    amount: str
    website: str = ""
    # Funding publication date from the ministry PDF (ISO, e.g. "2026-03-30").
    # Used as the lower bound for the search fallback — a call can't predate funding.
    funded_date: str = ""
    # Resolved by the mapping pass; tells the monitor which adapter to use.
    vendor: str = ""
    albo_id: str = ""
    albo_url: str = ""

    @property
    def has_website(self) -> bool:
        return bool(self.website.strip())

    @property
    def is_mapped(self) -> bool:
        """True once the mapping pass has assigned a vendor we can poll."""
        return bool(self.vendor.strip())


@dataclass(frozen=True, slots=True)
class AlboItem:
    """A single published notice, normalized across every albo platform.

    Adapters translate their vendor-specific payloads into this shape so the
    rest of the pipeline never needs to know which platform a school uses.
    """

    school_code: str
    vendor: str
    item_id: str
    title: str
    url: str
    published: str = ""
    expires: str = ""
    category: str = ""
    pdf_url: str = ""
    detail_ref: str = ""

    @property
    def key(self) -> str:
        """Globally-unique, stable dedup key (school-scoped)."""
        return f"{self.school_code}|{self.item_id}"


@dataclass(frozen=True)
class CandidateDocument:
    project: ProjectRecord
    url: str
    title: str
    source_url: str
    content_type: str = ""
    text: str = ""


@dataclass(frozen=True)
class VerificationResult:
    is_match: bool
    confidence: Confidence
    reason: str
    opportunity_type: str = "sconosciuto"
    deadline: str = "non specificata"
    ai_used: bool = False
    ai_error: str = ""  # set when an AI call was attempted but failed (quota/network)
    # True only for the exact-CUP-match branch where the document mixes
    # internal-staff AND external language (genuinely ambiguous - could be a
    # real mixed opening, or an internal-only notice whose external signal is
    # incidental boilerplate rules can't fully rule out). Confirmed live: this
    # specific case has a much higher wrong-send rate without AI than a plain
    # weak-context MEDIUM match, so monitor.py treats it differently when AI
    # is unavailable (defer, not send-unconfirmed) - see ambiguous_internal_external.
    ambiguous_internal_external: bool = False


@dataclass(frozen=True)
class Alert:
    candidate: CandidateDocument
    verification: VerificationResult

    @property
    def unique_id(self) -> str:
        project = self.candidate.project
        return f"{project.school_code}|{project.cup}|{project.clp}|{self.candidate.url}".lower()


@dataclass(frozen=True, slots=True)
class Opportunity:
    """A confirmed opportunity, persisted to the archive so expired/past calls
    remain browsable (link + key details). Built from an Alert at confirm time."""

    school_code: str
    school_name: str
    region: str
    cup: str
    clp: str
    title: str
    url: str
    published: str = ""
    deadline: str = ""
    opportunity_type: str = ""
    confidence: str = ""

    @property
    def key(self) -> str:
        return f"{self.school_code}|{self.url}".lower()

    @classmethod
    def from_alert(cls, alert: "Alert") -> "Opportunity":
        c = alert.candidate
        p = c.project
        v = alert.verification
        return cls(
            school_code=p.school_code, school_name=p.school_name, region=p.region,
            cup=p.cup, clp=p.clp, title=c.title, url=c.url,
            published=getattr(c, "published", ""), deadline=v.deadline,
            opportunity_type=v.opportunity_type, confidence=v.confidence.value,
        )
