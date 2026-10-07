# Governix: notes for coding agents

Governix is a banking-policy RAG platform. FastAPI backend (`app/`), sync SQLAlchemy with psycopg 3,
Alembic migrations, Postgres 18 with pgvector 0.8, and a React + Tailwind + Redux Toolkit frontend
(Vite, in `frontend/`). Governix issues its own JWTs; there is no external SSO.

## How the user wants work done

- Build phase by phase from the spec. Never generate the whole app at once.
- Fixes must be generic: users can upload any PDF and use any short form of its terms. Never add
  per-tenant or per-document keyword or synonym lists. Derive aliases from the document data.
  `validation.SYNONYM_GROUPS` is for general English synonyms only.
- Keep refusal safety. Never loosen a check in a way that lets an unsupported answer through.
- Ask before changing the DB schema or building the rule-level unit index (stage 3 of `docs/rag-audit.md`).
- Don't run the live question suites (`run_suite.py`, `run_csv_suite.py`) unless asked. They call the
  LLM for every question and cost tokens; the user tests in the UI. Unit tests are free and take
  about a second, so always run them: `.venv/bin/python -m pytest app/tests/unit -q`
- Report changes under these headings: What changed / Files / DB / API / Tests / Risks.
- When answer behaviour changes, bump `ANSWER_CACHE_VERSION` in `app/modules/rag/service.py` and add a
  comment line for it. Otherwise cached answers (10 min) hide the change.

## Environment

- `.env` uses `LLM_PROVIDER=openai`, `LLM_MODEL=gpt-4o-mini`, and OpenAI `text-embedding-3-small`
  (1536-d) embeddings. The OpenCode Zen settings (`space-bunny-free`) stay in `.env` as the fallback
  for when OpenAI credit runs out. Only that free model works through the API: the others return
  403/402, so don't work around that.
- Documents ingested while OpenAI embeddings were failing get `local-hashing-v1-1536` vectors. The
  vector lane only uses chunks whose `embedding_model` matches the current model, so those documents
  are reachable by keyword only. To fix: `.venv/bin/python -m app.cli reembed --organization <ORG_ID>`.
- Never put the database password on a command line. Use the app's engine from Python with
  `PYTHONPATH=.`, via `create_db_engine(get_settings())`.
- Test data:
  - Org `d81d8ac2-7d9c-490b-bf60-c816300b2ae7`: Home Loan Guide v1-v8 (one policy, 8 versions, about
    10 chunks each), plus small QA policies (Compliance, Gold Loan, HR & Leave, Mortgage, Operations).
  - Org `2fb01ee1-3e0d-44ec-bea0-ad57ca66332f`: the 333-question CSV (Home Loan / Personal Loan /
    KYC-AML), baseline in `docs/rag-audit.md`.
  - `run_suite.py` holds the National Electricity Plan suite.

## Answer pipeline (`app/modules/rag/service.py`, `RAGService.answer_events`)

1. Standalone rewrite.
2. `plan_query`.
3. `retrieve_with_variants`: hybrid lanes (exact / keyword / vector / section).
4. `build_evidence` (`evidence.py`).
5. `_gate`: key-term coverage plus IDF-weighted salient coverage. Skipped when a passage's vector
   similarity is at least `RAG_SEMANTIC_MIN_SIMILARITY`.
6. `_refuse_if_ambiguous`.
7. LLM call (`prompts.SYSTEM_PROMPT`; JSON claims plus `insufficient_evidence`).
8. `validate_claims` (`validation.py`: figures, word support, subjects).
9. `_check_on_topic`.
10. Summary.

On a refusal the pipeline may restate or split the question (`query_rewrite.restated_questions`) or
fall back to earlier versions.

`evidence.key_terms()` decides which question words must appear in the evidence. It leaves out
`QUESTION_TERMS` (framing words), `CLOSED_CLASS_TERMS` and the reader's own figures. A word that
appears in no visible document gets the highest weight. That is how "FIU-IND" asked of a bank with
no KYC policy is refused.

## Question types (since 2026-10-07)

