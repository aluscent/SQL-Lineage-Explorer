"""Lightweight lineage extraction toolkit for chained Spark SQL queries.

This package parses a JSON file of ordered queries with aliases, builds a
column-level lineage graph, and can output mappings from sink columns back to
their source table columns, including intermediate hops and non-projection
influences (filters, joins, group by).

Design notes:
- Tries to use `sqlglot` if available for more accurate parsing.
- Falls back to a minimal parser for simple SELECT/FROM/WHERE patterns.
"""

__all__ = [
    "jsonio",
    "models",
    "sqlparse",
    "build",
]

