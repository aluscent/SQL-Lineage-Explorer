from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

from .build import build_lineage, build_parsed_queries, build_schemas, lineage_for_sink
from .jsonio import find_sink_alias, load_queries
from .models import BuildContext


def main() -> None:
    ap = argparse.ArgumentParser(description="Column-level lineage from sink to sources")
    ap.add_argument("queries_json", help="Path to queries.json")
    ap.add_argument("--sink", help="Sink alias (override)" )
    ap.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")
    ap.add_argument("--resolve", help="Path to JSON mapping for disambiguation resolutions (key->qualifier)")
    args = ap.parse_args()

    specs = load_queries(args.queries_json)
    sink = args.sink or find_sink_alias(specs)

    ctx = BuildContext()
    if args.resolve:
        try:
            with open(args.resolve, "r", encoding="utf-8") as rf:
                ctx.disambiguation = json.load(rf)
        except Exception as e:
            print(f"Failed to load resolutions: {e}")
    ctx.alias_to_query = build_parsed_queries(specs)
    build_schemas(ctx)
    build_lineage(ctx)
    lineage = lineage_for_sink(ctx, sink)

    for alias, parsed in ctx.alias_to_query.items():
        for warning in parsed.warnings:
            print(f"{alias}: {warning}", file=sys.stderr)
    if ctx.ambiguities and not args.as_json:
        print("Ambiguities detected; disambiguation required:")
        for a in ctx.ambiguities:
            print(f" - {a.message}  [key={a.key}] options={a.options}")

    if args.as_json:
        payload: Dict[str, Any] = {
            "sink": sink,
            "diagnostics": {a: {"parser": p.parser_mode, "warnings": p.warnings}
                            for a, p in ctx.alias_to_query.items()},
            "columns": [
                {
                    "name": c.sink_column,
                    "sources": sorted(c.sources),
                    "expr": c.transform_summary,
                    "influences": {
                        "where": [r.fully_qualified() for r in c.influences.where_cols],
                        "join": [r.fully_qualified() for r in c.influences.join_cols],
                        "group_by": [r.fully_qualified() for r in c.influences.group_by_cols],
                        "order_by": [r.fully_qualified() for r in c.influences.order_by_cols],
                    },
                    "hops": [
                        {"alias": h.alias, "column": h.column, "expr": h.expr, "note": h.note}
                        for h in c.hops
                    ],
                }
                for c in lineage
            ],
            "ambiguities": [
                {"key": a.key, "kind": a.kind, "alias": a.alias, "output_col": a.output_col, "ref_name": a.ref_name, "options": a.options, "message": a.message}
                for a in ctx.ambiguities
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"Sink: {sink}")
        for c in lineage:
            src = ", ".join(sorted(c.sources)) if c.sources else "<literal>"
            print(f" - {c.sink_column}: {src}")
            if c.transform_summary:
                print(f"   expr: {c.transform_summary}")
            infl = c.influences
            if any([infl.where_cols, infl.join_cols, infl.group_by_cols, infl.order_by_cols]):
                print("   influences:")
                if infl.where_cols:
                    print("     where:", ", ".join(r.fully_qualified() for r in infl.where_cols))
                if infl.join_cols:
                    print("     join:", ", ".join(r.fully_qualified() for r in infl.join_cols))
                if infl.group_by_cols:
                    print("     group_by:", ", ".join(r.fully_qualified() for r in infl.group_by_cols))
                if infl.order_by_cols:
                    print("     order_by:", ", ".join(r.fully_qualified() for r in infl.order_by_cols))


if __name__ == "__main__":
    main()
