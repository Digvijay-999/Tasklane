"""SQLAlchemy declarative base shared by all models."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

# Every constraint and index gets a predictable name. Postgres would otherwise invent names, and
# a migration cannot reliably drop (downgrade) a constraint whose name it doesn't know.
# CHECK constraints are declared with a short name such as "status" and become "ck_jobs_status".
NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Default column types for Python annotations, so `Mapped[datetime]` always means
    # "timestamp with time zone" and `Mapped[str]` means unbounded `text`.
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        str: Text(),
        dict[str, Any]: JSONB(),
    }
