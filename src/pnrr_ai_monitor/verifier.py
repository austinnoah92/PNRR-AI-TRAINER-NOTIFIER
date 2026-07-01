from __future__ import annotations

import os
import re
from dataclasses import replace
from threading import Lock

from .models import AlboItem, CandidateDocument, Confidence, VerificationResult


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
    "affidamento diretto", "servizi di formazione", "rdo", "mepa", "capitolato",
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
    "rdo", "mepa", "istanza di partecipazione", "domanda di partecipazione",
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
    "stipula", "fornitura", "forniture", "acquisto", "consumabili", "cancelleria",
    "materiale igienico", "elettrodi", "cambridge", "accordo sindacale",
    "riunioni e collegi", "commissione liceo", "classe", "studenti", "studente",
    "acfdg", "acprot",
    "nomina rup", "incarico rup", "supporto tecnico", "supporto al rup", "supporto tecnico al rup",
    "responsabile unico del progetto", "assunzione in bilancio", "assunzione al bilancio",
    "decreto assunzione in bilancio", "decreto assunzione al bilancio",
    "disseminazione", "azione di disseminazione", "azione di disseminazione iniziale",
    "pubblicita", "lettera di incarico", "decreto di incarico", "decreto di nomina",
    "conferimento incarico", "conferimento incarich", "aggiudicazione", "determina di aggiudicazione",
    "determinazione di aggiudicazione", "determina cuc", "determinazione cuc",
    "dichiarazione insussistenza", "insussistenza", "schema di contratto", "contratto",
)

# Acts that are clearly NOT an open opportunity even if they cite the project CUP:
# already decided/awarded, internal admin, pure publicity, or contract-stage docs.
STRONG_NEGATIVE_TERMS = (
    "graduatoria", "graduat", "pubblicazione graduatoria", "pubblicaz. graduat", "aggiudicazione",
    "verbale", "verbale apertura", "apertura candidature", "nomina commissione",
    "determina", "determinazione", "decisione a contrarre", "decisione affid",
    "stipula", "fornitura", "forniture", "acquisto", "consumabili",
    "materiale igienico", "elettrodi", "cambridge", "accordo sindacale",
    "riunioni e collegi", "commissione liceo", "classe", "studenti", "studente",
    "acfdg", "acprot",
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
    "responsabile unico del progetto", "dichiarazione insussistenza", "insussistenza",
    "determina cuc", "determinazione cuc", "schema di contratto", "contratto",
    # Standalone procedural/compliance attachments (e.g. a RUP's conflict-of-interest
    # declaration) — routinely carry the project's CUP for traceability, but are
    # paperwork, not a call anyone can respond to.
    "dichiarazione di inesistenza", "inesistenza di cause", "inesistenza di conflitto",
    "conflitto di interessi", "conflitto d'interessi", "conflitto di interesse",
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
    "soggetti giuridici", "soggetti qualificati", "manifestazione di interesse",
    "collaborazione plurima", "collaborazione esterna", "mepa",
    # NOT bare "rdo": it's a 3-letter substring of common Italian words
    # ("riguardo", "accordo", "ricordo", "assurdo"...) — in any document of real
    # length it matches by accident and defeats the internal-only rejection this
    # list exists to support. (Confirmed: matched inside "riguardo" in a real
    # notice, silently turning an internal-only avviso into a false "external".)
)

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
        pattern = "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
        return re.compile(pattern, re.IGNORECASE)

    def is_relevant(self, item: AlboItem) -> bool:
        text = f"{item.title} {item.category}"
        return bool(self._strong.search(text) or (self._project.search(text) and self._weak.search(text)))


