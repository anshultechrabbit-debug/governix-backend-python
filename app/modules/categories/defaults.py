from sqlalchemy.orm import Session

from app.modules.categories.model import Category, CategoryType
from app.modules.organizations.model import Organization

# Authority rank a new category gets from its type unless one is given.
TYPE_AUTHORITY_RANK: dict[CategoryType, int] = {
    CategoryType.REGULATORY_DOCUMENT: 100,
    CategoryType.POLICY: 80,
    CategoryType.CIRCULAR: 70,
    CategoryType.GUIDELINE: 60,
    CategoryType.SOP: 50,
    CategoryType.MANUAL: 40,
    CategoryType.PROCESS_DOCUMENT: 40,
    CategoryType.NOTICE: 30,
    CategoryType.FORM: 20,
    CategoryType.FAQ: 10,
    CategoryType.OTHER: 50,
}

# (name, slug, type, authority_rank, classification keywords)
DEFAULT_CATEGORIES: list[tuple[str, str, CategoryType, int, list[str]]] = [
    ("Regulatory Documents", "regulatory-documents", CategoryType.REGULATORY_DOCUMENT, 100, [
        "reserve bank", "rbi", "master direction", "regulation", "regulatory", "notification",
        "statutory", "act,", "compliance requirement", "regulator",
    ]),
    ("Policies", "policies", CategoryType.POLICY, 80, [
        "policy", "policy no", "policy number", "board approved", "approved by the board",
        "policy statement", "scope of the policy", "policy owner",
    ]),
    ("Circulars", "circulars", CategoryType.CIRCULAR, 70, [
        "circular", "circular no", "all branches", "hereby informed", "is hereby amended",
        "stands amended", "with immediate effect", "ref:", "reference is invited",
    ]),
    ("Guidelines", "guidelines", CategoryType.GUIDELINE, 60, [
        "guideline", "guidelines", "best practice", "should be followed",
    ]),
    ("SOPs", "sops", CategoryType.SOP, 50, [
        "standard operating procedure", "sop", "step 1", "procedure", "responsibility matrix",
        "workflow",
    ]),
    ("Manuals", "manuals", CategoryType.MANUAL, 40, ["manual", "chapter", "handbook", "table of contents"]),
    ("Process Documents", "process-documents", CategoryType.PROCESS_DOCUMENT, 40, [
        "process", "process flow", "process owner", "turnaround time", "tat",
    ]),
    ("Notices", "notices", CategoryType.NOTICE, 30, ["notice", "is hereby notified", "for information", "attention"]),
    ("Forms", "forms", CategoryType.FORM, 20, ["form", "signature", "applicant name", "date of birth", "please fill"]),
    ("FAQs", "faqs", CategoryType.FAQ, 10, ["faq", "frequently asked questions", "q:", "question", "answer"]),
]


def seed_default_categories(session: Session, organization: Organization) -> None:
    for name, slug, category_type, rank, keywords in DEFAULT_CATEGORIES:
        session.add(
            Category(
                organization_id=organization.id,
                name=name,
                slug=slug,
                category_type=category_type,
                authority_rank=rank,
                keywords=keywords,
                is_system=True,
            )
        )
    session.flush()
