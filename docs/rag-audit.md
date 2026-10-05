# Governix RAG audit: current implementation, root causes, options

Date: 2026-10-05. Scope: Phases 1–6 of the improvement brief (audit, architecture map, failure
traces, classification, approach comparison, decision matrix). **Nothing in the pipeline has been
changed yet.**

Evidence base:

* Full evaluation: 333 questions (`rag_test_questions (2).csv`), org `2fb01ee1…` (Home Loan,
  Personal Loan, KYC/AML policies, v1.0–v3.0, 5,000 / 2,500 / 1,500 pages). Result: 94 correct,
  53 partial, 45 wrong, 141 no answer. Re-running 25 sampled refusals one at a time reproduced 24.
* Stage-by-stage traces of 30 questions through the live pipeline (`RAGService.ask` with hooks on
  retrieval, evidence, gate, generation and validation; no code changed).
* Lane-rank measurements: exact rank of the expected clause in the vector and keyword lanes with no
  limits.
* Granularity benchmark: rank of the expected rule at rule level vs chunk level for 20 failing
  questions.

---

## 1. Current architecture (Phase 1 + 2)

### 1.1 Inventory

| Area | What exists | Where |
|---|---|---|
| PDF parsing | PyMuPDF per page; ruled tables flattened to one line per row (`Header: cell \| Header: cell`); invisible text dropped | `app/modules/ingestion/extraction.py` |
| OCR | Local OCR when a page has < 25 chars (`OCR_MIN_CHARS`), 300 dpi; cloud OCR optional | `app/infrastructure/ai/ocr/`, `app/workers/ocr/tasks.py` |
| DOCX | Not an ingestion format (metadata only) | — |
| Structure | Layout-aware headings (numbering, keywords, font size/bold), repeated header/footer removal, section tree | `app/modules/ingestion/structure.py` |
| Metadata | Title/version/effective-date detection, category classification, policy matching, AI review | `app/modules/ingestion/analysis/*` |
| Duplicates | `file_sha256`, `content_hash`, 64-bit `simhash`; near-duplicate copies dropped from evidence (`one_copy_per_document`) | `ingestion/text.py`, `rag/evidence.py` |
| Versions | `policy_versions` with half-open effective ranges, supersedes chain, stored deterministic diff (`change_summary`) | `app/modules/versions/timeline.py`, `comparison.py` |
| Chunking | Section-aware, never crosses a section; target 1,500 chars, max 2,200, one-sentence overlap (≤300 chars); exact page marks | `app/modules/search/chunking.py` |
| Chunk metadata | org, branch, department, policy, version, category, section id/number/path, page start/end | `app/modules/search/model.py` |
| Full-text | `tsv` = section_path (weight A) + text (weight B), GIN index. **Policy name / document title are not indexed with the chunk.** | `search/model.py:43` |
| Embeddings | OpenAI `text-embedding-3-small`, 1,536 dims, batches of 256, content-hash `embedding_cache`; local hashing fallback labelled separately | `infrastructure/ai/embeddings/openai.py` |
| Vector store | PostgreSQL + pgvector 0.8.6, HNSW cosine (m=32, ef_construction=128), `ef_search=100`, iterative scan `relaxed_order` | `search/retrieval.py:528` |
| Keyword | OR-tsquery; cheap `ts_rank_cd` preselect (300) → IDF-weighted per-term rank (BM25-like) → top 50; terms in >20% of chunks dropped in large collections | `retrieval.py:501` |
| Exact | quoted phrases, numeric clause numbers, chapters/annexures, page numbers, identifiers with digits (`BNK-126`) | `retrieval.py:550` |
| Section lane | section-level FTS → chunks of the best 200 sections | `retrieval.py:669` |
| Fusion | weighted RRF (k=60; exact 1.5, keyword 1.0, vector 1.0, section 0.6); ×1.3 for chunks of a policy the question names; routed second vector pass over the 3 leading documents | `retrieval.py:818` |
| Query variants | deterministic synonym swaps (`ltv`→`loan to value`, `maximum`→`limit`, …), up to 3, each a full retrieval | `search/query_intelligence.py` |
| Reranker | `LocalReranker` "local-lexical-v1": IDF word overlap within the pool, proximity, phrase bonus; 6-char truncation stemming. **Not a cross-encoder.** Blended 50/50 with fused rank | `infrastructure/ai/reranker/local.py`, `rag/evidence.py:281` |
| Evidence | rerank pool 96 → distinct passages → ≤3 per document → 14 items; ±1 neighbouring chunk (600 chars each side); amendments; numeric conflict detection | `rag/evidence.py:181` |
| Query processing | follow-up/translation rewrite (LLM, only when needed); several-questions split (LLM); deterministic planner for version/date/compare; policy-name lookup by trigram `word_similarity ≥ 0.8` | `rag/query_rewrite.py`, `rag/query_plan.py`, `retrieval.py:769` |
| Gate (pre-LLM) | no items → `NO_RELEVANT_DOCUMENTS`; top score < 0.18 → `LOW_RELEVANCE`; key-term coverage < 0.4 or IDF-weighted salient coverage < 0.5 → `KEY_TERMS_NOT_FOUND`; ≥3 conflicting rules → `AMBIGUOUS` | `rag/service.py:984`, `:1743` |
| Generation | gpt-4o-mini, streamed structured JSON (claims + evidence ids + summary + `insufficient_evidence` + conflicts) | `rag/prompts.py`, `rag/service.py:1078` |
| Validation | per claim: citation exists, every number in cited text, ≥35% content-word support, subject terms present, number bound to subject, variant (tier/zone), version claims, polarity; then on-topic check; DB re-verification of citations; summary checked by a second LLM call unless a pure restatement | `rag/validation.py`, `rag/service.py:1175` |
| Fallbacks | current question with no support → earlier versions one at a time; then LLM "clarify" rewrite → whole pipeline again | `rag/service.py:732`, `:440` |
| Caching | answer cache (memory or Redis) keyed on question + scope + `knowledge_version`, TTL 600 s; query-vector cache 24 h; IDF statistics 10 min | `rag/service.py:502`, `search/service.py:45` |
| ACL | `visible_clause(...)` inside every lane's SQL, re-checked when citations are verified | `retrieval.py:250`, `service.py:1503` |

