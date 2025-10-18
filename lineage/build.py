from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .models import (
    AliasSchema,
    BuildContext,
    ColumnLineage,
    ColumnHop,
    ColumnRef,
    Ambiguity,
    ParsedQuery,
    QuerySpec,
)
from .sqlparse import parse_sql


def _is_physical_table(ref: str) -> bool:
    # crude heuristic: contains a dot catalog.table and not yet defined as alias
    return "." in ref


def build_parsed_queries(specs: List[QuerySpec]) -> Dict[str, ParsedQuery]:
    alias_to_query: Dict[str, ParsedQuery] = {}
    for spec in specs:
        parsed = parse_sql(spec.sql, spec.alias, [])
        alias_to_query[spec.alias] = parsed
    return alias_to_query


def _resolve_schema_for_base(parsed: ParsedQuery) -> Optional[AliasSchema]:
    # Derive schema from explicit select list when FROM points to a physical table
    cols: List[str] = []
    for p in parsed.projections:
        if p.is_star:
            return None
        cols.append(p.output_name)
    return AliasSchema(columns=cols)


def build_schemas(context: BuildContext) -> None:
    # Pass 1: establish schemas for aliases without stars
    for alias, pq in context.alias_to_query.items():
        if pq.sources and _is_physical_table(pq.sources[0]):
            schema = _resolve_schema_for_base(pq)
            if schema:
                context.alias_to_schema[alias] = schema

    # Pass 2: resolve schemas for derived aliases, handling star expansion when possible
    progressed = True
    while progressed:
        progressed = False
        for alias, pq in context.alias_to_query.items():
            if alias in context.alias_to_schema:
                continue
            # Ensure all source schemas known
            sources_ready = True
            for s in pq.sources:
                if s in context.alias_to_schema:
                    continue
                if _is_physical_table(s):
                    # No schema for raw physical table unless an alias defines it earlier
                    sources_ready = False
                else:
                    sources_ready = False
            if not sources_ready:
                continue

            # If projections have star(s), expand using source schemas
            cols: List[str] = []
            has_star = any(p.is_star for p in pq.projections)
            if has_star:
                for p in pq.projections:
                    if p.is_star:
                        token = p.expr_sql.strip()
                        if token == "*":
                            # concatenate all source columns in order
                            for s in pq.sources:
                                if s in context.alias_to_schema:
                                    cols.extend(context.alias_to_schema[s].columns)
                                else:
                                    # physical table without schema known -> cannot expand
                                    cols = []
                                    break
                        elif token.endswith(".*"):
                            q = token[:-2]
                            if q in context.alias_to_schema:
                                cols.extend(context.alias_to_schema[q].columns)
                            else:
                                cols = []
                                break
                    else:
                        cols.append(p.output_name)
            else:
                cols = [p.output_name for p in pq.projections]

            if cols:
                context.alias_to_schema[alias] = AliasSchema(columns=cols)
                progressed = True


def _amb_key(alias: str, kind: str, ref_name: str, output_col: Optional[str]) -> str:
    return f"{alias}|{kind}|{output_col or ''}|{ref_name}"


def _qualify_or_flag(
    context: BuildContext,
    ref: ColumnRef,
    scope: List[str],
    alias: str,
    kind: str,
    output_col: Optional[str] = None,
) -> Optional[ColumnRef]:
    if ref.qualifier:
        return ref
    if len(scope) == 1:
        return ColumnRef(scope[0], ref.name, ref.path)
    # Try resolution mapping
    k = _amb_key(alias, kind, ref.name, output_col)
    choice = context.disambiguation.get(k)
    if choice and choice in scope:
        return ColumnRef(choice, ref.name, ref.path)
    # Record ambiguity if not already present
    if not any(a.key == k for a in context.ambiguities):
        context.ambiguities.append(
            Ambiguity(
                key=k,
                kind=kind,
                alias=alias,
                ref_name=ref.name,
                output_col=output_col,
                options=list(scope),
                message=f"Ambiguous column '{ref.name}' in {kind} of alias '{alias}' with options {scope}",
            )
        )
    return None


