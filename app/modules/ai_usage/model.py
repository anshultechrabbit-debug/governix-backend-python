import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AIUsage(Base):
    """One call to the AI provider: what it was for, the model, and the tokens it used.

    The provider's key is shared by the whole platform, so usage is platform-wide.
    A call the provider refused for lack of credit is recorded with its error code.
    """

    __tablename__ = "ai_usage"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    provider: Mapped[str] = mapped_column(String(40))
    service: Mapped[str] = mapped_column(String(40))  # answers | embeddings
    model: Mapped[str] = mapped_column(String(120))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(80))

    __table_args__ = (Index("ix_ai_usage_created", "created_at"),)
