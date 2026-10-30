# Governix production audit (Phase 1)

Date: 2026-10-30. Scope: Phase 1 of the production-hardening brief: the whole platform as built,
not only the RAG pipeline. It extends `docs/rag-audit.md` (2026-10-05), which audited the answer
pipeline in depth; that document's root causes are re-checked here against the current code.

How this was checked: by reading the code at commit `db89fe0` (plus the uncommitted v71 answer
fixes), by requests to the live server from outside (`89.116.122.215`), and from server command
output pasted on 2026-10-14. **No tests were run and no model was called**, as the project rules
require without the owner's go-ahead. So nothing here claims a measured accuracy figure newer than
2026-10-05.

---

## 1. Findings by severity

### CRITICAL

| # | Finding | Evidence | Fix |
|---|---|---|---|
| C1 | **The live site has no HTTPS.** Passwords, access and refresh tokens, questions and policy text cross the internet in clear text. | `https://89.116.122.215:7001` does not answer; the site is plain `http` on port 7001. | A domain name and a Let's Encrypt certificate on nginx; redirect HTTP to HTTPS; then HSTS. Needs: the domain to use. |
| C2 | **The API port (7000) is reachable directly from the internet**, bypassing nginx: its body-size limit, any future TLS, headers and rate limits. | `http://89.116.122.215:7000/health` answers 200 from outside. | Bind uvicorn to `127.0.0.1` (`--host 127.0.0.1`) and close 7000 in the firewall. |
| C3 | **Answer quality is unmeasured since 2026-10-05.** The last full run was 94 correct, 53 partial, 45 wrong, 141 no answer of 333 (42% refusals). Eighteen answer releases (v54 to v71) have shipped since, each checked on individual questions only. Users still report "no answer" for plain questions (fixed in v71 for five question types, also unmeasured). No improvement can be claimed, and none can be ruled out. | `docs/rag-audit.md` §0; `ANSWER_CACHE_VERSION` history in `rag/service.py`. | A budgeted run of the existing 333-question harness (`run_csv_suite.py`, which already grades and computes Recall@k and MRR) to set today's baseline, then after each change. Needs: an OpenAI budget. |

### HIGH

| # | Finding | Evidence | Fix |
|---|---|---|---|
| H1 | **The live server runs with `APP_ENV` unset, so in "local" mode.** The production safeguards in `config.py` are off: a required `JWT_SECRET`, warnings for an in-process worker and a memory cache. If `JWT_SECRET` is also unset, tokens are signed with a key made at start-up, so every restart signs everyone out. | Server output 2026-10-14: `grep -E '^(WORKER_MODE\|LOG_LEVEL\|APP_ENV)=' .env` printed only `WORKER_MODE`. | Set `APP_ENV=production` and a 32+ character `JWT_SECRET` in the server `.env`; restart. |
| H2 | **Interactive API documentation is public**: `/docs`, `/api/docs` and `/api/openapi.json` list every route and schema to anyone. | All three return 200 from outside. | **Done in code**: off in production unless `API_DOCS_ENABLED=true` (`app/main.py`). Takes effect once H1 is fixed. |
| H3 | **API and worker run as root, and the worker parses uploaded PDFs** with MuPDF, a C library. A malicious PDF that exploits it would own the whole server, which also runs MongoDB and another company's app. | Memory notes of the 2026-10-12 deployment: "run as root". | A `governix` system user owning only the app and storage directories; systemd `User=`, `NoNewPrivileges=`, `ProtectSystem=strict`, `ReadWritePaths=` for storage. |
| H4 | **No automatic tests.** About 600 tests exist (unit 372, integration 138, rag 44, security 23, uploads 21, performance 2) but there is no CI, and the project rule is not to run them without asking, so their current pass state is unknown. | No `.github/`; `app/tests/`. | A CI workflow running the local-provider tests (no OpenAI cost) on every push; the 333-question run stays manual and budgeted. Needs: permission to run `pytest`, and a GitHub Actions decision. |
| H5 | **Retrieval granularity (October RC3) is still open.** A chunk holds 7–8 unrelated rules, so the rule asked about is often not ranked first, and checks judge a chunk, not the sentence that answers. The October benchmark found the expected rule in the top 10 for 4 of 20 failing questions at chunk level and 20 of 20 at rule level. | No rule-level table or lane in `search/`; `docs/rag-audit.md` RC3, Stage 3. | Stage 3 of the October plan (rule-level lexical lane, then vectors). Needs: owner approval for the schema change and re-index (~1–3.5 GB, a backfill job, ~$2 of embeddings). |
| H6 | **Only PDF can be ingested.** DOCX and XLSX policies are rejected. Scanned PDFs need Tesseract, which the live server did not have on 2026-10-12, so scanned pages fail there. | `documents/service.py:131` "Only PDF documents are supported"; memory of 2026-10-12. | Check `which tesseract` on the server and install it; add DOCX (python-docx) and XLSX (openpyxl) readers that produce the same page/line structure. Needs: approval of two new dependencies. |
| H7 | **Capacity does not match the enterprise target.** One shared vCPU (6–24% stolen by neighbours), one uvicorn process, an in-process cache and rate limiter. A 3,170-page upload took ~10 minutes (before the 2.3× extraction speed-up). Answers took p50 7.6 s and p95 32 s in October. 50,000 documents or 500,000 pages are not reachable on this host. | Live measurements 2026-10-14; `docs/rag-audit.md` §1.3. | Size a production host (4+ vCPUs: the PDF reader runs one process per core beyond the first, up to 8); Redis for the shared cache and limiter once there is more than one API process. Load test before choosing. |

