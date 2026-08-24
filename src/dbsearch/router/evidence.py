"""Evidence — the common envelope over any retrieval result (Phase E, card #98).

Generalises query.RetrievedChunk so a router can 'concat' heterogeneous results
(doc chunks now; SQL rows / records in E4) into one list the synthesizer consumes.
`provenance` is the citation shape: chunk -> {doc,title,uri,locator}; row ->
{sql,table,row_ids}. `content` is ALWAYS post-permission-trim text (LAW 2).
"""
from __future__ import annotations

from dataclasses import dataclass, field

CHUNK = "chunk"
ROW = "row"
RECORD = "record"


@dataclass
class Evidence:
    store_id: str
    business_unit: str
    kind: str                       # CHUNK | ROW | RECORD
    content: str                    # post-trim text — safe to hand to a model
    provenance: dict = field(default_factory=dict)
    score: float | None = None

    def to_dict(self) -> dict:
        return {
            "store_id": self.store_id,
            "business_unit": self.business_unit,
            "kind": self.kind,
            "content": self.content,
            "provenance": self.provenance,
            "score": self.score,
        }