### 1.2 Architecture map (as built)

```text
USER QUERY
   ↓  rag/router.py  POST /ai/ask  → RAGService.ask → answer_events          (service.py:401)
   ↓  answer cache lookup                                                    (service.py:413)
Query processing
   ↓  _prepare_request: standalone_question (LLM only for follow-ups/non-English)  (query_rewrite.py:125)
   ↓                    referenced_documents (file names)                   (retrieval.py:785)
   ↓                    plan_query → CURRENT | HISTORICAL | SPECIFIC_VERSION | COMPARISON | ACROSS  (query_plan.py:89)
   ↓  asks_several → _clarify (LLM split)                                   (service.py:226, :617)
   ↓  referenced_policies: trigram word_similarity ≥ 0.8                    (retrieval.py:769)
   ↓  _filters: version scope (labels → ids via _resolve_labels)            (service.py:843)
Retrieval
   ↓  retrieve_with_variants: query_variants (≤3) × HybridRetriever.retrieve (search/service.py:69)
   ↓     lanes in parallel: exact | keyword(IDF) | vector(HNSW) | section   (retrieval.py:848)
   ↓     routed vector pass on top-3 documents; weighted RRF; policy boost  (retrieval.py:856-877)
Ranking / context
   ↓  build_evidence: LocalReranker (lexical) 50% + fused 50%; dedupe; ≤3/doc; 14 items;
   ↓                  ±1 neighbour chunk; amendments; conflicts; coverage_of (evidence.py:181)
Gate
   ↓  _gate: coverage ≥0.4, salient (IDF) ≥0.5 over chunk text + section path  (service.py:984)
   ↓  _refuse_if_ambiguous                                                  (service.py:1743)
LLM
   ↓  _acronym_answer (deterministic glossary path)                         (service.py:1123)
   ↓  _write → _generate_stream: gpt-4o-mini structured claims              (service.py:680, :1078)
Validation
   ↓  validate_claims (citations, numbers, support, subject, binding, polarity) (validation.py:410)
   ↓  _check_on_topic (IDF coverage of question terms by claims + policy/section labels)  (service.py:1029)
   ↓  _verify_citations (DB re-check), _summary (2nd LLM check), _earlier_version_notes
Fallback
   ↓  KEY_TERMS_NOT_FOUND / INSUFFICIENT / … on a CURRENT question → each earlier version (service.py:732)
   ↓  still no answer → _clarify (LLM rewrite) → whole pipeline again      (service.py:440)
CURRENT ANSWER  (+ audit event, cache write)                                (service.py:575)
```

