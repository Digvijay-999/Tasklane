"""Values shared by every layer that lists jobs (pure, no I/O)."""

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class JobListPosition:
    """Where a page of the newest-first job list ended: the last job's (created_at, id).

    The next page continues strictly after this position. The id breaks ties between jobs created
    at the same instant.
    """

    created_at: datetime
    job_id: uuid.UUID
