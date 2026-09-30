"""Check the suite's expected answers against the actual indexed text."""
import re
import sys
from sqlalchemy import create_engine, text

from app.core.config import get_settings

e = create_engine(get_settings().DATABASE_URL)

PROBES = [
    ("acronym row", "text ilike '%' || :t || '%'"),
    ("title", "text ilike '%national electricity plan%'"),
    ("170 schemes", "text ilike '%170%'"),
    ("3,13,950", "text ilike '%3,13,950%'"),
    ("1,61,854", "text ilike '%1,61,854%'"),
    ("500 GW non-fossil", "text ilike '%500 GW%'"),
    ("60 GW rooftop", "text ilike '%60 GW%'"),
    ("Shankar Sharma", "text ilike '%Shankar Sharma%'"),
    ("KPTCL", "text ilike '%KPTCL%'"),
    ("AEML", "text ilike '%AEML%'"),
    ("Prayas", "text ilike '%Prayas%'"),
    ("forest routing", "text ilike '%non-forest%'"),
    ("nine scenarios", "text ilike '%nine scenarios%'"),
    ("Section 3(4)", "text ilike '%3(4)%'"),
    ("October 2024", "text ilike '%October 2024%'"),
]


def main(only=None):
    with e.connect() as c:
        for label, where in PROBES:
            if ":t" in where:
                continue
            n = c.execute(text(f"select count(*) from chunks where {where}")).scalar()
            print(f"{label:22} chunks={n}")
        # acronym rows
        print("\n--- glossary rows for suite acronyms ---")
        for acro in ["BESS", "HVDC", "HVAC", "STATCOM", "SVC", "FACTS", "ISTS", "CTU",
                     "STU", "TBCB", "OSOWOG", "REZ", "DLR", "PMU", "GEC"]:
            rows = c.execute(text("""
                select d.title, ch.text from chunks ch join documents d on d.id=ch.document_id
                where d.status='ready' and ch.text ~* ('(^|\\n)\\s*' || :a || '\\s*(-\\u2014\\u2013|:|\\n)')
            """), {"a": acro}).all()
            found = []
            for title, txt in rows:
                lines = [l.strip() for l in (txt or "").splitlines() if l.strip()]
                for i, line in enumerate(lines):
                    m = re.fullmatch(rf"{acro}\s*(?:[-—–:]\s*|\s{{2,}})(.+)", line, re.I)
                    if m:
                        found.append(m.group(1).strip()[:70])
                    elif line.upper() == acro and i + 1 < len(lines):
                        found.append(lines[i + 1][:70])
            print(f"  {acro:8} {found[:2] if found else '*** NOT FOUND ***'}")


if __name__ == "__main__":
    main()
