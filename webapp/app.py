from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Tuple

import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from flask import Flask, jsonify, render_template, request

from lineage.build import build_lineage, build_parsed_queries, build_schemas, lineage_for_sink
from lineage.jsonio import load_queries, find_sink_alias
from lineage.models import BuildContext


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024


@app.errorhandler(ValueError)
def invalid_input(error):
    return jsonify(error=str(error)), 400


@app.errorhandler(413)
def input_too_large(error):
    return jsonify(error="Queries payload exceeds 1 MiB"), 413



def _load_specs_from_req() -> Tuple[List[dict], str]:
    data_text = None
    if "json_text" in request.form and request.form["json_text"]:
        data_text = request.form["json_text"]
    elif "file" in request.files:
        f = request.files["file"]
        try:
            data_text = f.read().decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Queries file must use UTF-8") from error
    if not data_text:
        raise ValueError("No queries JSON provided")
    data = json.loads(data_text)
    if not isinstance(data, dict) or "queries" not in data:
        raise ValueError("Invalid JSON: expected object with 'queries' array")
    queries = data["queries"]
    if not isinstance(queries, list) or not 1 <= len(queries) <= 100:
        raise ValueError("Provide between 1 and 100 queries")
    aliases = set()
    for query in queries:
        if not isinstance(query, dict):
            raise ValueError("Each query must be an object")
        alias, sql = query.get("alias"), query.get("query")
        if not isinstance(alias, str) or not alias.strip() or alias in aliases:
            raise ValueError("Query aliases must be unique non-empty strings")
        if not isinstance(sql, str) or not sql.strip() or len(sql) > 50000:
            raise ValueError("Each query needs non-empty SQL of at most 50000 characters")
        aliases.add(alias)
    return queries, data_text


def _alias_edges(alias_to_query: Dict[str, Any]) -> List[Dict[str, str]]:
    edges: List[Dict[str, str]] = []
    aliases = set(alias_to_query.keys())
    for alias, pq in alias_to_query.items():
        for s in pq.sources:
            if s in aliases:
                edges.append({"from": s, "to": alias})
    return edges


def _classify_node(pq: Any) -> str:
    # Heuristic classification: read/join/filter/agg/project/custom
    has_join = bool(pq.influences.join_cols)
    has_where = bool(pq.influences.where_cols)
    has_group = bool(pq.influences.group_by_cols)
    has_agg = any(getattr(p, "is_aggregate", False) for p in pq.projections)
    # If sources are only physical (contain '.') and no preceding aliases, call it 'read'
    is_read = all('.' in s for s in pq.sources) and not has_join and not has_where and not has_agg and not has_group
    if is_read:
        return "read"
    if has_join:
        return "join"
    if has_group or has_agg:
        return "agg"
    if has_where:
        return "filter"
    if pq.projections:
        return "project"
    return "custom"


def _schemas_map(ctx: BuildContext) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for a, schema in ctx.alias_to_schema.items():
        out[a] = list(schema.columns)
    return out


def _derivations_map(ctx: BuildContext) -> Dict[str, Dict[str, Dict[str, Any]]]:
    # alias -> { output_col: { expr, inputs:[qual.col] } }
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for alias, pq in ctx.alias_to_query.items():
        m: Dict[str, Dict[str, Any]] = {}
        for p in pq.projections:
            if p.is_star:
                continue
            inputs = []
            for r in p.inputs:
                q = (r.qualifier + ".") if r.qualifier else ""
                inputs.append(f"{q}{r.name}")
            m[p.output_name] = {"expr": p.expr_sql, "inputs": inputs}
        out[alias] = m
    return out


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    queries_list, _ = _load_specs_from_req()
    # Build QuerySpec objects like jsonio.load_queries does
    specs = []
    for obj in queries_list:
        specs.append({"alias": obj.get("alias"), "query": obj["query"], "type": obj.get("type")})

    # Optional: sink override
    sink_override = request.form.get("sink")
    resolutions_json = request.form.get("resolutions")
    resolutions: Dict[str, str] = json.loads(resolutions_json) if resolutions_json else {}

    if not isinstance(resolutions, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in resolutions.items()
    ):
        raise ValueError("Resolutions must map strings to strings")

    # Use core lineage
    from lineage.models import QuerySpec as QS

    specs_objs = [QS(alias=s["alias"], sql=s["query"], type=s.get("type")) for s in specs]
    alias_sql_map = {s.alias: s.sql for s in specs_objs}
    sink = sink_override or find_sink_alias(specs_objs)
    if sink not in alias_sql_map:
        raise ValueError("Selected sink is not a query alias")
    ctx = BuildContext()
    ctx.disambiguation = resolutions
    ctx.alias_to_query = build_parsed_queries(specs_objs)
    build_schemas(ctx)
    build_lineage(ctx)
    cols = lineage_for_sink(ctx, sink)

    payload = {
        "sink": sink,
        "diagnostics": {a: {"parser": p.parser_mode, "warnings": p.warnings}
                        for a, p in ctx.alias_to_query.items()},
        "aliases": list(ctx.alias_to_query.keys()),
        "edges": _alias_edges(ctx.alias_to_query),
        "queries_map": alias_sql_map,
        "node_types": {a: _classify_node(pq) for a, pq in ctx.alias_to_query.items()},
        "schemas": _schemas_map(ctx),
        "derivations": _derivations_map(ctx),
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
            for c in cols
        ],
        "ambiguities": [
            {
                "key": a.key,
                "kind": a.kind,
                "alias": a.alias,
                "output_col": a.output_col,
                "ref_name": a.ref_name,
                "options": a.options,
                "message": a.message,
            }
            for a in ctx.ambiguities
        ],
    }
    return jsonify(payload)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
