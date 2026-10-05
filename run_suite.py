"""Run the acceptance suite against the live RAG pipeline and score the results.

Usage:  .venv/bin/python run_suite.py [--only SUBSTRING] [--full] [--min-pass-rate N]

Exits non-zero when the pass rate falls below --min-pass-rate, so this doubles
as a CI gate. Needs a live database with the suite document indexed and a
configured LLM provider.
"""
from __future__ import annotations

import re
import sys
import time

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.model_registry import import_all_models

import_all_models()

from app.infrastructure.ai.embeddings.factory import create_embedder
from app.infrastructure.ai.llm.factory import create_llm
from app.infrastructure.ai.reranker.factory import create_reranker
from app.infrastructure.cache.factory import create_cache
from app.modules.auth.dependencies import principal_from_user
from app.modules.rag.schema import AskRequest, ConversationTurn
from app.modules.rag.service import RAGService
from app.modules.documents.model import Document
from app.modules.users.model import User

# The suite asks about "this document": scope every question to it, as the UI
# does when a person chats with one document.
SUITE_DOCUMENT_TITLE = "National Electricity Plan%"

# (id, section, question, [required substrings], [forbidden substrings])
# A required entry may be a tuple of acceptable alternatives.
# A case passes when the status is answered AND every required token is present
# in the answer. Forbidden tokens catch wrong-but-plausible answers.
CASES = [
    # --- 1. basic retrieval -------------------------------------------------
    ("1.1", "basic", "What is the name of this document?",
     ["national electricity plan"], []),
    ("1.2", "basic", "Who published the National Electricity Plan Volume II Transmission?",
     ["central electricity authority", "ministry of power"], []),
    ("1.3", "basic", "When was this plan published?",
     ["october", "2024"], []),
    ("1.4", "basic", "Under which Act was the plan prepared?",
     ["electricity act", "2003"], []),
    ("1.5", "basic", "Which section of the Electricity Act is mentioned?",
     ["3(4)"], []),
    ("1.6", "basic", "What periods does the plan cover?",
     ["2017", "2022", "2027"], []),
    ("1.7", "basic", "What is Chapter 3 about?",
     ["planning philosophy"], []),
    ("1.8", "basic", "What is Chapter 4 about?",
     ["cyber security"], []),
    ("1.9", "basic", "What is Chapter 5 about?",
     ["analysis and studies", "2026"], []),
    ("1.10", "basic", "What is Chapter 8 about?",
     ["perspective", "2027"], []),

    # --- 2. acronyms --------------------------------------------------------
    ("2.1", "acronym", "What does BESS stand for?", ["battery energy storage"], []),
    ("2.2", "acronym", "What does HVDC stand for?", ["high voltage direct current"], []),
    ("2.3", "acronym", "What does HVAC stand for?", ["high voltage alternating current"], []),
    ("2.4", "acronym", "What does STATCOM stand for?", ["static compensator"], []),
    ("2.5", "acronym", "What does SVC stand for?", ["static var compensator"], []),
    ("2.6", "acronym", "What does FACTS stand for?",
     ["flexible alternating current transmission"], []),
    ("2.7", "acronym", "What does ISTS stand for?", ["inter state transmission"], []),
    ("2.8", "acronym", "What does CTU stand for?", ["central transmission utility"], []),
    ("2.9", "acronym", "What does STU stand for?", ["state transmission utility"], []),
    ("2.10", "acronym", "What does TBCB stand for?", ["tariff based competitive bidding"], []),
    ("2.11", "acronym", "What does OSOWOG stand for?",
     ["one sun one world one grid"], []),
    ("2.12", "acronym", "What does REZ stand for?", ["renewable energy zone"], []),
    ("2.13", "acronym", "What does DLR stand for?", ["dynamic line rating"], []),
    ("2.14", "acronym", "What does PMU stand for?", ["phasor measurement unit"], []),
    ("2.15", "acronym", "What does GEC stand for?", ["green energy corridor"], []),

    # --- 3. exact numbers ---------------------------------------------------
    ("3.1", "number", "What was the estimated cost of the 170 transmission schemes mentioned in the comments section?",
     ["3,13,950", "1,61,854"], []),
    ("3.2", "number", "How many transmission schemes were mentioned in that discussion?",
     ["170"], []),
    ("3.3", "number", "What renewable-energy capacity does the transmission system aim to integrate?",
     ["600", "gw"], []),
    ("3.4", "number", "What is the planned non-fossil electricity generation capacity for 2030?",
     ["500", "gw"], []),
    ("3.5", "number", "What is the planned rooftop solar capacity for 2032?",
     ["60"], []),

    # --- 4. reasoning -------------------------------------------------------
    ("4.1", "reasoning", "Why does the plan say transmission infrastructure is needed for renewable energy?",
     ["load cent"], []),
    ("4.2", "reasoning", "Why can't Kerala and Goa meet their entire electricity demand using only their own renewable resources?",
     ["seasonal", "national grid"], []),
    ("4.3", "reasoning", "What does the plan say about transmission lines passing through forests?",
     [("non forest", "no other option")], []),
    ("4.4", "reasoning", "Why are both BESS and pumped-storage plants considered?",
     ["bess", "pumped"], []),

    # --- 5. multi-hop -------------------------------------------------------
    ("5.1", "multihop", "What renewable-energy integration challenges does the plan identify, and which technologies address them?",
     ["statcom", "bess"], []),
    ("5.2", "multihop", "What scenarios were considered for the 2026-27 transmission studies?",
     ["nine", "june"], []),
    ("5.3", "multihop", "Why are planning margins used in transmission planning?",
     ["uncertaint"], []),

    # --- 6. contradiction / source grounding --------------------------------
    ("6.1", "grounding", "Did the document agree that rooftop solar alone could provide 70-80% of India's annual electricity demand?",
     ["shankar sharma", "cannot"], []),

    # --- 7. who said this ---------------------------------------------------
    ("7.1", "attribution", "Who suggested that rooftop solar could contribute more than 70-80% of annual electrical energy?",
     ["shankar sharma"], []),
    ("7.2", "attribution", "Who suggested changing the voltage margin from +5% to +10%?",
     ["kptcl"], []),
    ("7.3", "attribution", "Who suggested extending the transmission-planning horizon from 3-5 years to 5-10 years?",
     ["adani"], ["kptcl"]),
    ("7.4", "attribution", "Who suggested consideration of additional scenarios involving seasonal demand, energy storage and time-of-day tariffs?",
     ["prayas"], []),

    # --- 8. refusal ---------------------------------------------------------
    ("8.1", "refusal", "What is India's electricity demand in 2050?",
     [], ["2050 mw", "2050 gw"]),

    # --- 9. metadata --------------------------------------------------------
    ("9.1", "metadata", "What is the exact title, issuing authority, publication date, planning horizon and legal basis of this document?",
     ["central electricity authority", "october", "2003", ("3(4)", "sub section(4) of section 3"), "2027"], []),
]

