"""Imports every component so `infra.Base.metadata` is complete.

Components own their ORM rows privately and nothing imports them for their
tables, so the metadata is only fully populated once each module has been
imported. Alembic's `env.py` needs that for autogenerate, and the test fixture
needs it to create the schema. Both import this module instead of keeping two
drifting lists.

Add one line here per component that owns a table.
"""

from __future__ import annotations

import infra  # noqa: F401  (re-exported for convenience)

# Components with tables, added as each phase lands:
# import records   # noqa: F401
# import moods     # noqa: F401
# import sessions  # noqa: F401
# import recommendations  # noqa: F401
# import sync      # noqa: F401

metadata = infra.Base.metadata
