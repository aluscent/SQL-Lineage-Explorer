# SQL Lineage Explorer

A lightweight toolkit and web UI to parse chained Spark SQL queries from a JSON file, build column-level lineage, and visualize the flow from sources to sink. It supports intermediate alias hops, star expansion using provided schemas, and captures non-projection influences (filters, joins, group-by, order-by).

- Core lineage engine: static SQL parsing with optional `sqlglot` for higher fidelity.
- CLI: emits sink column mappings and influences as text or JSON.
- Web UI (Flask): interactive graph, node inspector, ambiguity resolution, and a lineage matrix.

## Features

- Parse ordered queries with `alias`, `query`, and `type: "sink"`.
- Infer schemas from base selects; expand `*` and `alias.*`.
- Column-level lineage for sink columns with intermediate hops and expressions.
- Capture non-projection influences across the path.
- Flag ambiguous references with structured prompts and allow resolutions.
- Web graph: layered DAG, zoom/pan, node SQL inspector, column path highlight, minimap.
- Bottom drawer tabs: Lineage Matrix (basic), placeholders for Preview/Profiles/Logs.
- Export current graph as PNG from the UI.

## Repository Structure

- `lineage/` – Python lineage core
  - `jsonio.py` – load queries JSON, find sink
  - `sqlparse.py` – minimal parser with optional `sqlglot`
  - `models.py` – data models for lineage and context
  - `build.py` – build schemas, alias DAG, and lineage with hops
  - `cli.py` – command-line interface
- `webapp/` – Flask app
  - `app.py` – server and `/analyze` handler
  - `templates/index.html` – UI, graph, inspector, matrix
  - `static/style.css` – minimal CSS
- `scripts/debug_lineage.py` – helper script to inspect schemas and mappings
- `queries.json` – example input file

## Requirements

- Python 3.8+
- Flask (for the web UI)
- Optional: `sqlglot` (improves SQL parsing accuracy for Spark dialect)

Install dependencies:

```
pip install -r requirements.txt
```

Without the web UI (CLI only), you can skip Flask and install just `sqlglot` (optional).

## Input Format

`queries.json` should contain an ordered array under `queries`. The sink is identified by `type: "sink"` (or defaults to the last query):

```
{
  "queries": [
    { "alias": "accounts", "query": "select id, name from db.accounts" },
    { "alias": "joined",   "query": "select * from accounts ... join ..." },
    { "alias": "final",     "type": "sink", "query": "select ... from joined" }
  ]
}
```

Schemas are inferred from the earliest selects referencing physical tables (e.g., `db.table`). Star expansions rely on these inferred schemas.

## CLI Usage

- JSON output:
```
python -m lineage.cli queries.json --json
```
- Text output:
```
python -m lineage.cli queries.json
```
- Provide disambiguation resolutions (key→qualifier):
```
python -m lineage.cli queries.json --json --resolve resolutions.json
```
The CLI prints any outstanding ambiguities with a `key`—save choices in a small JSON and re-run.

## Web UI

Run the Flask app:
```
python webapp/app.py
```
Open `http://127.0.0.1:5000` and:
- Choose your `queries.json` and click Analyze.
- Click nodes to see SQL; click sink column buttons to highlight their path.
- Resolve ambiguities in the sidebar and click Re-Analyze.
- Export graph as PNG from the header.

### UI Layout
- Header: file selector, search, analyze, export.
- Left Sidebar: sink info, view/filters, nodes list, warnings.
- Graph Canvas: layered DAG, minimap, breadcrumbs, zoom/pan.
- Inspector (right): node summary, SQL, schema, derivations.
- Bottom Drawer Tabs: Lineage Matrix (+ preview/profiles/logs placeholders).

## Ambiguity Resolution
When a column ref is unqualified in a multi-source scope, the engine records a structured ambiguity with:
- `key`, `alias`, `kind` (projection/where/join/group), `ref_name`, and `options` (candidate qualifiers).

In the web UI, pick a qualifier for each ambiguity and click Re-Analyze; in the CLI, supply a `--resolve` mapping.

## Limitations
- Minimal parser handles common patterns; `sqlglot` greatly improves support for complex Spark SQL.
- Edge metrics (row counts, quality) and data preview/profiles are placeholders (would require runtime access and instrumentation).
- Duplicate column names under `SELECT *` across joins are flagged; disambiguation is advised.

## Roadmap Ideas
- Edge tooltips with stats and selected columns.
- Multi-path compare and AST view for expressions.
- Dark mode and accessibility palette.
- Export lineage JSON/CSV from the web UI.
- Print-friendly audit report.

## Development
Run quick debug of core mappings:
```
python scripts/debug_lineage.py
```
Formatting and linting are not enforced; keep changes minimal and focused.

## License

MIT License – see `LICENSE`.

Copyright (c) 2025 Pythonomics Contributors

