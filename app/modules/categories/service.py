from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import translate_unique_violation
from app.core.exceptions import ConflictError, NotFoundError
from app.modules.audit.service import record_event
from app.modules.auth.permissions import Principal
from app.modules.auth.scope import tenant_id
from app.modules.categories.defaults import TYPE_AUTHORITY_RANK
from app.modules.categories.model import Category
from app.modules.categories.repository import CategoryRepository
from app.modules.categories.schema import CategoryCreate, CategorySummary, CategoryUpdate
from app.modules.categories import contents


def _clean_keywords(keywords: list[str]) -> list[str]:
    return sorted({k.strip().lower() for k in keywords if k.strip()})


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:90].strip("-")
    return slug if len(slug) >= 2 else "category"


class CategoryService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = CategoryRepository(session)

    def list(self, principal: Principal, *, include_inactive: bool) -> list[Category]:
        return self.repository.list(tenant_id(principal), include_inactive=include_inactive)

    def get(self, principal: Principal, category_id: uuid.UUID) -> Category:
        category = self.repository.get_in_org(tenant_id(principal), category_id)
        if category is None:
            raise NotFoundError("Category not found.")
        return category

    def overview(self, principal: Principal, *, include_inactive: bool) -> list[CategorySummary]:
        return contents.summaries(self.session, principal, self.list(principal, include_inactive=include_inactive))

    def summary(self, principal: Principal, category_id: uuid.UUID) -> CategorySummary:
        [summary] = contents.summaries(self.session, principal, [self.get(principal, category_id)])
        return summary

    def create(self, principal: Principal, data: CategoryCreate) -> Category:
        organization_id = tenant_id(principal)
        self._ensure_name_free(organization_id, data.name)
        if data.slug is not None and self.repository.slug_taken(organization_id, data.slug):
            raise ConflictError("A category with this slug already exists.")
        category = Category(
            organization_id=organization_id,
            name=data.name,
            slug=data.slug or self._free_slug(organization_id, _slugify(data.name)),
            description=(data.description or "").strip() or None,
            category_type=data.category_type,
            authority_rank=data.authority_rank if data.authority_rank is not None else TYPE_AUTHORITY_RANK[data.category_type],
            keywords=_clean_keywords(data.keywords),
            is_active=data.is_active,
        )
        with translate_unique_violation(self.session, f'A category named "{data.name}" already exists.'):
            self.session.add(category)
            self.session.flush()
            record_event(
                self.session, "category.created", actor=principal, resource_type="category",
                resource_id=category.id,
                details={"name": category.name, "slug": category.slug, "type": category.category_type},
            )
            self.session.commit()
        return category

    def update(self, principal: Principal, category_id: uuid.UUID, data: CategoryUpdate) -> Category:
        category = self.get(principal, category_id)
        changes = data.model_dump(exclude_unset=True)
        # Only the description may be cleared; a null elsewhere means "unchanged".
        changes = {k: v for k, v in changes.items() if v is not None or k == "description"}
        if "description" in changes:
            changes["description"] = (changes["description"] or "").strip() or None
        if "name" in changes and changes["name"].lower() != category.name.lower():
            self._ensure_name_free(category.organization_id, changes["name"], exclude=category.id)
        if "keywords" in changes:
            changes["keywords"] = _clean_keywords(changes["keywords"])
        for field, value in changes.items():
            setattr(category, field, value)
        with translate_unique_violation(self.session, "A category with this name already exists."):
            record_event(
                self.session, "category.updated", actor=principal, resource_type="category",
                resource_id=category.id, details={"changes": {k: str(v) for k, v in changes.items()}},
            )
            self.session.commit()
        return category

    def _ensure_name_free(self, organization_id: uuid.UUID, name: str, exclude: uuid.UUID | None = None) -> None:
        query = select(Category.id).where(
            Category.organization_id == organization_id, func.lower(Category.name) == name.lower()
        )
        if exclude is not None:
            query = query.where(Category.id != exclude)
        if self.session.scalar(query.limit(1)) is not None:
            raise ConflictError(f'A category named "{name}" already exists.', code="DUPLICATE_CATEGORY")

    def _free_slug(self, organization_id: uuid.UUID, base: str) -> str:
        slug, suffix = base, 2
        while self.repository.slug_taken(organization_id, slug):
            slug, suffix = f"{base}-{suffix}", suffix + 1
        return slug