### 1.3 Performance (measured)

| Stage | Median | Max |
|---|---|---|
| policy lookup | 5 ms | 23 ms |
| keyword lane | 522 ms | 1,702 ms |
| section lane | 653 ms | 2,043 ms |
| vector lane | 425 ms | 4,148 ms |
| evidence build | 61 ms | 82 ms |
| clarify (LLM) | 1,167 ms | 1,650 ms |
| generation + validation (answered) | 6,620 ms | 11,920 ms |
| total (30 traced) | 7.6 s | 21.5 s |

Full 333-question run (6 in parallel): p50 7.6 s, p95 32 s, p99 48 s. A refusal is the slow path:
Q266 ran **12 retrieval passes** (3 versions × variants, then a clarified re-run) before refusing.

---

## 2. Root causes (Phase 3)

Each cause below was observed in a trace, not inferred.

### RC1 — Policy names are only recognised when typed (almost) in full — *Critical, generic*

`referenced_policies` (retrieval.py:769) matches `word_similarity(policy.normalized_name, question) ≥ 0.8`.

| Question names the policy as | score | resolved |
|---|---|---|
| "Know Your Customer and Anti-Money Laundering Policy" | 0.86 | yes |
| "Unsecured Personal Loan Policy" | 1.00 | yes |
| "Personal Loan Policy" | 0.67 | no |
| "Home Loan Policy" | 0.56 | no |
| "KYC/AML Policy", "KYC Policy", "AML policy" | 0.15–0.16 | no |
| "HL policy" | 0.24 | no |

Consequences, all seen in traces:

* the policy is not used to scope retrieval: "KYC/AML Policy v1.0" searched **v1.0 of every
  policy** (`version_ids=3`), "List all changes between v1.0 and v2.0 of the Home Loan Policy"
  compared **six** versions; Q057 (personal loan) drew KYC passages as evidence;
* the name's words stay in the question as *subject* terms (`kyc/aml`, `home`, `loan`) and the
  gate later requires them in passage text (RC2);
* a comparison cannot find its target (RC8).

Version isolation still held in the evaluation only because the content of each policy differs.

This is not KYC-specific: any document a user calls by a short name, an abbreviation, or its
document ID ("HLP", "PL policy", "AML policy", "CROPM") hits it. The documents themselves state
usable aliases: every cover carries `Document ID: HLP-v3.0 / PLP-v3.0 / KAP-v3.0`, clauses carry
the same prefix, and titles have initials.

### RC2 — The key-term gate counts scoping and framing words as subject terms — *Critical*

`key_terms()` (evidence.py:134) keeps every question token except a hand-maintained list of ~250
framing words (`QUESTION_TERMS`, extended word by word: "serving serve serves served…"). The gate
(service.py:984) then requires ≥40% of them, and ≥50% of their IDF weight, in the evidence.

Leaks observed (missing-term counts over the 114 `KEY_TERMS_NOT_FOUND` refusals): `kyc/aml` 45,
`page` 12, `per` 10, `first` 9, `limit` 6, `provision` 6, `vs` 6, `all` 4, `three` 4, `sentences` 3,
`became`/`more`/`less`/`strict` 3.

Three separate defects:

1. **Scoping words** (policy names, aliases) are treated as subject. Q307/Q309: the clause was
   fused rank #1 (exact lane #1) and evidence item E1; refused for `kyc/aml` + `page`.
