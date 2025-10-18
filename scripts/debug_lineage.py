import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from lineage.jsonio import load_queries
from lineage.build import build_parsed_queries, build_schemas, build_lineage
from lineage.models import BuildContext


def main():
    specs = load_queries('queries.json')
    ctx = BuildContext()
    ctx.alias_to_query = build_parsed_queries(specs)
    build_schemas(ctx)
    build_lineage(ctx)
    print('Schemas:')
    for a, s in ctx.alias_to_schema.items():
        print(' ', a, s.columns)
    print('\nMappings:')
    for k in sorted(ctx.alias_col_sources.keys()):
        print(' ', k, '->', sorted(ctx.alias_col_sources[k]))
    print('\nAmbiguities:', ctx.ambiguities)


if __name__ == '__main__':
    main()
