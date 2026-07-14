from __future__ import annotations

import os
import re
import time
import unicodedata
from dataclasses import replace
from threading import Lock

from .models import AlboItem, CandidateDocument, Confidence, VerificationResult

# Real-world text breaks plain substring matching in three independent ways
# we've confirmed against live documents: Argo's feed delivers mojibake
# apostrophes as U+FFFD ("dell'incarico" -> "dell�incarico"), the term
# lists below were typed without full Italian accents ("pubblicita" never
# matches "pubblicità"), and document filenames use hyphens/underscores in
# place of spaces ("decreto-nomina-rup" never matches "nomina rup"). Each of
# these silently breaks NEGATIVE_TERMS/STRONG_NEGATIVE_TERMS/INTERNAL_TERMS
# rejections (and, symmetrically, could suppress genuine CALL_TERMS matches
# too) - normalize both sides identically before comparing so formatting
# differences stop mattering.
_SEPARATOR_CHARS = re.compile(r"[’‘´`'�_-]")


def normalize_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = _SEPARATOR_CHARS.sub(" ", folded)
    return " ".join(folded.lower().split())


PROJECT_CONTEXT_TERMS = (
    "dm 219/2025", "d.m. 219/2025", "dm 219_2025", "d.m. 219", "dm219", "dm 219",
    "219/2025", "219_2025", "avviso 73226/2026", "73226/2026",
    "m4c1i2.1", "m4c1i2.1-2026-1745", "snodi formativi", "snodo formativo",
    "intelligenza artificiale", "educare all'i.a.", "educare all’i.a.", "educare all'ia",
    "educare all’ia", "educare alla i.a.", "educare all ia",
)

CALL_TERMS = (
    "avviso pubblico", "avviso di selezione", "procedura di selezione",
    "procedura per la selezione", "procedura selettiva", "selezione esperti",
    "esperti formatori", "esperto formatore", "esperto", "esperti", "formatore",
    "formatori", "tutor", "reclutamento", "percorsi formativi", "laboratori formativi",
    "percorsi e laboratori formativi", "figure professionali", "operatori economici",
    "operatore economico", "manifestazione di interesse", "manifestazione d'interesse", "indagine di mercato",
    # NOT bare "rdo" here either — see the identical note on EXTERNAL_TERMS
    # below. Confirmed live: "l'accordo di concessione" (funding-agreement
    # boilerplate present in nearly every DM219 document) contains "rdo" and
    # was single-handedly producing a false HIGH-confidence MATCH.
    "affidamento diretto", "servizi di formazione", "mepa", "capitolato",
    "disciplinare", "presentazione candidatura", "istanza di partecipazione",
    "domanda di partecipazione", "richiesta disponibilita", "richiesta disponibilità",
    "candidatura",
)

CALL_CORE_TERMS = (
    "avviso pubblico", "avviso di selezione", "procedura di selezione",
    "procedura per la selezione", "procedura selettiva", "selezione esperti",
    "esperti formatori", "esperto formatore", "esperti esterni", "formatori esterni",
    "reclutamento esperti", "reclutamento tutor", "reclutamento formatori",
    "avviso di reclutamento", "figure professionali", "operatori economici",
    "operatore economico", "manifestazione di interesse", "manifestazione d'interesse operatori economici", "indagine di mercato",
    # NOT bare "rdo" - same accidental-substring issue as above.
    "mepa", "istanza di partecipazione", "domanda di partecipazione",
    "presentazione candidatura", "richiesta disponibilita",
)

# Weak words are useful only when they appear with a strong programme/project
# signal. Alone they are too noisy across school albos.
WEAK_PREFILTER_TERMS = (
    "avviso", "selezione", "formazione", "affidamento", "affidamento diretto",
    "capitolato", "disciplinare", "bandi", "gare", "contratti", "determina",
    "determine", "allegato a", "griglia di valutazione",
)