# Cases that must NOT be answered.
MUST_REFUSE = [
    ("8.1", "refusal", "What is India's electricity demand in 2050?"),
]


def norm(text: str) -> str:
    """Lower-case, hyphens as spaces ("non-forest"), "3 (4)" as "3(4)"."""
    text = (text or "").lower().replace("-", " ").replace("–", " ")
    return re.sub(r"\s+", " ", re.sub(r"\s+\(", "(", text))


# Follow-up questions are asked with the earlier turn as conversation history,
# as the assistant UI sends it.
FOLLOW_UPS = {"3.2": "3.1"}


def run(cases, only=None, show_all=False):
    answers: dict[str, tuple[str, str | None]] = {}
    settings = get_settings()
    engine = create_db_engine(settings)
    sf = create_session_factory(engine)
    embedder, llm, reranker, cache = (
        create_embedder(settings), create_llm(settings),
        create_reranker(settings), create_cache(settings),
    )
    results = []
    with sf() as ses:
        user = ses.scalars(
            select(User).where(User.is_active.is_(True), User.organization_id.is_not(None)).order_by(User.role)
        ).first()
        principal = principal_from_user(user, ses)
        policy_id = ses.scalar(
            select(Document.policy_id).where(Document.title.ilike(SUITE_DOCUMENT_TITLE), Document.status == "ready")
        )
        svc = RAGService(ses, sf, settings, cache, embedder=embedder,
                         reranker=reranker, llm_factory=lambda: llm)

        for cid, section, question, required, forbidden in cases:
            if only and only.lower() not in question.lower() and only != section:
                continue
            started = time.perf_counter()
            try:
                previous = FOLLOW_UPS.get(cid)
                history = [ConversationTurn(question=answers[previous][0], answer=answers[previous][1])] \
                    if previous in answers else []
                r = svc.ask(principal, AskRequest(question=question, history=history,
                                                  policy_ids=[policy_id] if policy_id else []))
                answers[cid] = (question, r.answer)
            except Exception as exc:  # surface crashes, do not hide them
                print(f"[{cid}] CRASH {type(exc).__name__}: {exc}")
                results.append((cid, section, question, "CRASH", "", r"", 0.0))
                continue
            elapsed = time.perf_counter() - started
            answer = norm(r.answer)
            reason = r.no_answer.reason if r.no_answer else None
            cited = f"p{src.page_start}" if (r.sources and (src := r.sources[0]).page_start) else "-"

            if (cid, section, question) in [(c[0], c[1], c[2]) for c in MUST_REFUSE]:
                ok = r.status == "no_answer"
                verdict = "PASS" if ok else "FAIL"
            elif r.status != "answered":
                verdict = "FAIL"
            else:
                missing = [t for t in required if not any(alt in answer for alt in (t if isinstance(t, tuple) else (t,)))]
                bad = [t for t in forbidden if t in answer]
                verdict = "PASS" if not missing and not bad else "FAIL"
                if missing and show_all:
                    verdict += f" (missing {missing})"
                if bad and show_all:
                    verdict += f" (forbidden {bad})"

            results.append((cid, section, question, verdict, reason or "", cited, elapsed))
            flag = "OK " if verdict == "PASS" else "!! "
            print(f"{flag}[{cid:5}] {verdict:5} {elapsed:6.1f}s {cited:6} {r.status:9} "
                  f"{reason or '':22} {question[:62]}")
            if verdict != "PASS" or show_all:
                print(f"          -> {(r.answer or '(no answer)')[:400]}")
    return results


