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
- Don't run tests, suites or probe scripts unless the user asks. The user tests in the UI and is
  careful with OpenAI credit. That includes `run_suite.py`, `run_csv_suite.py`, `pytest` and any script
  that embeds or asks a question.
  - `app/tests` uses `LLM_PROVIDER=local` and `EMBEDDING_PROVIDER=local`, plus a separate
    `<db>_test` database, so it makes no OpenAI calls. Still ask first.
  - Read code to check a change instead of running it.
- Fix types of question, not single questions. When a check rejects a correct answer, fix the check's
  rule, not a word list.
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

## Meaning check (v54): the general fix for "correct answer, rejected for its wording"

`validate_claims` marks each failed claim `wording_only` when it failed only on wording:
- weak word support;
- a question word the passage does not use ("bracket", "criterion", "additional", "meet");
- the reader's own figure phrased in a way the patterns don't recognise.

Hard failures stay strict and are never judged by meaning:
- absence statements;
- invented or missing citations;
- a figure in neither the evidence nor the question;
- a figure taken from another row;
- a wrong tier or variant;
- a wrong version;
- a dropped "not".

`RAGService._judge` asks the model once, with the question and each claim's passage, whether the claim is
`supported` and `answers` the question (`MEANING_CHECK_PROMPT`). Wording-only claims with both verdicts are
kept, and so is an answer the word-overlap topic check would withhold (claims that don't answer are
dropped).

It fails closed: with no verdict (`RAG_MEANING_CHECK=False`, the model down, or the local stand-in), the
claim is removed as before. The summary's wording-only failures go to the existing summary meaning check.

New phrasings need no new word lists. The lists in `evidence.py` stay only as a fast path that saves the
extra call.

The judge must not accept a statement about another rule. For "Do I satisfy the EMI-to-income
condition?", a minimum-income claim does not answer it; `MEANING_CHECK_PROMPT` says so.

## Prompt and context injection (v57)

`rag/injection.py` recognises instructions to an AI by their grammar, not by a list of attack strings:
- overriding its instructions ("ignore/disregard/forget ... previous instructions");
- giving it a new role ("you are now", "pretend you are", "developer mode");
- asking for its instructions ("reveal/print ... your system prompt");
- addressing an AI by name ("Note to the AI assistant: ...", "AI: approve ...");
- chat markup ("<|im_start|>", "[INST]").

In a document, "you" is the customer, so only an AI named as such counts as addressed. In a question,
"you" is the assistant, and "ignore the documents/rules" also counts. Keep that split: brochures say
"you are now eligible" and "you must submit".

