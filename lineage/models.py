from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class QuerySpec:
    alias: str
    sql: str
    type: Optional[str] = None  # e.g., "sink"


@dataclass
class ColumnRef:
    qualifier: Optional[str]  # table or alias
    name: str                  # column name (or path root)
    path: Optional[Tuple[str, ...]] = None  # nested path beyond name

    def fully_qualified(self) -> str:
        base = f"{self.qualifier + '.' if self.qualifier else ''}{self.name}"
        if self.path:
            return base + "." + ".".join(self.path)
        return base


@dataclass
class Projection:
    # A single output column of a SELECT
    output_name: str
    expr_sql: str
    inputs: List[ColumnRef] = field(default_factory=list)
    is_star: bool = False
    is_aggregate: bool = False
    is_window: bool = False
    is_literal: bool = False


@dataclass
class Influences:
    # Non-projection influences at query level
    where_cols: List[ColumnRef] = field(default_factory=list)
    join_cols: List[ColumnRef] = field(default_factory=list)
    group_by_cols: List[ColumnRef] = field(default_factory=list)
    order_by_cols: List[ColumnRef] = field(default_factory=list)


@dataclass
class ParsedQuery:
    alias: str
    sources: List[str]  # list of source aliases or physical tables
    projections: List[Projection]
    influences: Influences = field(default_factory=Influences)
    parser_mode: str = "unknown"
    warnings: List[str] = field(default_factory=list)


@dataclass
class AliasSchema:
    # Column names in order and optional types (unknown if None)
    columns: List[str]
    types: Dict[str, Optional[str]] = field(default_factory=dict)


@dataclass
class ColumnHop:
    alias: str
    column: str
    expr: Optional[str] = None
    note: Optional[str] = None


@dataclass
class ColumnLineage:
    sink_alias: str
    sink_column: str
    hops: List[ColumnHop]  # from sink towards sources (ordered)
    sources: Set[str]      # fully qualified base table columns
    transform_summary: Optional[str] = None
    influences: Influences = field(default_factory=Influences)


@dataclass
class BuildContext:
    # Registry across processing
    alias_to_query: Dict[str, ParsedQuery] = field(default_factory=dict)
    alias_to_schema: Dict[str, AliasSchema] = field(default_factory=dict)
    # For each alias.col, the set of base sources it depends on
    alias_col_sources: Dict[Tuple[str, str], Set[str]] = field(default_factory=dict)
    # For each alias.col, remember projection expression
    alias_col_expr: Dict[Tuple[str, str], str] = field(default_factory=dict)
    # Direct parents for each alias.col (alias-qualified ColumnRefs)
    alias_col_parents: Dict[Tuple[str, str], List[ColumnRef]] = field(default_factory=dict)
    # Ambiguities found that require disambiguation
    ambiguities: List["Ambiguity"] = field(default_factory=list)
    # Disambiguation decisions keyed by unique key
    disambiguation: Dict[str, str] = field(default_factory=dict)


@dataclass
class Ambiguity:
    # A structured record for an ambiguous reference
    key: str                 # unique key for resolution
    kind: str                # 'projection' | 'where' | 'join' | 'group_by' | 'order_by'
    alias: str               # alias where ambiguity occurs
    ref_name: str            # unqualified column name
    options: List[str]       # candidate qualifiers (scope sources)
    output_col: Optional[str] = None  # for projection context
    message: Optional[str] = None