def summarize(results):
    print("\n" + "=" * 100)
    total = len(results)
    passed = sum(1 for x in results if x[3] == "PASS")
    if not total:
        print("No cases matched the filter.")
        return 0, 0
    print(f"PASSED {passed}/{total}  ({passed / total * 100:.0f}%)")
    by_section: dict[str, list[bool]] = {}
    for cid, section, _q, verdict, *_ in results:
        by_section.setdefault(section, []).append(verdict == "PASS")
    print("\nby section:")
    for section, oks in by_section.items():
        mark = "OK " if all(oks) else "!! "
        print(f"  {mark}{section:12} {sum(oks)}/{len(oks)}")
    fails = [x for x in results if x[3] != "PASS"]
    if fails:
        print("\nfailures:")
        for cid, section, question, verdict, reason, _cited, _t in fails:
            print(f"  [{cid}] {section}: {question}")
            print(f"        {verdict} status_reason={reason}")
    return passed, total


def main(argv):
    only = None
    show_all = "--full" in argv
    if "--only" in argv:
        only = argv[argv.index("--only") + 1]
    passed, total = summarize(run(CASES, only=only, show_all=show_all))
    # Exit non-zero below the floor so this is usable as a CI gate rather than a
    # report nobody reads. A regression is then a red build, not a number in a file.
    floor = 1.0
    for index, arg in enumerate(argv):
        if arg == "--min-pass-rate" and index + 1 < len(argv):
            floor = float(argv[index + 1]) / 100
    if total and floor > 0 and passed / total < floor:
        print(f"\nFAILED: {passed / total * 100:.0f}% is below the required {floor * 100:.0f}%")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
