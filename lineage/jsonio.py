from __future__ import annotations

import json
from typing import List

from .models import QuerySpec


def load_queries(path: str) -> List[QuerySpec]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("queries", [])
    out: List[QuerySpec] = []
    for obj in items:
        out.append(
            QuerySpec(
                alias=obj.get("alias"),
                sql=obj["query"],
                type=obj.get("type"),
            )
        )
    return out


def find_sink_alias(specs: List[QuerySpec]) -> str:
    for s in specs:
        if s.type == "sink":
            return s.alias
    # Fallback to last alias if no explicit sink
    if specs:
        return specs[-1].alias
    raise ValueError("No queries found")