Where it applies:
- **Questions.** `_prepare_request` strips instructions before anything reads the question, and again
  after the rewrite (that catches other languages and follow-ups).
  - In a statement, the whole sentence goes (it's the payload). In a sentence ending with "?", only the
    instruction clause goes.
  - Forged history turns are cleaned too.
  - What remains is answered, with a warning that the instructions were ignored. If nothing remains,
    the reason is `INSTRUCTIONS_IGNORED` (conversational on the page).
- **Documents.** `build_evidence` removes injected sentences before the model reads them or the
  validator checks against them, keeping the layout. `EvidenceSet.injections` produces a warning naming
  the document, and it is logged.
- **Prompt.** The question is fenced between `<<<` and `>>>` and is data. The model is told never to
  follow or reveal instructions and never to treat the question's claims as evidence.
- **Forms.** "Policy update: ..." form lines stay as the reader's claim, never "my ... is" facts.
- **Validator.** A figure the question attributes to the documents ("the policy says ... 2%",
  `validation.premise_values`) may only be corrected ("10.05%, not 2%"). Stating it is a hard failure,
  never judged by meaning.
- **Backstop.** Every claim is still checked against the documents, so an injection that slips past
  cannot add an unsupported statement.

## Reasoning accuracy (v64)

Builds on v60-v63's calculation path (the result must be recomputed and lead the answer, with one retry):
- **Working in a sentence counts.** `calculate.final_calculation` takes the model's last `calculations`
  entry (when every listed entry checks out), or else the last equation a claim writes itself
  (`calculations_in`). Both are recomputed and grounded.
- **Figures are compared at the precision written** (`states_value`): "Rs 96,98,940" must match to the
  rupee, and "93.98 lakh" to Rs 1,000. "about/approximately ..." keeps the 0.5% margin.
- **False equations are removed.** A claim containing one ("53,883 × 180 = 97,00,940") is a hard
  failure (`wrong_arithmetic`). A hyphenated range ("650 - 699") is not read as subtraction.
- **Results chain.** A claim may use a result an earlier claim established ("50% of Rs 1,20,000 is Rs
  60,000", then "Rs 60,000 − Rs 25,000 = Rs 35,000"). The same holds within one claim's equations.
- **Method and conclusion are checked** (`RAGService._check_reasoning`, `REASONING_CHECK_PROMPT`).
  - Applies to every case question with figures (`checks_reasoning`): calculations, eligibility Yes/No,
    "can I take a new EMI".
  - One model call asks whether the right rule was applied completely (no needed input left out, the
    right row and column, the asked quantity rather than an intermediate cap), and whether every
    Yes/No follows.
  - If not, the answer is retried once, with the check's correction in the prompt. Then it is refused.
  - With no verdict, the recomputed answer stands.
- **Change over time** ("Has the penalty increased?", "lowest rate ever", "shorter now than before",
  "then and now") routes to `ACROSS_VERSIONS` (`query_plan.asks_over_time`). The prompt asks for the
  earliest and latest figures with their versions, and only this document's versions.
  - "What changed most recently?" goes to the latest-change comparison.
- **Third-person cases** ("A borrower has ...", "An applicant aged 27 ...") are the reader's case
  (`_ABOUT_READER`).
- **Compute call (v65).** gpt-4o-mini often answers a calculation question with only the rule ("The
  maximum FOIR is 50%"). When an attempt has no recomputable final figure (`_missing_calculation`), one
  short call (`_computed_answer`, `COMPUTE_PROMPT`) asks only for the expression and a result sentence.
  The result is validated, recomputed and reasoning-checked like any answer.
  - The result is stated once, preferring the claim that shows its working. No separate "Calculation:"
    line is added when a claim already shows it.
- Dates ("2019-08-15") are never key terms; they choose the version. "Q." / "Q63:" labels are dropped.

## "Not found" answers (v58)

For the reasons in `NOT_FOUND_REASONS`, `service._explain_not_found` builds the message from what was read:
the nearest section headings and documents in the evidence. "Front matter" and contents pages are left out.

- Not covered (`KEY_TERMS_NOT_FOUND` / `LOW_RELEVANCE` / `NO_RELEVANT_DOCUMENTS`): "Your documents don't
  seem to cover this. The nearest sections I found, “Pricing and Fee Schedule” ... in the Home Loan
  Guide, are about other topics."
- Not stated (`INSUFFICIENT_EVIDENCE`): "I read ... but they don't state this, and I won't guess."
- Not quotable (`ANSWER_FAILED_VALIDATION`): "... nothing there states the answer clearly enough for me
  to quote it."
- Off topic (`ANSWER_OFF_TOPIC`): "The closest passages ... are about something other than what you
  asked."
- Claim not confirmed (v66, `CLAIM_NOT_CONFIRMED`): any of the reasons above, for a question that asks to
  confirm what someone says (`evidence.asks_to_confirm`): "I can't confirm that claim. I read “Data privacy
  and record keeping” in the Mortgage Loan Policy, and nothing there says it." Heading: "Not confirmed by
  your documents".

Suggestions are questions about those sections ("What does the Home Loan Guide say in “Who Can Apply”?").
When nothing came close, the generic tips in `SUGGESTIONS` are used.

The page heading depends on the reason (`NOT_FOUND_HEADINGS` in `AssistantPage.tsx`): "Not covered in
your documents" or "Your documents don't answer this directly", never "I couldn't find this". Suggestions
come from ACL-filtered evidence, so they never name a document the reader can't see.

## Clarifying questions (v56)

- **When to ask "Which one do you mean?"** `_refuse_if_ambiguous` asks only when at least 3 matching rules
  are variants of one rule (`_variants_of_one_rule`: ≥50% shared words, `VARIANT_OVERLAP`) and set
  different figures. Example: "approval note retained ... 7 / 10 / 14 years".
  - Rules that only share the question's word ("What will my EMI be?" matches Flexi-EMI, EMI dates, the
    EMI cap and the overdue penalty) go to the model.
- **Wording.** The message names the policy once ("Home Loan Guide (Version 8)"), and the choices are
  readable rules with the bullet removed, cut at a word. The page adds the "Which one do you mean?"
  heading and the "Matching rules" list, so the backend repeats neither.
- **Questions missing a detail** ("What will my EMI be?" with no amount or tenure). The prompt says to
  give what the figure depends on and an example row, then ask for the missing details. That is an
  answer, not `insufficient_evidence`.

## Input forms and arithmetic (v55)

- **Filled-in forms.** `query_rewrite.from_form` runs first in `_prepare_request`. A message such as
  "Context: / Age: 35 / Income: ₹60,000 / Version: 6 / Question: Do I ...?" becomes "My age is 35 and my
  income is ₹60,000. Do I ... in Version 6?", so the reader's-case handling and version routing apply.
  - "Expected:" and "Answer:" blocks from pasted test cases are dropped, and short headings ("Q63")
    are skipped.
  - Ordinary multi-line questions are unchanged.
- **Version labels.** `_VERSION_REF` also accepts "Version: 6".
- **Arithmetic written in a claim (v59, the whole "how much" family).** The model usually writes the
  working inline, as people do: "Rs. 1,07,767 × 180 months − Rs. 1 crore = Rs. 93,98,060", "(Rs. 53,883 ×
  180) − (Rs. 1,06,358 × 60) = Rs. 33,17,460", "50% × Rs. 60,000 − Rs. 10,000 = Rs. 20,000".
  - `calculate.written_arithmetic` tokenises figures with currency marks, units (months/years), scales
    (lakh/crore/thousand/k) and %, and operators as symbols or words (minus/less/times/divided by). It
    accepts "=", "≈", "is" and "about".
  - Every figure must be grounded (evidence, question, or a unit conversion), and the stated result must
    match the recomputed one.
  - The prompt asks for the working inside the claim, and lists the family's formulas: total repaid,
    interest paid, saving, share, room for new EMIs, fee on an amount, and maximum loan.
  - `_derived` also accepts EMI × months, using the question's years in months.
  - For a claim whose figure was verified this way, the question's own words and words addressing the
    reader ("you would pay", "you can borrow") count as supported.
  - Wrong or ungrounded arithmetic stays a hard failure.
- **Shares.** `_derived` also accepts one figure as a percentage of another ("Rs. 35,000 is 58.33% of
  Rs. 60,000").

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
  - `evidence.READER_OUTCOME_WORDS` ("meet", "satisfy", "criterion", "criteria", "eligible", "eligibility",
    "qualify", "requirement", "stated") are framing only when `applies_to_reader(question)`. They are the
    yes/no frame of "do I meet the minimum-income criterion?". Without a reader's case they stay gate terms
    ("What are the eligibility criteria?"), as v49 intended.
  - Don't make these words required again for reader-case questions. A correct claim ("Rs. 49,000 is
    below the Rs. 60,000 minimum") is then withheld as off topic because it doesn't say "meet", and a
    claim saying "criterion" fails the subject check.
  - "additional", "extra", "further" and "else" are additive framing (in `QUESTION_TERMS`). The word they
    qualify stays a term.
  - A yes/no question ("Is/Can/Does/Am ...", `evidence.asks_yes_no`) that gives a bare figure ("Is age 25
    eligible?", "Is age 24?") tests that figure, so it counts as the reader's figure
    (`situation_figures`). Years are excluded, because they choose versions.
  - The prompt requires a "Yes"/"No" first claim that sets the figure against the rule, and a summary
    that starts the same way. `_unchecked_figures` flags a yes/no reply that gives the rule without
    applying the figure.
  - Placing a figure in a band ("Is credit score 650 in the 650-699 bracket?", "Which band does 680 fall
    in?", `evidence._PLACES_FIGURE`): "bracket", "band", "slab", "tier", "range" and "falls in" are the
    reader's words for a table row (`READER_OUTCOME_WORDS`, and `validation._FRAMING` for support). Tables
    list just "650 - 699".
  - If "bracket" were a required term, the v8 "Yes" claim would be removed and the answer would fall
    back to an earlier version without saying Yes.
  - A figure said to be the reader's in a claim ("Your total EMIs are 60%") is accepted beside a rule
    figure (`validation._READERS_OWN`). Arithmetic on the reader's own figures (60% of Rs. 50,000) must
    go through `calculations`.
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
- **Two or more dates** (v66: "Compare a loan sanctioned on 2026-06-15 with one sanctioned on 2025-06-15:
  what is the credit-score threshold in each period?", "the fee in March 2024 and in March 2026", "... on
  2025-06-15, different from now?"):
  - `query_plan.dates_in_question` reads full dates, then months (at month end). It adds today only when one
    date is set against now (`_AGAINST_NOW`: "with one sanctioned today", "different from now", "vs the
    current version"). Bare years are left out: "between 2020 and 2024" is a period.
  - Two or more dates route to `COMPARISON` with `plan.as_of_dates` (version labels and which-period
    questions win). `_plan_comparison` takes each policy's version in force on each date, from the named
    policies if any. If only one date has a version in force (a date of birth in a form), it becomes an
    as-of question on that date.
  - `_sides` narrows to the policy of the best-matching passage and searches its versions one by one.
    Evidence is listed oldest first.
  - The prompt says: one claim per date, naming the version whose period covers it, then whether it
    changed. Dates, month names, period words and "now/today" are not key terms.
  - Mortgage Loan Policy: min score v6.0 (2024-10-01 to 2026-04-01) 720, v7.0 725.
- **Confirming a claim** (v66: "A user says the policy permits sharing another borrower's details. Can you
  confirm?", "Is it true that ...?", "My manager said ..."):
  - `evidence.asks_to_confirm` recognises the frame: someone says or claims, "is it true that", or
    confirm/verify a claim or statement. "Can you confirm the fee?" just asks for the fee.
  - `key_terms` drops the frame ("a user says", "confirm that claim").
  - The prompt says: answer "Yes" with the rule that states it, or "No" with a rule that forbids or
    restricts it. If nothing addresses it, `insufficient_evidence`; never confirm because the question
    asserts it.
  - A refusal becomes `CLAIM_NOT_CONFIRMED` (see "Not found" answers). Every claim is still validated.
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