### MEDIUM

| # | Finding | Evidence | Fix |
|---|---|---|---|
| M1 | No limit on login attempts per client. Accounts lock after 5 failures, so one attacker can try a common password on every account, and can lock any user out on purpose. | `auth/service.py`: per-account lockout only. | `limit_req` on `/api/auth/` in nginx, which sees real client addresses. (Not done in the app: behind nginx it may see every visitor as 127.0.0.1.) |
| M2 | No security headers on the site: Content-Security-Policy, X-Frame-Options, X-Content-Type-Options, Referrer-Policy (and HSTS after C1). The refresh token in `sessionStorage` is exposed to any injected script, which a CSP limits. | Response headers from `:7001/`. | Add to the nginx server block (snippet in §4). |
| M3 | `/api/metrics` is public: request counts and total durations. | Returns counters from outside. | Allow only from the monitoring host, or behind auth. |
| M4 | Observability is thin. Metrics are two counters with every API route labelled `api` (no per-route latency, no histograms). The `ai.query` audit record has the plan, checks, retrieved and cited chunk ids, latency, model and token usage, but not the cost, nor (until now) which answer behaviour produced it. | `core/metrics.py`; `RAGService._audit`. | **Done**: the audit record now stores `behaviour` (the answer release). Next: per-route latency histograms; cost per query from `ai_usage` prices. |
| M5 | The answer prompt is ~3,600 words (~5,000 tokens) on every question, grown rule by rule from v54 to v71. It costs money and latency, and nobody has reviewed it whole for duplicate or contradictory rules. | `SYSTEM_PROMPT` 21,476 characters; the other prompts are 70–260 words. | Phase 7: review against the validators; move rules that code already enforces out of the prompt; measure each removal with the 333-question run. |
| M6 | The reranker is lexical (word overlap), not a cross-encoder (October RC10). | `infrastructure/ai/reranker/local.py`. | Measure R@1 against R@10 first (the harness records ranks); add a cross-encoder only if the gap is large. |
| M7 | Amendments apply per clause only when a person confirms the amending relationship with clause numbers. There is no automatic detection of which clauses a later document supersedes, and no authority order between policies beyond a category's rank. | `rag/evidence.py` `_amendment_candidates`, `amended_by`. | Phase 3: suggest clause scopes from the amending document's text; show them for confirmation. |
| M8 | The server has an OS update waiting for a reboot, and an unattended upgrade that failed. | Login banner, 2026-10-14. | Reboot at a quiet time; read `/var/log/unattended-upgrades/unattended-upgrades.log`. |

### LOW

| # | Finding | Fix |
|---|---|---|
| L1 | nginx announces its version (`nginx/1.24.0 (Ubuntu)`). | `server_tokens off;` |
| L2 | Bulk upload sends each PDF twice (title check, then upload). Measured at 1–2 s for 9.6 MB, so minor. | Upload once to a holding area, if it starts to matter. |
| L3 | Versions of one policy become ready together in a bulk upload: the newest names the policy and older dates are inferred from newer ones. | A design change, owner decision (`AGENTS.md`, "Ingestion speed"). |

---

## 2. What works and should be kept

* **Tenant isolation.** `visible_clause(...)` is inside every retrieval lane's SQL, re-checked when
  citations are verified. Cache keys carry organization, branch, department, permission scope and
  knowledge version. A User sees only assigned policies. 23 security tests cover it (state unknown,
  see H4).
* **Authentication.** Argon2 passwords (12+ characters); constant-time handling of unknown emails;
  lockout; rotating refresh tokens with reuse detection that ends the whole session family; a
  token version that ends sessions on deactivation, role or password change; the principal re-read
  from the database on every request.
* **Answer safety.** Claims are validated against their passages (figures, word support, subjects,
  versions, negation, absence statements). Citations are re-verified in the database. The model's
  `insufficient_evidence` is respected. There is a meaning check, a reasoning check, a summary check,
  and prompt/context-injection removal. Refusals say what was read. v71 closes the "close in meaning
  but about something else" gap.