def build_lineage(context: BuildContext) -> None:
    # For each alias, map its output columns to base sources
    for alias, pq in context.alias_to_query.items():
        scope = list(pq.sources)
        # Initialize schema present (from previous pass)
        schema = context.alias_to_schema.get(alias)
        out_columns: List[str]
        if schema:
            out_columns = schema.columns
        else:
            out_columns = [p.output_name for p in pq.projections if not p.is_star]

        # Handle star pass-through mappings when schemas are available
        if any(p.is_star for p in pq.projections) and schema:
            # Track duplicates to flag ambiguities
            seen_names: Set[str] = set()
            for p in pq.projections:
                if not p.is_star:
                    continue
                token = p.expr_sql.strip()
                if token == "*":
                    for s in pq.sources:
                        sch = context.alias_to_schema.get(s)
                        if not sch:
                            continue
                        for col in sch.columns:
                            key = (alias, col)
                            # Resolve to upstream alias col sources if available
                            upstream_key = (s, col)
                            srcs = context.alias_col_sources.get(upstream_key)
                            if srcs:
                                context.alias_col_sources[key] = set(srcs)
                            else:
                                context.alias_col_sources[key] = {f"{s}.{col}"}
                            context.alias_col_expr[key] = f"{s}.{col}"
                            # Record direct parent hop
                            context.alias_col_parents.setdefault(key, []).append(ColumnRef(s, col))
                            if col in seen_names:
                                # Duplicate columns from star expansion; flag as informational ambiguity
                                k = _amb_key(alias, "star", col, None)
                                if not any(a.key == k for a in context.ambiguities):
                                    context.ambiguities.append(
                                        Ambiguity(
                                            key=k,
                                            kind="star",
                                            alias=alias,
                                            ref_name=col,
                                            options=list(pq.sources),
                                            message=f"Duplicate column name '{col}' from '*' expansion in alias '{alias}'",
                                        )
                                    )
                            else:
                                seen_names.add(col)
                elif token.endswith(".*"):
                    s = token[:-2]
                    sch = context.alias_to_schema.get(s)
                    if not sch:
                        continue
                    for col in sch.columns:
                        key = (alias, col)
                        upstream_key = (s, col)
                        srcs = context.alias_col_sources.get(upstream_key)
                        if srcs:
                            context.alias_col_sources[key] = set(srcs)
                        else:
                            context.alias_col_sources[key] = {f"{s}.{col}"}
                        context.alias_col_expr[key] = f"{s}.{col}"
                        context.alias_col_parents.setdefault(key, []).append(ColumnRef(s, col))
                        if col in seen_names:
                            k = _amb_key(alias, "star", col, None)
                            if not any(a.key == k for a in context.ambiguities):
                                context.ambiguities.append(
                                    Ambiguity(
                                        key=k,
                                        kind="star",
                                        alias=alias,
                                        ref_name=col,
                                        options=[s],
                                        message=f"Duplicate column name '{col}' from '{token}' in alias '{alias}'",
                                    )
                                )
                        else:
                            seen_names.add(col)

        for p in pq.projections:
            if p.is_star:
                # Star expansion is handled at schema level; lineage per column is complex without names.
                continue
            key = (alias, p.output_name)
            sources: Set[str] = set()
            for inp in p.inputs:
                qref = _qualify_or_flag(context, inp, scope, alias, "projection", p.output_name)
                if not qref:
                    continue
                # If input refers to another alias column, expand to its sources
                if qref.qualifier in context.alias_to_schema and qref.qualifier in context.alias_to_query:
                    prev_key = (qref.qualifier, qref.name)
                    context.alias_col_parents.setdefault(key, []).append(qref)
                    if prev_key in context.alias_col_sources:
                        sources |= context.alias_col_sources[prev_key]
                    else:
                        # Assume direct pass-through if no prior mapping exists
                        sources.add(f"{qref.qualifier}.{qref.name}")
                else:
                    # Treat as base table column
                    sources.add(qref.fully_qualified())
            if not sources and p.is_literal:
                # represent literal as no sources
                sources = set()
            context.alias_col_sources[key] = sources
            context.alias_col_expr[key] = p.expr_sql


