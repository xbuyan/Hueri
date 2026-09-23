"""
The normalized record every source (UNGM, PPIP, World Bank STEP, AfDB
DACON, ...) gets flattened into. Step 2 (the Gemini scoring agent) is
written once against this shape and reused for every source — this is
the "same shape everywhere" design decision from the original plan.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class TenderNotice:
    source: str                      # e.g. "ungm", "ppip", "worldbank_step"
    title: str
    url: str
    buyer: Optional[str] = None      # issuing agency / procuring entity
    deadline: Optional[str] = None   # ISO date string if known, else raw text
    published_date: Optional[str] = None
    notice_type: Optional[str] = None
    country: Optional[str] = None
    reference: Optional[str] = None  # the source's own notice ID/reference
    full_text: str = ""              # anything else useful for the scoring agent
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def dedup_key(self) -> str:
        """
        Stable identifier for the 'seen notices' database (Step 4).
        Prefer the source's own reference/URL over a text hash, since
        titles can be re-published with tiny formatting changes.
        """
        basis = self.reference or self.url or self.title
        return hashlib.sha256(f"{self.source}:{basis}".encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)
