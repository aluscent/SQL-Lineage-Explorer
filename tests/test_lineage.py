import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from lineage.build import build_lineage, build_parsed_queries, build_schemas, lineage_for_sink
from lineage.models import BuildContext, QuerySpec
from lineage.sqlparse import parse_sql
from webapp.app import app


class LineageRegressionTests(unittest.TestCase):
    def test_physical_table_aliases_preserve_column_identity(self):
        parsed = parse_sql("SELECT p.amount * p.rate AS euros FROM bank.payments p", "a", [])
        self.assertEqual(parsed.sources, ["bank.payments"])
        self.assertEqual({c.fully_qualified() for c in parsed.projections[0].inputs},
                         {"bank.payments.amount", "bank.payments.rate"})
        self.assertEqual(parsed.parser_mode, "sqlglot")

    def test_multistage_star_and_transformation_golden(self):
        ctx = BuildContext(alias_to_query=build_parsed_queries([
            QuerySpec("raw", "SELECT amount, rate FROM bank.payments"),
            QuerySpec("copy", "SELECT * FROM raw"),
            QuerySpec("report", "SELECT amount * rate AS euros FROM copy"),
        ]))
        build_schemas(ctx)
        build_lineage(ctx)
        result = lineage_for_sink(ctx, "report")
        self.assertEqual(result[0].sources, {"bank.payments.amount", "bank.payments.rate"})

    def test_input_order_does_not_change_transitive_sources(self):
        specs = [QuerySpec("out", "SELECT amount FROM staged"),
                 QuerySpec("staged", "SELECT amount FROM bank.payments")]
        ctx = BuildContext(alias_to_query=build_parsed_queries(specs))
        build_schemas(ctx)
        build_lineage(ctx)
        self.assertEqual(ctx.alias_col_sources[("out", "amount")], {"bank.payments.amount"})

    def test_cycles_and_duplicate_aliases_are_rejected(self):
        for specs in [[QuerySpec("a", "SELECT id FROM b"), QuerySpec("b", "SELECT id FROM a")],
                      [QuerySpec("a", "SELECT 1"), QuerySpec("a", "SELECT 2")]]:
            with self.assertRaises(ValueError):
                build_parsed_queries(specs)

    def test_sql_keywords_in_literals_are_not_nested_queries(self):
        p = parse_sql("SELECT 'with union' AS label FROM bank.payments", "a", [])
        self.assertTrue(p.projections[0].is_literal)

    def test_unqualified_join_remains_ambiguous(self):
        ctx = BuildContext(alias_to_query=build_parsed_queries([
            QuerySpec("out", "SELECT id FROM bank.lefts l JOIN bank.rights r ON l.id = r.id")]))
        build_schemas(ctx)
        build_lineage(ctx)
        self.assertTrue(ctx.ambiguities)
        self.assertEqual(ctx.alias_col_sources[("out", "id")], set())

    def test_aggregate_window_literal_and_influences(self):
        p = parse_sql("SELECT SUM(p.amount) OVER (PARTITION BY p.country) AS total, 1 AS constant FROM bank.payments p WHERE p.active = 1", "a", [])
        self.assertTrue(p.projections[0].is_aggregate)
        self.assertTrue(p.projections[0].is_window)
        self.assertTrue(p.projections[1].is_literal)
        self.assertEqual(p.projections[1].inputs, [])
        self.assertEqual(p.influences.where_cols[0].fully_qualified(), "bank.payments.active")

    def test_unsupported_scopes_and_invalid_sql_fail(self):
        for sql in ["WITH a AS (SELECT id FROM x) SELECT id FROM a",
                    "SELECT id FROM (SELECT id FROM x) a", "SELECT id FROM x UNION SELECT id FROM y",
                    "DELETE FROM x", "SELECT FROM"]:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                parse_sql(sql, "a", [])

    def test_fallback_is_explicit_and_still_handles_basic_columns(self):
        with patch("lineage.sqlparse._try_import_sqlglot", return_value=(None, None)):
            p = parse_sql("SELECT amount FROM bank.payments", "a", [])
        self.assertEqual(p.parser_mode, "heuristic")
        self.assertTrue(p.warnings)
        self.assertEqual(p.projections[0].inputs[0].fully_qualified(), "bank.payments.amount")

    def test_cli_json_is_machine_readable(self):
        result = subprocess.run([sys.executable, "-m", "lineage.cli", "queries.json", "--json"], capture_output=True, text=True, check=True)
        self.assertIn("diagnostics", json.loads(result.stdout))

    def test_web_validation_and_size_limits(self):
        client = app.test_client()
        for payload in [{"queries": []}, {"queries": [None]},
                        {"queries": [{"alias": "x", "query": "SELECT 1"}] * 2}]:
            self.assertEqual(client.post("/analyze", data={"json_text": json.dumps(payload)}).status_code, 400)
        self.assertEqual(client.post("/analyze", data={"json_text": "x" * (1024 * 1024)}).status_code, 413)
        self.assertEqual(client.post("/analyze", data={"json_text": json.dumps({"queries": [{"alias": "x", "query": "SELECT id FROM t"}]}), "sink": "missing"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