def lineage_for_sink(context: BuildContext, sink_alias: str) -> List[ColumnLineage]:
    result: List[ColumnLineage] = []
    schema = context.alias_to_schema.get(sink_alias)
    if not schema:
        # Create a best-effort list from projections
        pq = context.alias_to_query[sink_alias]
        cols = [p.output_name for p in pq.projections if not p.is_star]
    else:
        cols = list(schema.columns)

    def walk_hops(alias: str, col: str, acc: List[ColumnHop], visited: Set[Tuple[str, str]]):
        key = (alias, col)
        if key in visited:
            return
        visited.add(key)
        parents = context.alias_col_parents.get(key, [])
        for p in parents:
            acc.append(ColumnHop(alias=p.qualifier or "", column=p.name, expr=context.alias_col_expr.get((p.qualifier or "", p.name))))
            # Recurse only if parent is an alias we know (not a physical table ref)
            if p.qualifier and p.qualifier in context.alias_to_query:
                walk_hops(p.qualifier, p.name, acc, visited)

    for col in cols:
        key = (sink_alias, col)
        hops = [ColumnHop(alias=sink_alias, column=col, expr=context.alias_col_expr.get(key))]
        walk_hops(sink_alias, col, hops, set())
        sources = set(context.alias_col_sources.get(key, set()))
        # Merge influences from sink and intermediate aliases traversed in hops
        merged_where: List[ColumnRef] = list(context.alias_to_query[sink_alias].influences.where_cols)
        merged_join: List[ColumnRef] = list(context.alias_to_query[sink_alias].influences.join_cols)
        merged_gb: List[ColumnRef] = list(context.alias_to_query[sink_alias].influences.group_by_cols)
        merged_ob: List[ColumnRef] = list(context.alias_to_query[sink_alias].influences.order_by_cols)
        for h in hops[1:]:  # skip the sink itself
            alias_influences = context.alias_to_query.get(h.alias)
            if not alias_influences:
                continue
            infl = alias_influences.influences
            merged_where.extend(infl.where_cols)
            merged_join.extend(infl.join_cols)
            merged_gb.extend(infl.group_by_cols)
            merged_ob.extend(infl.order_by_cols)
        # Qualify ambiguous influence refs using disambiguation, recording ambiguities if needed
        scope_map: Dict[str, List[str]] = {}
        for a in set([sink_alias] + [h.alias for h in hops[1:]]):
            pq = context.alias_to_query.get(a)
            if pq:
                scope_map[a] = list(pq.sources)

        def qualify_list(lst: List[ColumnRef], a: str, kind: str) -> List[ColumnRef]:
            out: List[ColumnRef] = []
            scope = scope_map.get(a, [])
            for ref in lst:
                qref = _qualify_or_flag(context, ref, scope, a, kind, None)
                if qref:
                    out.append(qref)
            return out

        qualified_where: List[ColumnRef] = []
        qualified_join: List[ColumnRef] = []
        qualified_gb: List[ColumnRef] = []
        qualified_ob: List[ColumnRef] = []
        # Qualify per alias where they originated
        qualified_where.extend(qualify_list(context.alias_to_query[sink_alias].influences.where_cols, sink_alias, "where"))
        qualified_join.extend(qualify_list(context.alias_to_query[sink_alias].influences.join_cols, sink_alias, "join"))
        qualified_gb.extend(qualify_list(context.alias_to_query[sink_alias].influences.group_by_cols, sink_alias, "group_by"))
        qualified_ob.extend(qualify_list(context.alias_to_query[sink_alias].influences.order_by_cols, sink_alias, "order_by"))
        for h in hops[1:]:
            pq = context.alias_to_query.get(h.alias)
            if not pq:
                continue
            qualified_where.extend(qualify_list(pq.influences.where_cols, h.alias, "where"))
            qualified_join.extend(qualify_list(pq.influences.join_cols, h.alias, "join"))
            qualified_gb.extend(qualify_list(pq.influences.group_by_cols, h.alias, "group_by"))
            qualified_ob.extend(qualify_list(pq.influences.order_by_cols, h.alias, "order_by"))

        infl = context.alias_to_query[sink_alias].influences.__class__(
            where_cols=qualified_where,
            join_cols=qualified_join,
            group_by_cols=qualified_gb,
            order_by_cols=qualified_ob,
        )
        result.append(
            ColumnLineage(
                sink_alias=sink_alias,
                sink_column=col,
                hops=hops,
                sources=sources,
                transform_summary=context.alias_col_expr.get(key),
                influences=infl,
            )
        )
    return result