class OpportunityVerifier:
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
        exact_hits = [term for term in (project.cup.lower(), project.clp.lower()) if term and term in text]
        context_hits = [term for term in PROJECT_CONTEXT_TERMS if term in text]
        call_hits = [term for term in CALL_TERMS if term in text]
        core_call_hits = [term for term in CALL_CORE_TERMS if term in text]
        negative_hits = [term for term in NEGATIVE_TERMS if term in kind_text]
        strong_neg = [term for term in STRONG_NEGATIVE_TERMS if term in kind_text]
        internal_hits = [term for term in INTERNAL_TERMS if term in text]
        external_hits = [term for term in EXTERNAL_TERMS if term in text]
        internal_only = bool(internal_hits) and not external_hits

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
            return VerificationResult(True, Confidence.HIGH, f"Identificativo di progetto esatto trovato ({', '.join(exact_hits)}) insieme a termini di bando ({', '.join(core_call_hits[:3])}).", core_call_hits[0])
        if len(context_hits) >= 2:
            return VerificationResult(True, Confidence.MEDIUM, f"Forte contesto D.M. 219 sull'IA ({', '.join(context_hits[:3])}) con termini di bando ({', '.join(core_call_hits[:3])}).", core_call_hits[0])
        return VerificationResult(True, Confidence.LOW, f"Linguaggio di bando presente ({', '.join(core_call_hits[:3])}) ma collegamento al progetto debole - richiede valutazione AI.", core_call_hits[0])

    def _normalize(self, value: str) -> str:
        return " ".join(value.lower().split())


AI_OFF = "off"
AI_CAPPED = "capped"
AI_FULL = "full"
AI_MODES = (AI_OFF, AI_CAPPED, AI_FULL)


class AiVerifier:
    """Second-opinion judge backed by Gemini. The *mode* controls how many AI
    calls are spent, so the same code runs free (off/capped) or fully (paid)."""

    def __init__(self, mode: str = AI_CAPPED, budget: int = 50) -> None:
        if mode not in AI_MODES:
            raise ValueError(f"ai mode must be one of {AI_MODES}, got {mode!r}")
        self.mode = mode
        self._remaining = budget
        self._disabled_reason: str | None = None
        self._lock = Lock()

    @property
    def budget_left(self) -> int:
        return self._remaining

    def verify(self, candidate: CandidateDocument, rule_result: VerificationResult) -> VerificationResult:
        if self.mode == AI_OFF:
            return rule_result
        if not os.getenv("GEMINI_API_KEY"):
            return rule_result
        with self._lock:
            if self._disabled_reason:
                return rule_result
        try:
            from google import genai
        except Exception as exc:
            return replace(rule_result, ai_error=f"genai import failed: {exc}")
        prompt = f'''
You are an expert procurement analyst for an AI-training consulting firm. You read an Italian school's published notice (Albo Pretorio / Amministrazione Trasparente) and decide whether it is a real opportunity for our firm to bid on as an EXTERNAL provider.

Return MATCH only if ALL hold:
1. It is a CURRENTLY OPEN call/tender/selection/market survey (avviso pubblico, selezione, manifestazione di interesse, indagine di mercato, RDO/MePA, affidamento) - not an already-closed/awarded/ranked one.
2. It seeks an EXTERNAL party: "esperto esterno", "operatori economici", "collaborazione plurima", an open market/company tender.
   -> Return IGNORE if it is reserved to INTERNAL staff only ("docenti interni", "personale interno", "in servizio presso questa istituzione"), unless external candidates are explicitly admitted (e.g. only if no internal candidate is found).
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
                if self._disabled_reason:
                    return rule_result
                if self.mode == AI_CAPPED:
                    if self._remaining <= 0:
                        return rule_result
                    self._remaining -= 1
                client = genai.Client()
                response = client.models.generate_content(model="gemini-3-flash-preview", contents=prompt)
            answer = (response.text or "").strip()
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {str(exc)[:120]}"
            if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text:
                with self._lock:
                    self._disabled_reason = error_text
            return replace(rule_result, ai_error=error_text)
        if not answer.upper().startswith("MATCH"):
            return VerificationResult(False, Confidence.LOW, f"AI rejected candidate: {answer}", ai_used=True)
        parts = [part.strip() for part in answer.split("|")]
        confidence = Confidence(parts[1].lower()) if len(parts) > 1 and parts[1].lower() in {"high", "medium", "low"} else Confidence.MEDIUM
        return VerificationResult(True, confidence, parts[4] if len(parts) > 4 else answer, parts[2] if len(parts) > 2 else rule_result.opportunity_type, parts[3] if len(parts) > 3 else "non specificata", True)