NEGATIVE_TERMS = (
    "graduatoria definitiva", "graduatoria provvisoria", "graduat", "pubblicazione graduatoria",
    "pubblicaz. graduat", "decreto pubblicazione graduatoria", "verbale commissione",
    "verbale apertura", "apertura candidature", "nomina commissione",
    "determina", "determinazione", "decisione a contrarre", "decisione affid",
    "fornitura", "forniture", "acquisto", "consumabili", "cancelleria",
    "materiale igienico", "elettrodi", "cambridge", "accordo sindacale",
    "riunioni e collegi", "commissione liceo", "studenti", "studente",
    "acfdg", "acprot",
    "nomina rup", "incarico rup", "supporto tecnico", "supporto al rup", "supporto tecnico al rup",
    "responsabile unico del progetto", "assunzione in bilancio", "assunzione al bilancio",
    "decreto assunzione in bilancio", "decreto assunzione al bilancio",
    "disseminazione", "azione di disseminazione", "azione di disseminazione iniziale",
    "pubblicita", "lettera di incarico", "decreto di incarico", "decreto di nomina",
    "conferimento incarico", "conferimento incarich", "aggiudicazione", "determina di aggiudicazione",
    "determinazione di aggiudicazione", "determina cuc", "determinazione cuc",
    "schema di contratto", "contratto",
)

# Acts that are clearly NOT an open opportunity even if they cite the project CUP:
# already decided/awarded, internal admin, pure publicity, or contract-stage docs.
STRONG_NEGATIVE_TERMS = (
    "graduatoria", "graduat", "pubblicazione graduatoria", "pubblicaz. graduat", "aggiudicazione",
    "verbale", "verbale apertura", "apertura candidature", "nomina commissione",
    "determina", "determinazione", "decisione a contrarre", "decisione affid",
    "fornitura", "forniture", "acquisto", "consumabili",
    "materiale igienico", "elettrodi", "cambridge", "accordo sindacale",
    "riunioni e collegi", "commissione liceo", "studenti", "studente",
    "acfdg", "acprot",
    # NOT "stipula" or "classe": both confirmed live to be false-positive noise,
    # not real closure signals. "stipula" only ever appeared as the standard
    # legal-basis boilerplate every avviso cites ("l'istituzione scolastica può
    # STIPULARE contratti di prestazione d'opera con esperti...", D.I. 129/2018
    # art. 45) - describing a FUTURE possible contract, not evidence one was
    # already signed. "classe" is a generic word (school "class", website nav
    # menus) that also happens to appear inside a real funded project's own
    # official title ("IA in classe: consapevolezza e innovazione") - it wrongly
    # rejected that project's own genuine avviso. Every other document these two
    # terms touched in a live 100-school test was already correctly rejected by
    # a separate, more specific term (graduatoria/determina/disseminazione/
    # contratto), so removing them doesn't reopen any of those.
    "commissione valutazione", "assunzione in bilancio", "assunzione al bilancio",
    "decreto assunzione in bilancio", "decreto assunzione al bilancio",
    "disseminazione", "azione di informazione", "azione di disseminazione",
    "azione di disseminazione iniziale", "comunicazione e disseminazione",
    # NOT bare "decreto di assegnazione": that alone also matches "decreto di
    # assegnazione DEL FINANZIAMENTO" — the funding-award decree cited in nearly
    # every DM219 document's own legal preamble, not evidence of a closed
    # selection. Require it to reference the ROLE/incarico specifically.
    "decreto di assegnazione incarico", "decreto di assegnazione dell'incarico",
    "decreto di assegnazione degli incarichi", "assegnazione incarich",
    "conferimento incarico", "conferimento incarich",
    # A school's own project-management/RUP self-appointment decree (e.g. "Decreto
    # di assunzione incarico DS Project Manager") — administrative setup, not a
    # call anyone can respond to. Distinct from "assunzione in bilancio" (budget).
    "assunzione incarico", "assunzione dell'incarico", "atti di nomina",
    "lettera di incarico", "decreto di incarico", "decreto di nomina", "nomina rup",
    "incarico rup", "supporto tecnico", "supporto al rup", "supporto tecnico al rup",
    "responsabile unico del progetto",
    "determina cuc", "determinazione cuc", "schema di contratto", "contratto",
    # NOT "insussistenza"/"conflitto di interessi"/"inesistenza" variants: these
    # were meant to catch a standalone RUP conflict-of-interest declaration act,
    # but in practice they're the standard blank declaration FORM every
    # applicant signs as part of a real application package (confirmed live:
    # PAIS02200V's genuinely open, currently-live "Avviso Pubblico Esperti D.M.
    # 219" — deadline 21/07/2026 — was wrongly rejected solely because its
    # attachment list includes "dichiarazione...insussistenza cause ostative.pdf"
    # and "dichiarazione assenza di conflitto di interessi.pdf", the routine
    # blank forms, not evidence of closure). Same false-positive shape as
    # "classe"/"stipula" above.
    "dichiarazione rup", "dichiarazione del rup",
)