* **Calculations.** Deterministic, `Decimal`, a safe `ast` evaluator. Every figure must come from the
  evidence or the question. Stated results are recomputed (wrong ones removed), the result leads the
  answer, and the method is reasoning-checked.
* **Versions.** Half-open effective periods (`effective_from`, exclusive `effective_to`), as-of
  queries, earlier-version fallback with a note, a deterministic stored diff for comparisons, and
  questions with two or more dates.
* **Retrieval.** Hybrid (exact identifiers and clause numbers, keyword with IDF weighting,
  section-level, vector by HNSW), weighted rank fusion, policy aliases derived from each policy's
  own data (now also misspelled names), and the front matter for questions about a document.
* **Ingestion.** Durable Postgres queue (SKIP LOCKED, leases, idempotent steps), separate worker
  lanes for CPU and OpenAI-bound jobs, embedding token budget, embedding cache, and 2.3× faster
  table extraction verified identical on 5,374 pages.
* **Harness.** `run_csv_suite.py` runs, grades, diffs and computes Recall@k and MRR. It only needs a
  budget.

## 3. Status of the October plan (`docs/rag-audit.md` §6)

| Stage | What | Status |
|---|---|---|
| 0 | Regression harness | Built (`run_csv_suite.py`); no current baseline (C3) |
| 1 | Honour `insufficient_evidence`, absence statements, identifier clauses | Done |
| 2 | Policy aliases, scoping words out of key terms, planner fixes | Done (misspelled names added in v71) |
| 3 | Rule-level index | **Not started**: needs schema approval (H5) |
| 4 | Reranker | Not started: measure first (M6) |
| 5–6 | Sufficiency on the cited rule | Partly: meaning and reasoning checks; full version needs Stage 3 |
| 7 | Comparison routing to the stored diff; per-version retrieval | Done |
| 8 | Deterministic calculator | Done (`Decimal`) |

## 4. Server configuration for C1, C2, M1–M3 and L1

For `/etc/nginx/sites-available/governix`, once a domain points at the server. Replace
`policy.example.com`.

```nginx
limit_req_zone $binary_remote_addr zone=governix_login:10m rate=10r/m;

server {
    listen 80;
    server_name policy.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name policy.example.com;
    ssl_certificate     /etc/letsencrypt/live/policy.example.com/fullchain.pem;   # certbot --nginx
    ssl_certificate_key /etc/letsencrypt/live/policy.example.com/privkey.pem;

    server_tokens off;
    client_max_body_size 1024m;
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    add_header Content-Security-Policy "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'" always;

    root /var/www/governix-frontend/dist;
    location / { try_files $uri /index.html; }

    location /api/auth/ {
        limit_req zone=governix_login burst=5 nodelay;
        proxy_pass http://127.0.0.1:7000/auth/;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location /api/metrics { allow 127.0.0.1; deny all; proxy_pass http://127.0.0.1:7000/metrics; }
    location /api/ {
        proxy_pass http://127.0.0.1:7000/;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;          # streamed answers
        proxy_read_timeout 300s;
    }
}
```

In `governix-api.service`, start uvicorn on `--host 127.0.0.1 --port 7000`, then `ufw deny 7000`.
In `.env`, set `APP_ENV=production` and a `JWT_SECRET` of 32+ random characters (`openssl rand -hex 32`).
The frontend root path above is an example; keep the one nginx uses today.

## 5. Plan (phase by phase, each one approved before it starts)

| Order | Phase | Work | Needs from the owner |
|---|---|---|---|
| 1 | Server security | C1, C2, H1, H3, M1–M3, M8, L1 (§4) | A domain; someone with server access to apply it |
| 2 | Measurement | Run the existing tests; add CI; run the 333-question baseline; record per-type accuracy, refusal rate, Recall@k/MRR, latency, cost | Permission to run `pytest`; an OpenAI budget for one baseline run |
| 3 | Retrieval (Stage 3) | Rule-level lexical lane, then vectors; judge sufficiency on the cited rule | Schema approval, re-index window |
| 4 | Ingestion formats | DOCX, XLSX; OCR on the server | Approval of `python-docx` and `openpyxl` |
| 5 | Prompt review | Shorten `SYSTEM_PROMPT` where code enforces the rule; resolve duplicates; measure each change | — (uses Phase 2's runs) |
| 6 | Versions and amendments | Suggested clause scopes for amendments; authority order across policies | Decisions on authority rules |
| 7 | Observability | Per-route latency histograms, cost per query, a dashboard | — |
| 8 | Capacity | Load test, host sizing, Redis when more than one API process | Hosting budget |

Every answer change is measured with the Phase 2 run before and after. A change that turns a passing
question into a failing one is not kept.

## 6. Changes made during this audit

* `app/main.py`, `app/core/config.py`: API docs off in production unless `API_DOCS_ENABLED=true` (H2).
* `app/modules/rag/service.py`: the `ai.query` audit record stores `behaviour` (the answer release) (M4).

Neither changes an API response or the database.
