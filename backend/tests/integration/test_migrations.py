"""Migration tests: the round trip works, and the migrations agree with the models."""

import re

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.db.base import Base
from app.db.session import create_db_engine
from app.domain.enums import (
    ArtifactType,
    AttemptOutcome,
    AttemptTrigger,
    JobStatus,
    OutboxTask,
    UploadKind,
    UploadStatus,
    WorkflowType,
)
from app.domain.state_machine import TERMINAL_STATUSES
from tests.db_utils import alembic_config, recreate_database, run_alembic, settings_for_database

APP_TABLES = {"uploads", "jobs", "job_attempts", "job_events", "artifacts", "outbox"}


@pytest.fixture
def empty_migrations_engine(dev_settings):
    """A brand-new, empty database just for the round-trip test (it downgrades, which would
    otherwise wipe the shared test schema mid-run)."""
    settings = settings_for_database(dev_settings, f"{dev_settings.postgres_db}_migrations_test")
    recreate_database(settings, dev_settings.postgres_db)
    engine = create_db_engine(settings)
    yield engine
    engine.dispose()


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _schema_differences(engine) -> list:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_server_default": True})
        return compare_metadata(context, Base.metadata)


def _schema_snapshot(engine) -> dict[str, set]:
    """Everything Postgres reports about the app tables, in comparable form.

    Alembic's comparison skips CHECK bodies and is unreliable for partial-index predicates, so
    this compares Postgres's own rendering of every constraint, index, and column.
    """
    with engine.connect() as connection:
        constraints = connection.execute(
            text(
                "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
                "FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
                "AND contype <> 'n' AND conrelid::regclass::text <> 'alembic_version'"
            )
        ).all()
        indexes = connection.execute(
            text(
                "SELECT tablename, indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        ).all()
        columns = connection.execute(
            text(
                "SELECT table_name, column_name, data_type, is_nullable, column_default, "
                "is_identity, identity_generation FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
            )
        ).all()
    return {
        "constraints": set(constraints),
        "indexes": set(indexes),
        "columns": set(columns),
    }


def test_upgrade_downgrade_upgrade_round_trip(empty_migrations_engine):
    engine = empty_migrations_engine
    assert _tables(engine) == set()

    run_alembic(engine, "upgrade", "head")
    assert _tables(engine) == APP_TABLES | {"alembic_version"}

    run_alembic(engine, "downgrade", "base")
    # Only Alembic's bookkeeping table survives a full downgrade, and it records no version.
    assert _tables(engine) == {"alembic_version"}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 0

    run_alembic(engine, "upgrade", "head")
    assert _tables(engine) == APP_TABLES | {"alembic_version"}
    assert _schema_differences(engine) == []


def test_models_match_migrations(migrated_engine):
    """Fails if someone changes a model without writing a migration (or vice versa).

    Note: Alembic's comparison does not cover CHECK constraints; those are checked below.
    """
    assert _schema_differences(migrated_engine) == []


@pytest.fixture
def models_only_engine(dev_settings):
    """A database built directly from the models with create_all (no migrations)."""
    settings = settings_for_database(dev_settings, f"{dev_settings.postgres_db}_models_test")
    recreate_database(settings, dev_settings.postgres_db)
    engine = create_db_engine(settings)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.mark.parametrize("kind", ["constraints", "indexes", "columns"])
def test_migrations_build_exactly_the_schema_the_models_describe(
    migrated_engine, models_only_engine, kind
):
    """Strict check: every constraint definition (including CHECK bodies), every index
    definition (including partial-index WHERE clauses), and every column's type, nullability,
    default, and identity setting must be identical."""
    from_migrations = _schema_snapshot(migrated_engine)[kind]
    from_models = _schema_snapshot(models_only_engine)[kind]
    assert from_migrations - from_models == set(), "only in the migrated database"
    assert from_models - from_migrations == set(), "only in the models"


def test_migration_history_has_a_single_head():
    """Two migrations with the same parent (e.g. from two branches) would make `upgrade head`
    ambiguous. Merge them before committing."""
    assert len(ScriptDirectory.from_config(alembic_config()).get_heads()) == 1


def _check_constraints(engine) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE contype = 'c' AND connamespace = 'public'::regnamespace"
            )
        ).all()
    return dict(rows)


def test_check_constraint_names_match_models(migrated_engine):
    from sqlalchemy import CheckConstraint

    model_names = {
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert set(_check_constraints(migrated_engine)) == model_names


ENUM_CONSTRAINTS = {
    "ck_uploads_kind": UploadKind,
    "ck_uploads_status": UploadStatus,
    "ck_jobs_status": JobStatus,
    "ck_jobs_workflow_type": WorkflowType,
    "ck_jobs_finished_iff_terminal": TERMINAL_STATUSES,
    "ck_job_attempts_trigger": AttemptTrigger,
    "ck_job_attempts_outcome": AttemptOutcome,
    "ck_artifacts_artifact_type": ArtifactType,
    "ck_outbox_task_name": OutboxTask,
}


@pytest.mark.parametrize("constraint_name", sorted(ENUM_CONSTRAINTS))
def test_database_value_lists_match_python_enums(migrated_engine, constraint_name):
    """The migration's frozen value lists must equal the app's enums. When an enum gains a value,
    this fails until a migration updates the database's CHECK constraint."""
    definition = _check_constraints(migrated_engine)[constraint_name]
    in_database = set(re.findall(r"'([a-z_]+)'::text", definition))
    in_python = {str(value) for value in ENUM_CONSTRAINTS[constraint_name]}
    assert in_database == in_python
