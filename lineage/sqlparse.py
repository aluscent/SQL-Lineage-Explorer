from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .models import ColumnRef, Influences, ParsedQuery, Projection


def _try_import_sqlglot():
    try:
        import sqlglot  # type: ignore
        from sqlglot import exp
        return sqlglot, exp
    except ImportError:
        return None, None


def parse_simple_ident(token: str) -> Tuple[Optional[str], str]:
    token = token.strip().strip("`")
    if "." in token:
        q, c = token.split(".", 1)
        return q.strip().strip("`"), c.strip().strip("`")
    return None, token


def _split_csv(expr: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    current = []
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "(" or ch == "[":
            depth += 1
        elif ch == ")" or ch == "]":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    if current:
        parts.append("".join(current).strip())
    return parts


def _extract_between(sql: str, start_kw: str, end_kws: List[str]) -> Optional[str]:
    m = re.search(start_kw, sql, flags=re.IGNORECASE)
    if not m:
        return None
    start = m.end()
    end = len(sql)
    for kw in end_kws:
        n = re.search(kw, sql[start:], flags=re.IGNORECASE)
        if n:
            end = start + n.start()
            break
    return sql[start:end].strip()


def _find_column_refs(expr_sql: str, in_scope: List[str]) -> List[ColumnRef]:
    # Very naive: find tokens like a.b or bare identifiers that match scoped aliases
    refs: List[ColumnRef] = []
    # Remove string literals to avoid treating them as identifiers
    expr_no_str = re.sub(r"'(?:[^'\\]|\\.)*'", " ", expr_sql)
    expr_no_str = re.sub(r'"(?:[^"\\]|\\.)*"', " ", expr_no_str)
    # qualified refs
    for m in re.finditer(r"([a-zA-Z_][\w`]*)\s*\.\s*([a-zA-Z_][\w`]*)", expr_no_str):
        q = m.group(1).strip().strip("`")
        c = m.group(2).strip().strip("`")
        refs.append(ColumnRef(q, c))
    # bare refs (heuristic)
    tokens = re.findall(r"\b([a-zA-Z_][\w`]*)\b", expr_no_str)
    keywords = {"select", "from", "where", "join", "on", "and", "or", "as", "sum", "count", "over",
                "partition", "order", "by", "left", "right", "inner", "outer", "group", "having"}
    for t in tokens:
        tt = t.strip("`").lower()
        if tt in keywords:
            continue
        # Skip numeric-like tokens
        if tt.isdigit():
            continue
        # Already captured as qualified? skip
        if any((r.name == t or r.name == t.strip("`")) or (r.qualifier and r.qualifier == t.strip("`")) for r in refs):
            continue
        # If only one source in scope, attach to it
        if len(in_scope) == 1:
            refs.append(ColumnRef(in_scope[0], t.strip("`")))
        else:
            # leave qualifier None to mark potential ambiguity
            refs.append(ColumnRef(None, t.strip("`")))
    return refs


def parse_sql(sql: str, alias: str, in_scope_aliases: List[str]) -> ParsedQuery:
    # Scope-aware nested-query resolution is not implemented. Refuse to
    # manufacture lineage by mixing columns from different SELECT scopes.
    structural_sql = re.sub(r"'(?:''|[^'])*'|--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S)
    if re.search(r"\b(with|union|intersect|except)\b|\(\s*select\b", structural_sql, re.I):
        raise ValueError("Nested queries, CTEs and set operations are not supported; split them into named query stages")
    fallback_reason = "SQLGlot is unavailable; heuristic lineage may be incomplete"
    # Try sqlglot first
    sqlglot, exp = _try_import_sqlglot()
    if sqlglot:
        try:
            parsed = sqlglot.parse_one(sql, read="spark")
            if not isinstance(parsed, exp.Select):
                raise ValueError("Only SELECT queries are supported")
            source_map = {}
            for table in parsed.find_all(exp.Table):
                physical = ".".join(part.name for part in table.parts)
                source_map[table.alias_or_name] = physical
            sources = list(dict.fromkeys(source_map.values()))

            def column_ref(column):
                qualifier = column.table or None
                return ColumnRef(source_map.get(qualifier, qualifier), column.name)

            projections: List[Projection] = []
            select = parsed.find(exp.Select)
            if select:
                for e in select.expressions:
                    is_star = bool(e.is_star) if hasattr(e, "is_star") else False
                    alias_name = e.alias_or_name
                    expr_sql = e.sql(dialect="spark")
                    if is_star and expr_sql.endswith(".*"):
                        expr_sql = source_map.get(expr_sql[:-2], expr_sql[:-2]) + ".*"
                    inputs: List[ColumnRef] = []
                    is_aggr = e.find(exp.AggFunc) is not None
                    is_win = e.find(exp.Window) is not None
                    is_lit = isinstance(e.unalias(), (exp.Literal, exp.Null, exp.Boolean))
                    if not is_star:
                        # Collect referenced columns
                        for c in e.find_all(exp.Column):
                            q = c.table or None
                            n = c.name
                            inputs.append(column_ref(c))
                    projections.append(
                        Projection(
                            output_name=alias_name,
                            expr_sql=expr_sql,
                            inputs=inputs,
                            is_star=is_star,
                            is_aggregate=is_aggr,
                            is_window=is_win,
                            is_literal=is_lit,
                        )
                    )
            infl = Influences()
            where = parsed.args.get("where")
            if where:
                for c in where.find_all(exp.Column):
                    infl.where_cols.append(column_ref(c))
            for j in parsed.find_all(exp.Join):
                on = j.args.get("on")
                if on:
                    for c in on.find_all(exp.Column):
                        infl.join_cols.append(column_ref(c))
            gb = parsed.args.get("group")
            if gb:
                for c in gb.find_all(exp.Column):
                    infl.group_by_cols.append(column_ref(c))
            ob = parsed.args.get("order")
            if ob:
                for c in ob.find_all(exp.Column):
                    infl.order_by_cols.append(column_ref(c))
            return ParsedQuery(alias=alias, sources=sources, projections=projections,
                               influences=infl, parser_mode="sqlglot")
        except sqlglot.errors.ParseError as error:
            # Invalid SQL must not become plausible-looking heuristic lineage.
            raise ValueError("SQLGlot could not parse the SELECT query") from error

    # Minimal fallback parser
    if not re.match(r"^\s*select\b", sql, re.I):
        raise ValueError("Only SELECT queries are supported")
    # Normalize whitespace
    norm = re.sub(r"\s+", " ", sql.strip())
    sel = _extract_between(norm, r"\bselect\b", [r"\bfrom\b"])
    frm = _extract_between(norm, r"\bfrom\b", [r"\bwhere\b", r"\bgroup by\b", r"\border by\b", r"\bjoin\b", r"$"])
    where = _extract_between(norm, r"\bwhere\b", [r"\bgroup by\b", r"\border by\b", r"$"])
    # Collect joins crudely: look for occurrences of "join ... on ..."
    join_parts = re.findall(r"\bjoin\b\s+([^\s]+)\s+\bon\b\s+([^\b]+?)(?=\bjoin\b|\bwhere\b|\bgroup by\b|\border by\b|$)", norm, flags=re.IGNORECASE)

    sources: List[str] = []
    if frm:
        # FROM may contain multiple sources separated by commas or joins; we only pick first token as primary
        first = frm.split()[0]
        sources.append(first)
    for s, _ in join_parts:
        sources.append(s.split()[0])

    in_scope = [s for s in in_scope_aliases] if in_scope_aliases else sources

    projections: List[Projection] = []
    if sel:
        for item in _split_csv(sel):
            if item == "*" or item.endswith(".*"):
                projections.append(Projection(output_name=item, expr_sql=item, is_star=True))
                continue
            # aliasing: expr AS name or expr name
            m = re.match(r"(.+?)\s+as\s+([a-zA-Z_][\w`]*)$", item, flags=re.IGNORECASE)
            if not m:
                m = re.match(r"(.+?)\s+([a-zA-Z_][\w`]*)$", item)
            if m:
                expr_sql = m.group(1).strip()
                out = m.group(2).strip().strip("`")
            else:
                expr_sql = item
                # Derive output name for simple column refs
                q, n = parse_simple_ident(item)
                out = n
            inputs = _find_column_refs(expr_sql, in_scope)
            projections.append(
                Projection(
                    output_name=out,
                    expr_sql=expr_sql,
                    inputs=inputs,
                    is_star=False,
                    is_aggregate=bool(re.search(r"\b(sum|count|min|max|avg)\s*\(", expr_sql, flags=re.IGNORECASE)),
                )
            )
    infl = Influences()
    if where:
        infl.where_cols = _find_column_refs(where, in_scope)
    for _, on_expr in join_parts:
        infl.join_cols.extend(_find_column_refs(on_expr, in_scope))
    return ParsedQuery(alias=alias, sources=sources, projections=projections, influences=infl,
                       parser_mode="heuristic", warnings=[fallback_reason])