# Restricted to internal staff -> early signal, not directly biddable by an
# external firm unless the same document clearly admits external candidates.
INTERNAL_TERMS = (
    "personale interno", "docenti interni", "docente interno", "esperti interni",
    "tutor interni", "personale docente interno", "personale ata", "figure professionali interne",
    "rivolto a personale interno", "avviso interno", "in servizio presso questa",
    "riservat", "selezione interna",
)
EXTERNAL_TERMS = (
    "esterno", "esterni", "esperti esterni", "formatori esterni", "personale esterno",
    "operatore economico", "operatori economici", "ente di formazione", "enti di formazione",
    "soggetti giuridici", "soggetti qualificati",
    # NOT "manifestazione di interesse": it's a neutral procedural label used
    # for internal-only calls exactly as often as external ones (confirmed
    # live: CHRH01000N's own internal-only-titled avviso is itself named
    # "AVVISO MANIFESTAZIONE DI INTERESSE... ESPERTI INTERNI") - it says
    # nothing about who's eligible, so it can't support an external_hits claim.
    "collaborazione plurima", "collaborazione esterna", "mepa",
    # NOT bare "rdo": it's a 3-letter substring of common Italian words
    # ("riguardo", "accordo", "ricordo", "assurdo"...) — in any document of real
    # length it matches by accident and defeats the internal-only rejection this
    # list exists to support. (Confirmed: matched inside "riguardo" in a real
    # notice, silently turning an internal-only avviso into a false "external".)
)
# NOTE: a rule-based "external is only a fallback contingency" check was
# tried and reverted — the obvious candidate phrase ("indire nuovo avviso
# oppure ricercare all'esterno...") turned out to be generic Italian PA
# boilerplate that shows up in genuinely-both-internal-and-external notices
# too (confirmed: present verbatim in the Caselette notice, which explicitly
# targets "interno/esterno" from the outset). This distinction needs actual
# semantic understanding of the document, not keyword matching — see the
# AI prompt in AiVerifier.verify() instead.

# Strong title/category terms can pass the cheap prefilter alone. Weak terms pass
# only when paired with a programme/project signal.
#
# A title like "Avviso procedura selettiva ... docenti tutor, docenti esperti ...
# per il progetto 'Ai a scuola'" never says "DM 219" or "intelligenza artificiale"
# — so it would fail the weak-path's project-context requirement even though it's
# a real, exact-CUP match once fully verified. Selection/recruitment calls that
# explicitly target esperti/tutor/formatori are specific enough to pass on their
# own; the (already CUP- and AI-gated) verifier is what filters unrelated ones
# out downstream, so this trades a little extra verification cost for catching
# real notices whose title doesn't literally cite the programme.
STRONG_PREFILTER_TERMS = (
    *PROJECT_CONTEXT_TERMS,
    "operatori economici", "operatore economico", "manifestazione di interesse",
    "indagine di mercato", "rdo", "mepa", "esperti esterni", "formatori esterni",
    "incarico esterno", "ente di formazione", "enti di formazione", "soggetti giuridici",
    "soggetti qualificati", "procedura di selezione", "procedura per la selezione",
    "procedura selettiva", "avviso di selezione", "istanza di partecipazione",
    "domanda di partecipazione", "richiesta disponibilita", "richiesta disponibilità",
    "percorsi e laboratori formativi", "reclutamento esperti", "reclutamento tutor",
    "reclutamento formatori", "avviso di reclutamento", "selezione di esperti",
    "selezione di tutor", "individuazione di tutor", "individuazione di esperti",
    "individuazione tutor", "individuazione esperti",
)