- **Conditional / the reader's own case** ("If my credit score is 680...", "I am 25, can I apply?",
  "I'm a software engineer earning 70k..."):
  - `evidence.readers_situation()` splits off the situation clause. Numbers in it are the reader's
    figures and are not required in the evidence.
  - Situation words that no visible document uses (`RAGService._circumstances`) are skipped by the gate
    and by the on-topic check, but only when the question also names a subject of its own. Claims are
    still checked against them.
  - The ambiguity refusal is skipped, because the reader's case picks the row.
  - A statement about the reader runs on until a clause asks something, so "I am 27, earn Rs 65,000 and
    have a 760 score. Can I get a loan?" is all situation up to "Can I".
  - The prompt says to check every figure in its own claim and give the overall Yes/No in the summary.
    `_unchecked_figures` is the safety net: if some figures are checked but others are not, it adds a
    warning and drops the summary.
  - The validator accepts the reader's outcome ("You are 27, which is below ...", "at 27 you do not
    qualify"): a claim that applies the reader's figure may add a negation (`_flips_polarity(outcome=)`),
    but never drop the rule's own one. "27 years" counts as the reader's "27".
  - Earlier-version notes for reader-case questions match each rule by its own wording
    (`_rule_worded_like`, `SAME_RULE_OVERLAP`), failing rules first. Example: v8 age 28-65, v7 25-65.
- **Advisory and speed** ("should I", "which is better", "how fast", "turnaround"): these words are in
  `QUESTION_TERMS`. The prompt says to give the options and trade-offs the documents state, with no
  verdict.
- **Process and service** ("how do I...", "how long", "online?"): handled by prompt rules.
- **When did it start or change** ("When did the 50% EMI-to-income rule start?", "Since when is
  Flexi-EMI offered?", "Which version first introduced X?"):
  - `query_plan.asks_when_introduced()` routes the question to `ACROSS_VERSIONS` with `plan.since`.
  - `RAGService._sides` searches each version of the policy that was named, chosen or best matched.
  - The prompt lists evidence oldest version first and asks for the earliest version, with its
    effective date, that states the rule.
  - "When is the EMI due?" and "When does X start?" are rules in force and stay `CURRENT`.
  - For the Home Loan Guide, the 50% EMI cap first appears in v5 and Flexi-EMI in v7. v4-v6 say
    "flexible EMI dates", a different feature.
- **Which period had a figure** ("Which period had the lowest EMI for Rs 50 lakh for 15 years?", "When
  was the fee highest?"):
  - `query_plan.asks_which_period()` matches past tense only, and routes the question to
    `ACROSS_VERSIONS`. "Which period has the highest FD rate?" can mean the tenure bands of one rate
    table, so it stays `CURRENT`.
  - Every across-versions question searches each version on its own (`_sides`), and the evidence is
    listed oldest first.
  - The prompt says to read each version's table under its own column headings.
  - Home Loan Guide: the EMI tables move the "15 years" column (2nd in v4-v5, 3rd in v6-v8), and v1-v3
    have no Rs 50 lakh row. The lowest 50 lakh / 15-year EMI is v6 at Rs. 50,565.
- **Calculations** ("How much total interest on Rs 1 crore over 15 years?", "How much do I save with 5
  years instead of 15?"): this is stage 8 of the audit, `rag/calculate.py`.
  - The model writes `calculations` (expressions with + - * / over figures from the evidence or the
    question, plus the unit constants 12, 52, 365, 100, 1000, 1 lakh, 1 crore) before its claims.
  - `checked_calculations` re-evaluates each one safely with `ast`, and drops it if any figure is not
    grounded.
  - `validate_claims(calculations=)` lets a claim citing the same evidence state a result or an
    intermediate step, within 0.5% for rounding. The words "total", "save" and "difference" are then
    exempt from the subject check.
  - A "Calculation: 1,07,767 × (15 × 12) − 1,00,00,000 = 93,98,060" claim is appended for each result
    used.
  - The claim stream reads the calculations from the JSON before the claims (`ClaimStream.preamble`).
  - Powers are not allowed, so an EMI is never computed from a rate. EMI scales with the amount
    borrowed, which is allowed.
- **Paraphrase** ("CIBIL" for credit score, "complaint" for grievance): a close vector match bypasses
  the gate. With close evidence, the on-topic check also ignores words that no document uses.
- **Known risk:** "If I take a car loan, what is the rate?" in an org with no car-loan document now
  relies on the model's `insufficient_evidence` judgement, because "car" is a situation word that no
  document uses.
