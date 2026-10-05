"""Run a question CSV against the live RAG pipeline, grade it, and measure retrieval.

    .venv/bin/python run_csv_suite.py run   QUESTIONS.csv OUT.jsonl [--workers 4] [--only Q001,Q002] [--org ORG_ID]
    .venv/bin/python run_csv_suite.py grade QUESTIONS.csv OUT.jsonl [--table table.csv]
    .venv/bin/python run_csv_suite.py diff  QUESTIONS.csv BEFORE.jsonl AFTER.jsonl
    .venv/bin/python run_csv_suite.py repeat QUESTIONS.csv OUT.jsonl --only Q001,... [--times 3]

CSV columns: id, category, question, expected_answer, expected_behavior (answer | abstain |
answer_absent | correct_premise | clarify_or_cover_all | summarise), documents, versions,
clause_ids. `run` is resumable (ids already in OUT are skipped) and records, for questions
naming clause ids, the rank of the expected clause's chunk in the final evidence, so that
Recall@k and MRR can be computed without a second pass.

Grading is automatic and deliberately simple, so two runs are graded the same way: a
CORRECT answer states every figure (or, without figures, every content word) of the expected
answer. Use `diff` to review what a change moved, rather than trusting the totals alone.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

DOCS = {  # CSV document keys -> document title patterns
    "Home_Loan_Policy": "Home Loan%",
    "Personal_Loan_Policy": "Unsecured Personal Loan%",
    "KYC_AML_Policy": "Know Your Customer%",
}
DEFAULT_ORG = "2fb01ee1-3e0d-44ec-bea0-ad57ca66332f"


def _arg(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def _rows(path):
    return list(csv.DictReader(open(path, encoding="utf-8-sig")))


def _records(path):
    out = {}
    try:
        for line in open(path):
            record = json.loads(line)
            out[record["id"]] = record
    except FileNotFoundError:
        pass
    return out


# --- running ---------------------------------------------------------------------------

def _pipeline():
    from app.core.config import get_settings
    from app.core.database import create_db_engine, create_session_factory
    from app.core.model_registry import import_all_models

    import_all_models()
    from app.infrastructure.ai.embeddings.factory import create_embedder
    from app.infrastructure.ai.llm.factory import create_llm
    from app.infrastructure.ai.reranker.factory import create_reranker
    from app.infrastructure.cache.factory import create_cache

    settings = get_settings()
    session_factory = create_session_factory(create_db_engine(settings))
    return settings, session_factory, create_embedder(settings), create_llm(settings), create_reranker(settings), \
        create_cache(settings)


def _capture_evidence():
    """Record the evidence set of each answer, per worker thread."""
    from app.modules.rag import service

    local = threading.local()
    original = service.build_evidence

    def build_evidence(*args, **kwargs):
        evidence = original(*args, **kwargs)
        local.sets = getattr(local, "sets", []) + [[str(i.candidate.chunk_id) for i in evidence.items]]
        return evidence

    service.build_evidence = build_evidence
    return local


def _key_chunks(session, org, row) -> set[str]:
    from sqlalchemy import text

    keys = set()
    clauses = [c for c in row.get("clause_ids", "").split(";") if c]
    versions = [v.lstrip("v") for v in row.get("versions", "").split(";") if v]
    documents = [DOCS[d] for d in row.get("documents", "").split(";") if d in DOCS] or ["%"]
    for clause in clauses:
        for document in documents:
            statement = ("select c.id from chunks c join documents d on d.id = c.document_id "
                         "join policy_versions pv on pv.id = c.version_id "
                         "where c.organization_id = :org and d.title like :title and d.status = 'ready' "
                         "and c.text like '%' || :clause || ' %'")
            params = {"org": org, "title": document, "clause": clause}
            if versions:
                statement += " and pv.version_label = any(:versions)"
                params["versions"] = versions
            keys |= {str(r[0]) for r in session.execute(text(statement), params)}
    return keys


def run(argv):
    csv_path, out_path = argv[2], argv[3]
    workers = int(_arg(argv, "--workers", 4))
    only = set(_arg(argv, "--only", "").split(",")) - {""}
    org = _arg(argv, "--org", DEFAULT_ORG)
    times = int(_arg(argv, "--times", 1))
    rows = [r for r in _rows(csv_path) if not only or r["id"] in only]
    done = _records(out_path) if times == 1 else {}
    todo = [r for r in rows if r["id"] not in done]

    settings, session_factory, embedder, llm, reranker, cache = _pipeline()
    from sqlalchemy import select

    from app.infrastructure.cache.factory import create_cache
    from app.modules.auth.dependencies import principal_from_user
    from app.modules.rag.schema import AskRequest
    from app.modules.rag.service import RAGService
    from app.modules.users.model import User

    local = _capture_evidence()
    with session_factory() as session:
        user = session.scalars(select(User).where(User.organization_id == org, User.is_active.is_(True))).first()
        principal = principal_from_user(user, session)
        keys = {r["id"]: _key_chunks(session, org, r) for r in todo}
        session.expunge_all()

    lock = threading.Lock()
    out = open(out_path, "a" if times == 1 else "w")

    def one(row, attempt):
        local.sets = []
        started = time.perf_counter()
        record = {"id": row["id"], "attempt": attempt}
        try:
            with session_factory() as session:
                # A fresh cache per repeat: a repeated question must be answered again.
                answer_cache = create_cache(settings) if times > 1 else cache
                service = RAGService(session, session_factory, settings, answer_cache, embedder=embedder,
                                     reranker=reranker, llm_factory=lambda: llm)
                response = service.ask(principal, AskRequest(question=row["question"]))
                session.rollback()
            ranks = []
            for evidence in local.sets:
                hits = [i for i, chunk in enumerate(evidence, 1) if chunk in keys.get(row["id"], ())]
                ranks.append(hits[0] if hits else None)
            record.update(
                status=response.status, answer=response.answer, summary=response.summary,
                reason=response.no_answer.reason if response.no_answer else None,
                missing_terms=response.no_answer.missing_terms if response.no_answer else [],
                plan=response.plan.get("query_class"), warnings=response.warnings,
                sources=[{"n": s.number, "doc": s.document_title, "ver": s.version_label, "p": s.page_start,
                          "p2": s.page_end} for s in response.sources],
                key_rank=next((r for r in ranks if r), None) if keys.get(row["id"]) else "n/a",
                timings=response.timings_ms,
            )
        except Exception as exc:  # a crash is a result, not a reason to stop the run
            record.update(status="CRASH", answer=None, reason=f"{type(exc).__name__}: {exc}", sources=[])
        record["seconds"] = round(time.perf_counter() - started, 2)
        with lock:
            out.write(json.dumps(record, default=str) + "\n")
            out.flush()
        return record

    jobs = [(r, a) for a in range(1, times + 1) for r in todo]
    with ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(one, r, a) for r, a in jobs]
        for index, future in enumerate(as_completed(futures), 1):
            record = future.result()
            print(f"[{index}/{len(jobs)}] {record['id']} {record['status']:9} {record.get('reason') or '':26} "
                  f"{record['seconds']:5.1f}s {(record.get('answer') or '')[:80]!r}", flush=True)


# --- grading ---------------------------------------------------------------------------

_REFUSAL = re.compile(
    r"not found|does not (?:contain|include|have|mention|specify)|no (?:provision|information|mention)|"
    r"not (?:mentioned|specified|available|provided|covered|stated|present)|isn't|is not (?:in|part)", re.I)
_CORRECTION = re.compile(r"\b(?:incorrect|not correct|not accurate|not right|wrong|actually|rather|not\s+\d)", re.I)
_STOP = {"the", "and", "with", "for", "only", "from", "that", "this", "must", "shall", "should", "will",
         "member", "after", "before", "more", "than", "into", "under", "each", "once", "every"}


def _norm(text):
    text = (text or "").lower().replace("₹", "rs ").replace("rs.", "rs ").replace("–", "-")
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    return re.sub(r"\s+", " ", re.sub(r"(\d)\s+%", r"\1%", text))


def _figures(expected):
    text = re.sub(r"\bv\d+(\.\d+)?\b", " ", _norm(expected))
    text = re.sub(r"\b\d{1,2}-[a-z]{3}-\d{4}\b", " ", text)
    found = []
    for token in re.findall(r"\d+(?:\.\d+)?%?", text):
        if "." in token:
            pct = "%" if token.endswith("%") else ""
            token = token.rstrip("%").rstrip("0").rstrip(".") + pct
        if token not in found:
            found.append(token)
    return found


def _has(answer, figure):
    pct = figure.endswith("%")
    core = re.escape(figure.rstrip("%")) + (r"0*" if "." in figure else "")
    return re.search(rf"(?<![\d.]){core}(?:\.0+)?" + (r"\s*%" if pct else r"(?!\d)"), answer) is not None


def verdict(row, record):
    behaviour, status = row["expected_behavior"], record.get("status")
    answer = _norm(" ".join(filter(None, [record.get("answer"), record.get("summary")])))
    if status == "CRASH":
        return "ERROR"
    if behaviour == "abstain":
        return "CORRECT" if status == "no_answer" or _REFUSAL.search(answer) else "WRONG"
    if behaviour == "answer_absent":
        if status == "no_answer":
            return "PARTIAL"
        return "CORRECT" if _REFUSAL.search(answer) or re.search(r"\bno\b", answer) else "WRONG"
    if status != "answered":
        return "NO ANSWER"
    if re.search(r"not (?:explicitly |specifically )?(?:stated|specified|mentioned)", answer):
        return "NO ANSWER"
    figures = _figures(row["expected_answer"])
    if figures:
        hit = [f for f in figures if _has(answer, f)]
    else:
        words = [w for w in re.findall(r"[a-z][a-z-]{3,}", _norm(row["expected_answer"])) if w not in _STOP]
        figures, hit = words, [w for w in words if w in answer]
    if behaviour == "correct_premise" and not _CORRECTION.search(answer):
        return "PARTIAL" if hit else "WRONG"
    if figures and len(hit) == len(figures):
        return "CORRECT"
    return "PARTIAL" if hit else "WRONG"


def _retrieval(rows, records):
    # Only questions whose expected clause is known; a clause never in the evidence counts as a miss.
    ranks = [records[r["id"]].get("key_rank") for r in rows if records[r["id"]].get("key_rank", "n/a") != "n/a"]
    ranks = [r if isinstance(r, int) else None for r in ranks]
    if not ranks:
        return {}
    metrics = {f"recall@{k}": sum(1 for r in ranks if r and r <= k) / len(ranks) for k in (1, 3, 5, 10)}
    metrics["mrr"] = sum(1 / r for r in ranks if r) / len(ranks)
    metrics["n"] = len(ranks)
    return metrics


def _percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0


def grade(argv):
    rows, records = _rows(argv[2]), _records(argv[3])
    rows = [r for r in rows if r["id"] in records]
    verdicts = {r["id"]: verdict(r, records[r["id"]]) for r in rows}
    total = Counter(verdicts.values())
    print(f"TOTAL {len(rows)}  " + "  ".join(f"{k}={total[k]}" for k in ("CORRECT", "PARTIAL", "WRONG", "NO ANSWER", "ERROR")))
    per = defaultdict(Counter)
    for r in rows:
        per[r["category"]][verdicts[r["id"]]] += 1
    for category in sorted(per, key=lambda c: (int(c.split("_")[0]) if c.split("_")[0].isdigit() else 0, c)):
        c = per[category]
        print(f"  {category:32} {sum(c.values()):3}  C={c['CORRECT']:3} P={c['PARTIAL']:3} W={c['WRONG']:3} N={c['NO ANSWER']:3}")
    retrieval = _retrieval(rows, records)
    if retrieval:
        print("evidence ranking of the expected clause: " + "  ".join(
            f"{k}={v:.3f}" if k != "n" else f"n={v}" for k, v in retrieval.items()))
    seconds = [records[r["id"]]["seconds"] for r in rows]
    print(f"latency p50={_percentile(seconds, .5)}s p95={_percentile(seconds, .95)}s p99={_percentile(seconds, .99)}s")
    print("no-answer reasons:", dict(Counter(records[r["id"]].get("reason") for r in rows
                                             if records[r["id"]].get("status") == "no_answer")))
    if table := _arg(argv, "--table"):
        with open(table, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["id", "category", "question", "expected_answer", "actual_answer", "status",
                             "reason", "verdict", "key_rank", "sources", "seconds"])
            for r in rows:
                rec = records[r["id"]]
                writer.writerow([r["id"], r["category"], r["question"], r["expected_answer"], rec.get("answer") or "",
                                 rec.get("status"), rec.get("reason") or "", verdicts[r["id"]], rec.get("key_rank"),
                                 "; ".join(f"{s['doc']} v{s['ver']} p{s['p']}" for s in rec.get("sources", [])),
                                 rec.get("seconds")])
    return verdicts


def diff(argv):
    rows = {r["id"]: r for r in _rows(argv[2])}
    before, after = _records(argv[3]), _records(argv[4])
    order = ["ERROR", "WRONG", "NO ANSWER", "PARTIAL", "CORRECT"]
    better, worse = [], []
    for qid in rows:
        if qid not in before or qid not in after:
            continue
        a, b = verdict(rows[qid], before[qid]), verdict(rows[qid], after[qid])
        if a != b:
            (better if order.index(b) > order.index(a) else worse).append((qid, a, b))
    for label, items in (("IMPROVED", better), ("REGRESSED", worse)):
        print(f"\n{label} ({len(items)})")
        for qid, a, b in items:
            print(f"  {qid} {a} -> {b} | {rows[qid]['question'][:80]}")
            print(f"      expected: {rows[qid]['expected_answer'][:100]}")
            print(f"      now:      {(after[qid].get('answer') or after[qid].get('reason') or '')[:220]}")


def repeat(argv):
    run(argv)
    rows = {r["id"]: r for r in _rows(argv[2])}
    by_id = defaultdict(list)
    for line in open(argv[3]):
        record = json.loads(line)
        by_id[record["id"]].append(verdict(rows[record["id"]], record))
    stable = sum(1 for v in by_id.values() if len(set(v)) == 1)
    print(f"\nconsistent verdicts: {stable}/{len(by_id)}")
    for qid, values in by_id.items():
        if len(set(values)) > 1:
            print(f"  {qid}: {values}")


if __name__ == "__main__":
    sys.path.insert(0, ".")
    {"run": run, "grade": grade, "diff": diff, "repeat": repeat}[sys.argv[1]](sys.argv)