# Precomputed once at import time (verify() runs per-candidate, thousands of
# times per run) so matching is cheap: normalize each static term the same
# way the input text gets normalized, rather than re-normalizing per call.
_PROJECT_CONTEXT_TERMS_N = tuple(normalize_text(t) for t in PROJECT_CONTEXT_TERMS)
_CALL_TERMS_N = tuple(normalize_text(t) for t in CALL_TERMS)
_CALL_CORE_TERMS_N = tuple(normalize_text(t) for t in CALL_CORE_TERMS)
_NEGATIVE_TERMS_N = tuple(normalize_text(t) for t in NEGATIVE_TERMS)
_STRONG_NEGATIVE_TERMS_N = tuple(normalize_text(t) for t in STRONG_NEGATIVE_TERMS)
_INTERNAL_TERMS_N = tuple(normalize_text(t) for t in INTERNAL_TERMS)
_EXTERNAL_TERMS_N = tuple(normalize_text(t) for t in EXTERNAL_TERMS)


class OpportunityPrefilter:
    """Fast first-pass gate on title+category.

    Strong project/opportunity terms pass directly. Weak procurement/school words
    only pass when the title/category also carries a D.M. 219 / AI project signal.
    """

    def __init__(self) -> None:
        self._strong = self._compile(STRONG_PREFILTER_TERMS)
        self._project = self._compile(PROJECT_CONTEXT_TERMS)
        self._weak = self._compile(WEAK_PREFILTER_TERMS + CALL_TERMS)

    @staticmethod
    def _compile(terms: tuple[str, ...]) -> re.Pattern[str]:
        normalized = {normalize_text(t) for t in terms}
        pattern = "|".join(re.escape(t) for t in sorted(normalized, key=len, reverse=True))
        return re.compile(pattern, re.IGNORECASE)

    def is_relevant(self, item: AlboItem) -> bool:
        text = normalize_text(f"{item.title} {item.category}")
        return bool(self._strong.search(text) or (self._project.search(text) and self._weak.search(text)))