2. **Abbreviations never match their expansion.** `npa` vs "Non-Performing Asset", `fiu` vs
   "Financial Intelligence Unit", `kyc` vs "Know Your Customer". A generic initials matcher already
   exists (`_matches_acronym`, service.py:1841) but is only used for "what does X stand for".
3. **Paraphrases never match** ("stretch repayment of a house loan" vs "maximum tenure … home
   loan"). Q266: correct clause retrieved as E2, refused for `stretch`, `house`.

Also: the "named clause" exemption (`requested_clauses`) recognises only numeric clauses
("clause 5.2"), not identifiers such as `KAP-KEY-01`, `HLP-4.2`, `RBI/2025/12`.

**Current vs v3.0 (Q099 vs Q030)** is this cause, not version resolution: the same clause was
fused rank #1 in both. Q030 passed with one missing term (`npa`); Q099's "as **per** the current
policy" adds `per`, salient coverage drops to 0.43 < 0.5, refused. The refusal then triggered the
earlier-version fallback (v2.0, v1.0 — same refusal) → 3 passes.

### RC3 — Retrieval granularity: one chunk = 7–8 unrelated rules — *Critical*

Chunks are ~1,400 chars (target 1,500). In these policies each chunk holds 7–8 independent rules;
the asked rule is ~10% of the chunk, so the chunk's embedding and term statistics describe the
other rules. Exact rank of the expected chunk (no limits; lanes keep 50, keyword preselects 300):

| Test | vector rank (right document version) | keyword preselect rank |
|---|---|---|
| Q072 STR deadline | 492 / 6,915 | 796 |
| Q063 FIU cash report | 1,447 / 6,915 | 9,501 |
| Q016 prepayment charge | 1,573 / 22,993 | 65 |
| Q057 sanction limit | 2,786 / 11,341 | 455 |
| Q043 PL processing fee | 994 / 11,341 | 1,283 |

The answer never reaches the candidate set, so no gate, reranker or prompt change can fix these.

Granularity benchmark (rule = one paragraph/clause; vector pool = rules of the 300 nearest chunks
+ top 300 rule-level BM25 hits + the key chunk, ~1,000–2,100 rules):

| | chunk vector (today) | rule BM25 (all 40k–135k rules) | rule vector (pool) |
|---|---|---|---|
| Rank 1 | 1 / 20 | 11 / 20 | **17 / 20** |
| Top 3 | 3 / 20 | 15 / 20 | **19 / 20** |
| Top 10 | 4 / 20 | 15 / 20 | **20 / 20** |

Rule BM25 fixes the look-alike and KYC lookups; paraphrases (Q265, Q269, Q270, Q273) need
rule-level vectors. **The embedding model is adequate; the unit it embeds is the problem.**
Caveat: the rule-vector figure is measured against the hardest ~2,000 distractors, not all rules,
and must be confirmed on a real index.

### RC4 — Sufficiency is judged on chunk + heading words, not on the sentence that answers — *Critical*

* `coverage_of` runs over `full_text + section_path` (evidence.py:271): a heading "Repayment and
  **Prepayment**" satisfies `prepayment`.
* `_off_topic` counts the cited passage's label (policy name + section path) as words the answer
  "said" (service.py:1200).
* `validate_claims` proves a claim is a *true statement of its passage* (numbers present, words
  overlap, polarity kept) — not that it answers the question.

Q016: evidence = chunks from "Repayment and Prepayment" sections; gate passed (coverage 0.857);
model wrote four true sentences "The Bank reserves the right to revise the applicable charges…";
all four validated; answered. Q057: true sentences about "Regional Credit **Officer**" approval for
"exposure above Rs. 315 lakh" answered a question about "Regional Credit **Committee**".

### RC5 — The model's `insufficient_evidence` flag is ignored when any claim validates — *Critical*

`_validated_answer` reads the flag only when no claim survives (service.py:1194). Q245 (gold loan;
no such policy): the model returned `insufficient_evidence: true` **and** one claim about the
home-loan LTV; the claim validated; the answer was served.

### RC6 — Absence statements pass as cited facts — *High*

`_ABSENCE` (validation.py:39) does not allow an adverb: "not **explicitly** stated in the …" passes.
Q043–Q045: "The processing fee … is not explicitly stated" served with 14 citations.

### RC7 — The number check forbids user-given and derived figures — *High*

Every figure in a claim must appear in the cited text (validation.py:458). The model computed
correctly and the validator removed it:

| Test | Model's claim | Removed because |
|---|---|---|
| Q203 | "…would be **Rs. 24,000** (0.40% of Rs. 60 lakh)" | 'Rs. 60 lakh', 'Rs. 24,000' not in evidence |
| Q215 | "Rs. 8 lakh … does not meet the threshold of Rs. 10 lakh" | 'Rs. 8 lakh' not in evidence |
| Q258 | summary "0.40% in Version 3.0, **not 1%**" | '1%' not in evidence → summary dropped |

Only one derived figure is supported today: the difference between two versions
(`_magnitude_comparison`, service.py:1670).

### RC8 — Planner and comparison routing gaps — *High*

* `_ACROSS` (query_plan.py:42) accepts "all versions" / "the two versions" but not "all **three**
  versions", "all 3 versions", "every version of". Q319/Q321 were planned as CURRENT and returned
  one version.
* `comparison_subject` treats `all` ("List ALL changes") as the subject, so the question goes to
  passage search instead of the version diff; with RC1 the diff would anyway see three policies →
  `COMPARISON_TARGET_UNCLEAR`.
* The diff itself is good: Home Loan v1.0→v2.0 took 1.7 s and returned exactly 6 modified sections
  of 14,175 (cover date + all 5 real changes), already stored as `change_summary`. But
  `comparison_text` details only numeric changes; a modified section without a detected numeric
  change is listed by name only, and bare numbers (credit score 700→725) are not detected.

### RC9 — Refusals take the most expensive path — *Medium*

A false `KEY_TERMS_NOT_FOUND` on a current question triggers every earlier version (full pipeline
each) and then an LLM clarify + another full run. 12 retrieval passes for Q266; refusals p95 32 s
under load. Fixing RC1–RC3 removes most of these runs; the cascade itself should also stop
re-asking when the refusal reason would not change with the version.

### RC10 — Lexical reranker — *Medium/Low*

`LocalReranker` contributes 50% of the evidence order but is word overlap with 6-char truncation; it
cannot judge meaning. It can only reorder what retrieval found, so it is not the cause of RC3.
Re-measure after rule-level retrieval before deciding whether a cross-encoder earns its cost.

### What works and must be kept

* Version isolation once versions are resolved; version labels in evidence headers; the
  earlier-version notes; the version register checks.
* Exact clause lookup by identifier (exact lane): KAP-KEY-xx at fused rank #1.
* Citation verification against the database; page provenance (cited page 199–200 for "page 200").
* Refusal of genuinely missing information (11/13), `VERSION_NOT_FOUND`, ambiguity clarification.
* ACL inside every lane's SQL, re-checked at citation time.
* The deterministic version diff and its stored `change_summary`.

---

## 3. Failure classification (Phase 4)

| Test | Failure | Root cause | Component | Severity |
|---|---|---|---|---|
| Q307–Q318 | Refused (clause retrieved #1) | `kyc/aml` (policy name) + `page` required as subject terms; identifier clause not exempt | Gate (`_gate`, `key_terms`), policy resolver | Critical |
| Q063, Q072, Q078, Q086 | Refused | Key rule ranks 483–1,447 by chunk vector; plus `kyc/aml`, `fiu` in gate | Retrieval granularity; gate | Critical |
| Q016 | Wrong (look-alike) | Key rule rank 1,573; heading "…Prepayment" satisfies gate and on-topic check | Retrieval granularity; sufficiency at heading level | Critical |
| Q057 | Wrong (look-alike, cross-policy) | Policy unresolved (0.67) → KYC evidence; key rank 2,786; "Officer" ≈ "Committee" | Policy resolver; retrieval; sufficiency | Critical |
| Q245 | Wrong (unanswerable answered) | `insufficient_evidence=true` ignored | `_validated_answer` | Critical |
| Q043–Q045 | "Not explicitly stated" with 14 citations | Key rank 994; `_ABSENCE` misses adverbs | Retrieval; validation regex | High |
| Q203, Q215, Q258 | Rule quoted, no calculation / correction | Model's derived and question figures rejected | Numeric validation | High |
| Q180–Q185 | 0 real changes listed | Policy unresolved → 6 versions; `all` as subject → no diff | Policy resolver; comparison routing | High |
| Q319–Q324 | One version listed | "all three versions" not recognised | Planner `_ACROSS` | High |
| Q099 (vs Q030) | Refused | `per` + `npa` → salient 0.43 < 0.5 | Gate (framing word, abbreviation) | High |
| Q266 | Refused (clause retrieved E2) | Paraphrase words required by gate | Gate | High |
| Q265, Q267–Q271 | Refused | Rule not retrieved (chunk granularity) and gate | Retrieval; gate | High |
| Q096, refusals | Timeouts / 20–60 s | Fallback + clarify cascade on false refusals | Orchestration | Medium |

---

## 4. Approaches compared (Phase 5)

**Query understanding.** Current: deterministic planner + trigram policy lookup + LLM rewrite for
follow-ups. LLM query expansion would help paraphrases but adds ~1 s and an LLM call to every
question, and rule-level vectors already rank paraphrases first (RC3 table). Entity extraction for
*documents and versions* is the real gap, and it can be done deterministically from data the
documents carry (titles, initials, declared IDs, policy numbers). → Deterministic alias resolver;
no new LLM call.

**Retrieval.** Vector-only and BM25-only each fail half the cases at rule level (BM25: paraphrases;
vector alone loses exact identifiers). Current hybrid + metadata filtering is the right shape;
the unit is wrong. → Keep the 4-lane hybrid and its SQL ACL; add a rule-level unit index (lexical
and vector) whose hits map back to their parent chunk for context ("small-to-big").

**Ranking.** RRF is fine; the lexical reranker cannot add meaning; a cross-encoder can only reorder
candidates, and today the answer is not among them. → Re-measure after rule-level retrieval; add a
cross-encoder only if Recall@1 lags Recall@10 materially.

**Version handling.** Metadata filtering is correct and already in SQL; it fails only when the
policy is unresolved. A dedicated comparison pipeline already exists (deterministic diff). →
Resolve policy + versions first; route "what changed / list all changes / how did X change" to the
stored diff; run per-version retrieval for "all versions" questions.

**Hallucination protection.** Prompt-only is not enough (Q245). Similarity thresholds alone must
not authorise answers (brief rule). Current claim validation is strong on *truth of quote* and weak
on *relevance*. → Honour `insufficient_evidence`; judge sufficiency on the cited **rule**, not the
chunk or its heading; require the cited rule to carry the question's subject (with abbreviation /
synonym matching) — and keep the refusal default.

**Short / informal queries.** Typos are already handled by the LLM clarify step; most short-query
failures are RC1/RC2. → Fix those first; measure before adding expansion.

**Numeric questions.** LLM arithmetic is right in the traces but unverifiable. → Deterministic
calculator for the common rule shapes (percent of an amount with min/max cap, threshold test,
duration/date comparison) using the cited figure and the question's figures, emitted as a computed
claim that cites the rule (the `_magnitude_comparison` pattern), and allow question-supplied figures
in claims when tagged as given.

**All-version queries.** Larger Top-K does not guarantee each version is represented. → Per-version
retrieval (one pass per version, ≥1 cited passage per version or "not found in vX").

## 5. Decision matrix (Phase 6)

| Problem | Current | Option A | Option B | Option C | Choice | Why |
|---|---|---|---|---|---|---|
| Short names / any document alias (KYC, HL, PL, AML, doc IDs) | trigram ≥0.8 on full name | KYC synonym list | LLM entity extraction | Aliases derived per document (title, distinctive tokens, initials, declared document ID, policy number); unique match → scope + strip from key terms | **C** | Generic for any PDF, deterministic, no per-tenant lists, no extra latency |
| Gate false refusals | bag-of-words coverage incl. scoping/framing words | remove gate | lower thresholds | Strip scoping terms; framing words by structure; abbreviation↔expansion by initials; identifier clauses exempt; judge on the cited rule | **C** | Keeps refusal safety (A/B would weaken it) |
| Look-alike answers | chunk + heading words | stricter prompt | LLM judge per answer | Rule-level retrieval + sufficiency on the cited rule (headings excluded) + honour `insufficient_evidence` | **C** (+B only if residual) | Fixes cause; no extra call |
| Retrieval recall | chunk-level hybrid | bigger top-K | cross-encoder | Rule-level unit lane (BM25 + vectors) mapped to parent chunks | **C** | Measured 4/20 → 20/20 in top 10 |
| Paraphrase | chunk vectors + lexical gate | LLM query expansion | synonym lists | Rule-level vectors + rule-level gate | **C** (A if residual) | Measured 17/20 rank 1 without an LLM call |
| Version comparison | passages; diff unreachable | LLM summarises passages | larger top-K | Route to existing deterministic diff; old→new sentence for every modified section | **C** | Already built and exact |
| All-version listing | current version only | top-K | — | Planner fix + per-version retrieval | **C** | Guarantees one answer per version |
| Calculation | figures forbidden | let LLM compute | — | Deterministic calculator + tagged question figures | **C** | Verifiable arithmetic |
| Hallucination (unanswerable) | flag ignored | LLM judge | threshold | Honour flag; absence regex; rule-level sufficiency | **C** | Bugs, not design gaps |
| Latency | fallback + clarify cascade | bigger timeout | — | Fewer false refusals; skip fallback when the reason is version-independent | **C** | Removes repeated full passes |

---

## 6. Implementation plan (for approval)

Every stage is followed by the full 333-question run plus the regression set; a stage that turns a
passing critical test into a failure is not kept.

| Stage | Change | Files | Schema/cost |
|---|---|---|---|
| 0 | Regression harness: CSV runner + grader in repo; Recall@k/MRR from evidence ranks; pytest cases for every fixed failure; baseline | `run_csv_suite.py`, `app/tests/...` | none |
| 1 | Bugs: honour `insufficient_evidence`; `_ABSENCE` adverbs; identifier clauses exempt in gate/on-topic | `rag/service.py`, `rag/validation.py` | none |
| 2 | Document/policy alias resolver (derived aliases, unique match) → policy scope + strip scoping terms from key terms; abbreviation↔expansion in `TermIndex`; planner "all N versions"; comparison framing words | `search/retrieval.py`, `rag/evidence.py`, `rag/validation.py`, `rag/query_plan.py`, `rag/service.py` | none |
| 3a | Rule-level lexical lane (units table with tsv, parent chunk id, ACL columns) | migration, ingestion chunk task, `retrieval.py` | +~2M rows, ~1 GB, backfill job |
| 3b | Rule-level vectors (benchmark 512-d `halfvec` vs 1536-d first) | migration, embeddings task | ~3.5 GB at 512-d halfvec; ~$2 embeddings |
| 4 | Reranker: re-measure; cross-encoder only if R@1 ≪ R@10 | `reranker/` | maybe API cost |
| 5–6 | Sufficiency + claim support on the cited rule (headings excluded) | `rag/evidence.py`, `rag/service.py`, `rag/validation.py` | none |
| 7 | Comparison routing to stored diff; per-version retrieval for "all versions"; sentence-level diff detail | `rag/service.py`, `versions/comparison.py`, `rag/prompts.py` | none |
| 8 | Deterministic calculator + tagged question figures | new `rag/calculate.py`, `rag/validation.py` | none |

Decisions needed from the owner: approval to start, and for Stage 3 (schema change, ~1–3.5 GB of
new index, a re-index job, ~$2 of embeddings).