class OpportunityVerifier:
    # "decreto di assegnazione AL DIRIGENTE SCOLASTICO dell'incarico di..." -
    # confirmed live (NAPS110002): the role sits BETWEEN "assegnazione" and
    # "dell'incarico", so no fixed-phrase term in STRONG_NEGATIVE_TERMS can
    # match it (and a bare "decreto di assegnazione" would wrongly also
    # match the funding-award decree cited in every document's preamble -
    # see the comment on that list). A bounded-word-gap regex catches the
    # real self-appointment pattern without that false-positive risk.
    _ASSIGNMENT_OF_ROLE_RE = re.compile(r"decreto di assegnazione\b(?:\s+\S+){0,6}\s+dell.incarico")

    # "external only if internal search comes up empty" is standard boilerplate
    # in an internal-only avviso ("sarà a discrezione del DS indire nuovo avviso
    # oppure ricercare all'esterno la figura professionale mancante" / "di
    # reiterare l'avviso interno ovvero di adottare sistemi di reclutamento per
    # le figure mancanti, all'esterno della istituzione scolastica" / the D.Lgs
    # 165/2001 art. 7 c. 6 "esigenza di ricorrere a soggetti esterni" citation) -
    # confirmed live across 7 different schools nationwide, clearly a shared
    # template, not free text. A prior attempt to reject on this phrase alone
    # was reverted (see below) because the SAME sentence also appears in
    # Caselette's notice, which is genuinely open to external candidates.
    _EXTERNAL_AS_FALLBACK_ONLY_RE = re.compile(
        r"indire nuovo avviso oppure ricercare all.esterno"
        r"|per le figure mancanti,?\s*all.esterno della istituzione scolastica"
        r"|esigenza di ricorrere a soggetti esterni"
    )
    # What actually distinguishes genuinely-open documents from the 7
    # internal-only false positives above: they explicitly rank/admit
    # external candidates in the SAME process, either via Caselette's phrasing
    # ("priorità a favore dei candidati interni... e, solo in via subordinata,
    # dei candidati ESTERNI RISULTATI AMMESSI"; "per esterni: partita iva o
    # disponibilità a contratto...") or via CZIS022003/TOIC87000N's phrasing
    # ("rivolto a figure professionali interne ed esterne... in collaborazione
    # plurima o come lavoro autonomo/prestazione occasionale" — a live,
    # confirmed miss on first pass: TOIC87000N shares this exact subject-line
    # template and was wrongly caught as internal-only until this was added).
    # None of the 7 false positives contain any of this - they only describe
    # an escalation PROCEDURE if the internal pool is empty, never actually
    # evaluating an external candidate. So the fallback phrase above only
    # means "internal-only" when none of these are present.
    _GENUINE_EXTERNAL_ELIGIBILITY_RE = re.compile(
        r"in via subordinata|risultati ammessi|per esterni\s*:|partita iva"
        r"|lavoro autonomo|prestazione occasionale|interne ed esterne|interni ed esterni"
    )

    # A generic self-identification checkbox on an application form ("this
    # candidate is: an employee of this school / another school / another
    # P.A. / an external expert") lists every possible applicant category
    # regardless of whether THIS notice actually invites external candidates
    # - it's a data field, not a policy statement, so its mere presence
    # proves nothing about eligibility. Confirmed live: SRPC08000R's own
    # scoring criteria only had a "Per le figure interne" tier (no external
    # counterpart), yet this checkbox alone tripped external_hits. Genuinely
    # open documents state real REQUIREMENTS for external candidates instead
    # (Caselette: "per esterni: partita iva o disponibilità a contratto...")
    # - none of the verified genuine matches rely on this checkbox phrase.
    _STATUS_DECLARATION_CHECKBOX_RE = re.compile(
        r"dipendente di altra p\.a\.,?\s*(?:o|ovvero)\s*se\s*[eè]\s*esperto esterno"
    )

    # How much of the (title + metadata + filenames + body) text counts as the
    # document's "kind" signal for STRONG_NEGATIVE_TERMS. Adapters put the title,
    # descrizione/tipologia, and attachment filenames FIRST, deep attachment body
    # text after — a genuinely open avviso routinely explains its own process in
    # completely normal terms ("seguendo l'ordine di graduatoria...", "l'istituzione
    # può stipulare contratti...") deep in its body, which is not evidence the
    # process has concluded. Matching STRONG_NEGATIVE_TERMS against the full text
    # falsely rejects those; matching only this early window catches the real
    # signals (a closed-award filename, an admin-attachment title) without them.
    KIND_SIGNAL_CHARS = 2000

    def verify(self, candidate: CandidateDocument) -> VerificationResult:
        text = self._normalize(f"{candidate.title} {candidate.url} {candidate.text}")
        kind_text = self._normalize(f"{candidate.title} {candidate.url} {candidate.text[:self.KIND_SIGNAL_CHARS]}")
        project = candidate.project
        exact_hits = [term for term in (normalize_text(project.cup), normalize_text(project.clp)) if term and term in text]
        context_hits = [term for term in _PROJECT_CONTEXT_TERMS_N if term in text]
        call_hits = [term for term in _CALL_TERMS_N if term in text]
        core_call_hits = [term for term in _CALL_CORE_TERMS_N if term in text]
        negative_hits = [term for term in _NEGATIVE_TERMS_N if term in kind_text]
        strong_neg = [term for term in _STRONG_NEGATIVE_TERMS_N if term in kind_text]
        if self._ASSIGNMENT_OF_ROLE_RE.search(kind_text):
            strong_neg.append("decreto di assegnazione ... dell'incarico")
        internal_hits = [term for term in _INTERNAL_TERMS_N if term in text]
        text_for_external = self._STATUS_DECLARATION_CHECKBOX_RE.sub(" ", text)
        external_hits = [term for term in _EXTERNAL_TERMS_N if term in text_for_external]
        internal_only = bool(internal_hits) and not external_hits
        if (
            internal_hits and external_hits and not internal_only
            and self._EXTERNAL_AS_FALLBACK_ONLY_RE.search(text)
            and not self._GENUINE_EXTERNAL_ELIGIBILITY_RE.search(text)
        ):
            internal_only = True

        if strong_neg and not core_call_hits:
            return VerificationResult(False, Confidence.LOW, f"Closed/already-decided, contract-stage, or admin act ({', '.join(strong_neg[:3])}).")
        if strong_neg and core_call_hits:
            return VerificationResult(False, Confidence.LOW, f"Mixed with closed/admin terms ({', '.join(strong_neg[:3])}); requires a cleaner open-call document.")
        if internal_only:
            return VerificationResult(False, Confidence.LOW, f"Selection appears reserved to internal staff ({', '.join(internal_hits[:3])}).")
        if negative_hits and not core_call_hits:
            return VerificationResult(False, Confidence.LOW, f"Looks like non-opportunity document: {', '.join(negative_hits[:3])}.")
        if not core_call_hits:
            return VerificationResult(False, Confidence.LOW, "No clear open-call/procurement language found.")
        if exact_hits:
            # An exact CUP/CLP match is strong evidence this document is
            # about the right project, but says nothing about whether
            # internal/external framing is ambiguous. A document mixing both
            # internal-staff AND external language (e.g. "external only if
            # no internal candidate is found") needs AI's actual semantic
            # judgment, not a rule-only HIGH-confidence bypass — downgrading
            # to MEDIUM here routes it through the ai_unavailable_deferred
            # path (retry later) instead of alerting on rule-only confidence
            # when AI happens to be unavailable for this specific document.
            mixed_signal = bool(internal_hits and external_hits)
            confidence = Confidence.MEDIUM if mixed_signal else Confidence.HIGH
            return VerificationResult(
                True, confidence,
                f"Identificativo di progetto esatto trovato ({', '.join(exact_hits)}) insieme a termini di bando ({', '.join(core_call_hits[:3])}).",
                core_call_hits[0],
                ambiguous_internal_external=mixed_signal,
            )
        # No exact CUP/CLP citation, so this is judged purely on how many
        # distinct D.M. 219 signals ("219", "intelligenza artificiale", "snodi
        # formativi", ...) show up alongside the call language. There is no
        # LOW tier here anymore: a single weak mention isn't reliable evidence
        # on its own (confirmed live: a school BUYING an AI training course
        # matched on one bare "intelligenza artificiale" hit) and, since this
        # confidence can be sent without AI confirmation when AI is
        # unavailable, only a genuinely strong rule-only signal should qualify.
        # Real DM 219 notices overwhelmingly cite the CUP directly (the exact_hits
        # branch above), so this path firing at all should be rare - confirmed
        # against 50 real live schools: 23/26 non-CUP candidates had zero
        # context hits, only 1 had 2, none had 3+.
        if len(context_hits) >= 3:
            return VerificationResult(True, Confidence.MEDIUM, f"Forte contesto D.M. 219 sull'IA ({', '.join(context_hits[:3])}) con termini di bando ({', '.join(core_call_hits[:3])}).", core_call_hits[0])
        return VerificationResult(False, Confidence.LOW, f"Linguaggio di bando presente ({', '.join(core_call_hits[:3])}) ma collegamento al progetto insufficiente ({len(context_hits)} riferimento/i) - probabilmente un altro programma.")

    def _normalize(self, value: str) -> str:
        return normalize_text(value)


AI_OFF = "off"
AI_CAPPED = "capped"
AI_FULL = "full"
AI_MODES = (AI_OFF, AI_CAPPED, AI_FULL)


class AiVerifier:
    """Second-opinion judge backed by Gemini. The *mode* controls how many AI
    calls are spent, so the same code runs free (off/capped) or fully (paid)."""

    # A 429/RESOURCE_EXHAUSTED from Gemini is usually a per-minute rate limit
    # (common on free-tier keys), not a hard daily quota — permanently giving
    # up on AI for the rest of a multi-hour run over one transient 429 would
    # silently degrade thousands of schools to rule-only. Back off instead,
    # with the cooldown doubling on repeated failures (capped) so a genuinely
    # exhausted daily quota doesn't get hammered with retries forever.
    _BASE_COOLDOWN_SECONDS = 60.0
    _MAX_COOLDOWN_SECONDS = 300.0

    def __init__(self, mode: str = AI_CAPPED, budget: int = 200) -> None:
        if mode not in AI_MODES:
            raise ValueError(f"ai mode must be one of {AI_MODES}, got {mode!r}")
        self.mode = mode
        self._remaining = budget
        self._cooldown_until = 0.0
        self._cooldown_seconds = self._BASE_COOLDOWN_SECONDS
        self._last_error: str | None = None
        self.skipped_in_cooldown = 0
        self._lock = Lock()

    @property
    def budget_left(self) -> int:
        return self._remaining

    @property
    def last_rate_limit_error(self) -> str | None:
        return self._last_error

    def verify(self, candidate: CandidateDocument, rule_result: VerificationResult) -> VerificationResult:
        if self.mode == AI_OFF:
            return rule_result
        if not os.getenv("GEMINI_API_KEY"):
            return rule_result
        with self._lock:
            if time.monotonic() < self._cooldown_until:
                self.skipped_in_cooldown += 1
                # ai_error (not a bare rule_result) so monitor.py's
                # ai_unavailable_deferred path retries this on a future run
                # instead of permanently finalizing on rule-only confidence —
                # a cooldown recovers within minutes, this isn't a settled
                # "AI won't be consulted" decision like mode=off/no key.
                return replace(rule_result, ai_error="AI in cooldown after a recent rate limit")
        try:
            from google import genai
        except Exception as exc:
            return replace(rule_result, ai_error=f"genai import failed: {exc}")
        prompt = f'''
You are an expert procurement analyst for an AI-training consulting firm. You read an Italian school's published notice (Albo Pretorio / Amministrazione Trasparente) and decide whether it is a real opportunity for our firm to bid on as an EXTERNAL provider.

Return MATCH only if ALL hold:
1. It is a CURRENTLY OPEN call/tender/selection/market survey (avviso pubblico, selezione, manifestazione di interesse, indagine di mercato, RDO/MePA, affidamento) - not an already-closed/awarded/ranked one.
2. It seeks an EXTERNAL party UNCONDITIONALLY, from the outset: "esperto esterno", "operatori economici", "collaborazione plurima", an open market/company tender.
   -> Return IGNORE if it is reserved to INTERNAL staff only ("docenti interni", "personale interno", "in servizio presso questa istituzione").
   -> Also return IGNORE if external candidates are admitted only as a FALLBACK/contingency — e.g. "in mancanza di candidature interne", "qualora non si rinvenga personale interno", "solo in caso di indisponibilità di personale interno" — i.e. internal hiring is the real plan and external is merely a contingency clause, not a present offer. This school is very likely to publish a separate, clearer notice later if the fallback is ever actually triggered; that notice — not this contingency clause — is the real opportunity to catch.
3. It plausibly concerns AI / digital / PNRR training services this firm could deliver - ideally this project (CUP/CLP below), but accept clear D.M. 219 / "intelligenza artificiale" / "snodi formativi" / digital-skills training even if the CUP is not literally printed.

Return IGNORE for: news/dissemination, internal-only appointments, graduatorie/verbali/aggiudicazioni (results), RUP/accounting acts, contract-stage documents, pure goods purchases unrelated to training, and unrelated projects.

Target project:
  School: {candidate.project.school_name} ({candidate.project.school_code}), {candidate.project.region}
  CUP: {candidate.project.cup}   CLP: {candidate.project.clp}
  Programme: D.M. 219/2025 - Avviso 73226/2026 - Snodi formativi sull'intelligenza artificiale
Notice title: {candidate.title}
Notice URL: {candidate.url}
Rule pre-check: {rule_result.reason}

FULL NOTICE TEXT (may include multiple attachments):
"""
{candidate.text[:20000]}
"""

Think about open-vs-closed and internal-vs-external carefully. The "opportunity type" and "reason" fields are shown to an Italian-speaking user, so write THEM IN ITALIAN. Keep the MATCH/IGNORE keyword and the confidence word (high/medium/low) in English. Respond on ONE line, exactly:
MATCH | confidence high/medium/low | tipo di opportunità (in italiano) | scadenza (GG/MM/AAAA o "non specificata") | motivazione in una frase (in italiano)
or
IGNORE | motivo (in italiano)
'''
        try:
            with self._lock:
                if time.monotonic() < self._cooldown_until:
                    self.skipped_in_cooldown += 1
                    return replace(rule_result, ai_error="AI in cooldown after a recent rate limit")
                if self.mode == AI_CAPPED:
                    if self._remaining <= 0:
                        # Budget resets next run — same reasoning as cooldown
                        # above: this is "not right now", not "never".
                        return replace(rule_result, ai_error="AI budget exhausted for this run")
                    self._remaining -= 1
                client = genai.Client()
                response = client.models.generate_content(model="gemini-3-flash-preview", contents=prompt)
            answer = (response.text or "").strip()
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {str(exc)[:120]}"
            if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text:
                with self._lock:
                    self._last_error = error_text
                    self._cooldown_until = time.monotonic() + self._cooldown_seconds
                    self._cooldown_seconds = min(self._cooldown_seconds * 2, self._MAX_COOLDOWN_SECONDS)
            return replace(rule_result, ai_error=error_text)
        with self._lock:
            # A successful call means the rate limit has cleared - reset the
            # backoff so the next 429 (if any) starts from the short cooldown
            # again instead of staying pinned at the max from an earlier spike.
            self._cooldown_seconds = self._BASE_COOLDOWN_SECONDS
        if not answer.upper().startswith("MATCH"):
            return VerificationResult(False, Confidence.LOW, f"AI rejected candidate: {answer}", ai_used=True)
        parts = [part.strip() for part in answer.split("|")]
        confidence = Confidence(parts[1].lower()) if len(parts) > 1 and parts[1].lower() in {"high", "medium", "low"} else Confidence.MEDIUM
        return VerificationResult(True, confidence, parts[4] if len(parts) > 4 else answer, parts[2] if len(parts) > 2 else rule_result.opportunity_type, parts[3] if len(parts) > 3 else "non specificata", True)

    def _call_gemini_raw(self, prompt: str) -> str | None:
        """Shared low-level Gemini call for the narrow disambiguation methods
        below — same budget/cooldown/backoff gating as verify(), just
        returning the raw answer (or None if unavailable) instead of a
        VerificationResult, since these callers have no rule_result to fall
        back to."""
        if self.mode == AI_OFF or not os.getenv("GEMINI_API_KEY"):
            return None
        try:
            from google import genai
        except Exception:
            return None
        try:
            with self._lock:
                if time.monotonic() < self._cooldown_until:
                    self.skipped_in_cooldown += 1
                    return None
                if self.mode == AI_CAPPED:
                    if self._remaining <= 0:
                        return None
                    self._remaining -= 1
                client = genai.Client()
                response = client.models.generate_content(model="gemini-3-flash-preview", contents=prompt)
            answer = (response.text or "").strip()
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {str(exc)[:120]}"
            if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text:
                with self._lock:
                    self._last_error = error_text
                    self._cooldown_until = time.monotonic() + self._cooldown_seconds
                    self._cooldown_seconds = min(self._cooldown_seconds * 2, self._MAX_COOLDOWN_SECONDS)
            return None
        with self._lock:
            self._cooldown_seconds = self._BASE_COOLDOWN_SECONDS
        return answer

    def same_process(self, text_a: str, text_b: str) -> bool | None:
        """Are these two documents about the same specific selection process
        (companion documents — e.g. a Decreto and its Avviso) rather than two
        genuinely different calls that happen to share a funded project?
        Only invoked for the rare case where rule-based signals (protocol
        citation, role overlap + publish-date proximity) couldn't decide on
        their own. Shares this verifier's existing budget/cooldown pool."""
        prompt = f'''Sei un analista che confronta due documenti amministrativi pubblicati dalla stessa scuola italiana per lo stesso progetto finanziato (stesso CUP). Decidi se descrivono LO STESSO processo di selezione (es. un decreto di avvio e il relativo avviso pubblico) oppure due processi DIVERSI (es. ruoli diversi, selezioni distinte).

DOCUMENTO A:
"""
{text_a[:4000]}
"""

DOCUMENTO B:
"""
{text_b[:4000]}
"""

Rispondi con UNA sola parola: STESSO oppure DIVERSO.'''
        answer = self._call_gemini_raw(prompt)
        return answer.strip().upper().startswith("STESSO") if answer is not None else None

    def is_actionable_content(self, text: str) -> bool | None:
        """Does this document give an external candidate concrete steps to
        apply (deadline, how/where to submit), or is it primarily an internal
        authorization act that merely references the process? Only invoked
        when a cheap rule check on the text couldn't tell."""
        prompt = f'''Sei un analista che legge un documento amministrativo di una scuola italiana. Decidi se il testo fornisce a un candidato ESTERNO le informazioni concrete per candidarsi (termine di scadenza, modalità/indirizzo di invio della domanda), oppure se è principalmente un atto di autorizzazione interna che si limita a citare il processo senza spiegare come partecipare.

TESTO:
"""
{text[:6000]}
"""

Rispondi con UNA sola parola: AZIONABILE oppure SOLO_AUTORIZZAZIONE.'''
        answer = self._call_gemini_raw(prompt)
        return answer.strip().upper().startswith("AZIONABILE") if answer is not None else None
