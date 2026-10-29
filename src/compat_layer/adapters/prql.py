"""PRQL — SQL compatibility adapter.

The user-facing artifact is PRQL and the agent-facing view is SQL.
Projection compiles PRQL to SQL with prqlc; delivery parses the edited SQL
with sqlglot, constructs PRQL, and validates a compiled SQL round trip.
The structural check is intentionally scoped: unsupported query shapes are
refused rather than returned as an unchecked PRQL artifact.
"""

from __future__ import annotations

import re
from pathlib import Path
import prqlc
import sqlglot
from sqlglot import exp

from ..interface import ToolAdapter, TranslationResult


# ---------------------------------------------------------------------------
# PRQL Adapter
# ---------------------------------------------------------------------------


class PRQLAdapter(ToolAdapter):
    """PRQL ↔ SQL adapter.

    PRQL compiles to SQL deterministically, so new_to_old (PRQL→SQL) is
    executable SQL. old_to_new (SQL→PRQL) uses sqlglot for parsing and
    rule-based translation, with conservative structural round-trip checks.
    """

    tool_name = "PRQL"
    old_tool_name = "SQL"
    old_extension = ".sql"
    new_extension = ".prql"

    # ------------------------------------------------------------------
    # new_to_old: PRQL → SQL (deterministic)
    # ------------------------------------------------------------------

    def new_to_old(self, content: str, filepath: str = "") -> TranslationResult:
        """Translate PRQL to SQL using prqlc.

        This is the "easy direction" — PRQL was designed to compile to SQL.
        """
        try:
            # prqlc.compile can take either PRQL source or options
            sql = prqlc.compile(content)

            # Strip the PRQL compiler version comment
            sql = self._strip_prql_comment(sql)

            return TranslationResult.ok(
                sql.strip() + ";",
                method="prqlc", version=prqlc.__version__,
            )
        except Exception as e:
            error_msg = str(e)
            # Try to extract a useful error message from prqlc
            return TranslationResult.fail(
                [f"PRQL compilation failed: {error_msg}"],
                warnings=[error_msg],
            )

    # ------------------------------------------------------------------
    # old_to_new: SQL → PRQL (checked rule-based subset)
    # ------------------------------------------------------------------

    def old_to_new(self, content: str, filepath: str = "") -> TranslationResult:
        """Translate SQL to PRQL.

        Strategy:
        1. Parse SQL with sqlglot into AST
        2. Generate PRQL from the supported AST subset
        3. Compile generated PRQL back to SQL and compare supported AST shapes
        4. Refuse shapes outside the conservative equivalence rules
        """
        sql = content.strip().rstrip(";")

        # Try rule-based translation first
        result = self._sqlglot_to_prql(sql)
        if result.success:
            # Validate by round-tripping
            roundtrip = self._validate_roundtrip(result.content, sql)
            if roundtrip.success:
                result.metadata["method"] = "sqlglot"
                result.metadata["validated"] = True
                result.metadata["validation"] = roundtrip.metadata.get("validation", "scoped_sql_ast")
                result.warnings.extend(roundtrip.warnings)
                return result
            return TranslationResult.fail(roundtrip.errors, warnings=roundtrip.warnings)

        # Unsupported SQL must fail explicitly, without an unchecked LLM fallback.
        return result

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_new(self, content: str) -> bool:
        """Validate PRQL: try compiling to SQL."""
        try:
            prqlc.compile(content)
            return True
        except Exception:
            return False

    def validate_old(self, content: str) -> bool:
        """Validate SQL: try parsing with sqlglot."""
        try:
            for statement in sqlglot.parse(content, dialect="sqlite"):
                pass  # Just checking parseability
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # SQL → PRQL: Rule-based translation via sqlglot AST
    # ------------------------------------------------------------------

    def _sqlglot_to_prql(self, sql: str) -> TranslationResult:
        """Convert SQL to PRQL using sqlglot AST walking."""
        self._alias_map = {}  # Reset alias map for each translation
        try:
            parsed = sqlglot.parse(sql, dialect="sqlite")
            if not parsed:
                return TranslationResult.fail(["sqlglot returned empty AST"])
            if len(parsed) != 1 or not isinstance(parsed[0], (exp.Select, exp.Union)):
                raise ValueError("Only a single SELECT or UNION is supported")
            if next(parsed[0].find_all(exp.Cast), None) is not None:
                raise ValueError("CAST semantics are unsupported; refusing to discard the cast")

            results = []
            for statement in parsed:
                if statement is None:
                    continue
                prql = self._translate_statement(statement)
                results.append(prql)

            output = "\n\n".join(results)
            if isinstance(parsed[0], exp.Union) or next(parsed[0].find_all(exp.Union), None) is not None:
                # This adapter parses and checks SQLite SQL. Generic PRQL emits
                # `UNION DISTINCT`, which SQLite rejects; target SQLite explicitly.
                output = "prql target:sql.sqlite\n" + output
            return TranslationResult.ok(output)
        except Exception as e:
            return TranslationResult.fail([f"sqlglot translation failed: {e}"])

    def _translate_statement(self, stmt: exp.Expression) -> str:
        """Translate a single SQL statement to PRQL."""
        # Handle CTE (WITH clause) — sqlglot key is 'with_' not 'with'
        ctes = stmt.args.get("with_") or stmt.args.get("with")
        cte_parts: list[str] = []
        if ctes:
            for cte in ctes.expressions if hasattr(ctes, 'expressions') else []:
                alias = cte.alias
                inner = self._translate_statement(cte.this)
                cte_parts.append(f"let {alias} = (\n{self._indent(inner)}\n)")
                self._alias_map[alias] = alias  # CTE name is its own table

        if isinstance(stmt, exp.Union):
            if not isinstance(stmt.this, (exp.Select, exp.Union)) or not isinstance(stmt.expression, exp.Select):
                raise ValueError("UNION requires SELECT branches")
            left = self._translate_statement(stmt.this)
            right = self._translate_select(stmt.expression)
            main = left + "\nappend (\n" + self._indent(right) + "\n)"
            if stmt.args.get("distinct"):
                first = stmt.this
                while isinstance(first, exp.Union):
                    first = first.this
                cols = first.expressions
                if not cols or not all(isinstance(c, exp.Column) for c in cols):
                    raise ValueError("UNION DISTINCT requires simple columns")
                names = [c.name for c in cols]
                if len(names) != len(set(names)):
                    raise ValueError("UNION DISTINCT requires unambiguous column names")
                keys = names[0] if len(names) == 1 else "{" + ", ".join(names) + "}"
                main += f"\ngroup {keys} (take 1)"
            if stmt.args.get("order"):
                main += "\n" + self._translate_order_by(stmt.args["order"])
            if stmt.args.get("limit"):
                limit = stmt.args["limit"].expression
                offset = stmt.args.get("offset")
                if offset:
                    off_expr = offset.expression
                    if (not isinstance(limit, exp.Literal) or not isinstance(off_expr, exp.Literal)
                            or not limit.is_int or not off_expr.is_int
                            or int(limit.this) <= 0 or int(off_expr.this) < 0):
                        raise ValueError("UNION OFFSET requires positive integer LIMIT and nonnegative integer OFFSET")
                    start = int(off_expr.this) + 1
                    main += f"\ntake {start}..{int(off_expr.this) + int(limit.this)}"
                else:
                    main += "\ntake " + self._translate_expression(limit)
        elif isinstance(stmt, exp.Select):
            main = self._translate_select(stmt)
        elif isinstance(stmt, exp.Create):
            main = f"# (SQL DDL: {stmt.sql(dialect='sqlite')})"
        else:
            main = f'# (untranslated SQL)\ns"{{{stmt.sql(dialect="sqlite")}}}"'

        if cte_parts:
            return "\n".join(cte_parts) + "\n\n" + main
        return main

    def _translate_select(self, sel: exp.Select) -> str:
        """Translate a SELECT statement to PRQL pipeline."""
        parts: list[str] = []
        defined: set[str] = set()  # column aliases already defined by prior clauses

        # --- FROM clause (sqlglot key is 'from_', not 'from') ---
        from_expr = sel.args.get("from_")
        if from_expr:
            from_str = self._translate_from(from_expr)
            if from_str:
                parts.append(from_str)

        # --- JOINs ---
        joins = sel.args.get("joins")
        if joins:
            for join in joins:
                parts.append(self._translate_join(join))

        # --- WHERE clause ---
        where = sel.args.get("where")
        if where:
            cond = where.this if hasattr(where, "this") else where
            parts.append(f"filter {self._translate_expression(cond)}")

        # --- GROUP BY ---
        group = sel.args.get("group")
        if group:
            gb_part, gb_defined = self._translate_group_by(group, sel)
            parts.append(gb_part)
            defined.update(gb_defined)

        # --- HAVING ---
        having = sel.args.get("having")
        if having:
            cond = having.this if hasattr(having, "this") else having
            aggregate_aliases = {
                c.this.sql(dialect="sqlite"): c.alias
                for c in sel.expressions
                if isinstance(c, exp.Alias) and self._is_aggregate(c.this)
            }
            cond = cond.transform(
                lambda node: exp.column(aggregate_aliases[node.sql(dialect="sqlite")])
                if node.sql(dialect="sqlite") in aggregate_aliases else node
            )
            if next(cond.find_all(exp.AggFunc), None) is not None:
                raise ValueError("HAVING aggregates must match a selected aggregate alias")
            parts.append(f"filter {self._translate_expression(cond)}")

        # Establish selected aliases before sorting; SQL permits ORDER BY aliases.
        columns = sel.args.get("expressions")
        if (not group and columns and all(isinstance(c, exp.Alias) and self._is_aggregate(c.this)
                                          for c in columns)):
            parts.append('aggregate {' + ', '.join(self._translate_aggregate(c.this, c.alias)
                                                  for c in columns) + '}')
            defined.update(c.alias for c in columns)
        if columns and not self._is_star_select(columns):
            sel_part = self._translate_select_columns(columns, sel, defined)
            if sel_part:
                parts.append(sel_part)

        if sel.args.get("distinct"):
            # Distinct applies after projection and before ordering/limit. PRQL
            # compiles this grouping idiom to SELECT DISTINCT for simple
            # column projections. More complex forms remain guarded by the
            # normal round-trip validator rather than dropping DISTINCT.
            if group or not columns or not all(isinstance(c, exp.Column) for c in columns):
                raise ValueError("DISTINCT requires a simple column projection")
            names = [c.name for c in columns]
            if len(names) != len(set(names)):
                raise ValueError("DISTINCT requires unambiguous column names")
            keys = names[0] if len(names) == 1 else "{" + ", ".join(names) + "}"
            parts.append(f"group {keys} (take 1)")

        # --- ORDER BY ---
        order = sel.args.get("order")
        if order:
            parts.append(self._translate_order_by(order))

        # --- LIMIT ---
        limit = sel.args.get("limit")
        if limit:
            if isinstance(limit, exp.Limit):
                limit_val = self._translate_expression(limit.expression)
            else:
                limit_val = self._translate_expression(limit)
            parts.append(f"take {limit_val}")

        if not parts:
            return f'# (empty SELECT)\ns"{{{sel.sql(dialect="sqlite")}}}"'

        return "\n".join(parts)

    def __init__(self):
        """Initialize with empty alias mapping."""
        super().__init__()
        self._alias_map: dict[str, str] = {}  # alias → real_table_name

    def _translate_from(self, from_expr: exp.Expression) -> str:
        """Translate FROM clause. sqlglot wraps tables in exp.From."""
        if isinstance(from_expr, exp.From):
            inner = from_expr.this
            return self._translate_from(inner)

        if isinstance(from_expr, exp.Table):
            name = from_expr.name  # real table name
            alias = from_expr.alias_or_name  # alias if specified, else same as name
            # Always use the real table name (not alias) for PRQL readability
            self._alias_map[alias] = name
            return f"from {name}"

        if isinstance(from_expr, exp.Subquery):
            alias = from_expr.alias
            inner_prql = self._translate_statement(from_expr.this)
            if alias:
                self._alias_map[alias] = alias  # subquery alias stays
                return f"from {alias} = (\n{self._indent(inner_prql)}\n)"
            return f"from (\n{self._indent(inner_prql)}\n)"

        return f"from {from_expr.sql(dialect='sqlite')}"

    def _translate_join(self, join: exp.Join) -> str:
        """Translate JOIN clause."""
        # sqlglot records LEFT/RIGHT/FULL in `side`, not `kind` (which is
        # commonly empty or OUTER). Reading only `kind` silently turns an
        # outer join into an inner join before the round-trip guard catches it.
        join_kind = join.args.get("side") or join.args.get("kind") or "inner"
        if isinstance(join_kind, str):
            join_type = join_kind.lower()
        else:
            join_type = "inner"

        join_table = join.this
        if isinstance(join_table, exp.Table):
            name = join_table.name  # real table name
            alias = join_table.alias_or_name
            self._alias_map[alias] = name
            table_ref = name
            # For cross joins, skip if kind is cross
            if join_type == "cross":
                table_ref = name
        else:
            table_ref = f"({join_table.sql(dialect='sqlite')})"

        # Map join type to PRQL side:
        # PRQL uses: join side:left table (cond), no cross needed
        prql_side = {"inner": "inner", "left": "left", "right": "right", "full": "full"}
        if join_type == "cross":
            # PRQL cross join: just join without side:
            on_cond = join.args.get("on")
            if on_cond:
                return f"join {table_ref} ({self._translate_join_on(on_cond)})"
            return f"join {table_ref} (true)"

        side = prql_side.get(join_type, "inner")
        on_cond = join.args.get("on")
        if on_cond:
            cond_str = self._translate_join_on(on_cond)
            return f"join side:{side} {table_ref} ({cond_str})"
        else:
            return f"join side:{side} {table_ref} (true)"

    def _translate_join_on(self, expr: exp.Expression) -> str:
        """Translate JOIN ON condition to PRQL join criteria."""
        if isinstance(expr, exp.EQ):
            left = self._translate_expression(expr.left)
            right = self._translate_expression(expr.right)
            # If both reference the same column name (==id pattern)
            if left == right:
                return f"=={left.split('.')[-1]}"
            return f"{left} == {right}"
        if isinstance(expr, exp.And):
            left = self._translate_join_on(expr.left)
            right = self._translate_join_on(expr.right)
            return f"{left} && {right}"
        return self._translate_expression(expr)

    def _translate_group_by(self, group: exp.Expression, sel: exp.Select | None) -> tuple[str, set[str]]:
        """Translate GROUP BY + aggregates to PRQL group.

        Returns:
            (prql_string, set_of_defined_aliases)
        """
        defined: set[str] = set()

        if isinstance(group, exp.Group):
            keys = group.expressions

            # Find aggregate expressions from SELECT
            agg_parts = []
            if sel:
                select_exprs = sel.args.get("expressions", [])
                for se in select_exprs:
                    if isinstance(se, exp.Alias):
                        inner = se.this
                        inner_alias = se.alias
                    else:
                        inner = se
                        inner_alias = inner.alias_or_name

                    if self._is_aggregate(inner):
                        if not isinstance(se, exp.Alias):
                            raise ValueError("Selected aggregates require explicit aliases")
                        agg_prql = self._translate_aggregate(inner, inner_alias)
                        agg_parts.append(agg_prql)
                        defined.add(inner_alias)
                    elif next(inner.find_all(exp.AggFunc), None) is not None:
                        raise ValueError("Unsupported aggregate wrapper")

            key_strs = [self._translate_expression(k) for k in keys]
            # Track group key columns as defined
            for k in keys:
                key_name = k.alias_or_name if hasattr(k, "alias_or_name") else self._translate_expression(k)
                defined.add(key_name)

            if len(key_strs) == 1:
                key_part = key_strs[0]
            else:
                key_part = "{" + ", ".join(key_strs) + "}"

            if agg_parts:
                agg_str = ", ".join(agg_parts)
                return f"group {key_part} (\n  aggregate {{{agg_str}}}\n)", defined
            else:
                # Group without aggregates: just group by keys
                return f"group {key_part} (take 1)", defined

        return f"# GROUP BY: {group.sql(dialect='sqlite')}", defined

    def _is_aggregate(self, expr: exp.Expression) -> bool:
        """Check if an expression is an aggregate function."""
        if isinstance(expr, (exp.Count, exp.Sum, exp.Avg, exp.Max, exp.Min)):
            return True
        if isinstance(expr, exp.Alias):
            return self._is_aggregate(expr.this)
        if isinstance(expr, exp.Coalesce):
            return (
                isinstance(expr.this, exp.Sum)
                and len(expr.expressions) == 1
                and isinstance(expr.expressions[0], exp.Literal)
                and not expr.expressions[0].is_string
                and expr.expressions[0].this == "0"
            )
        return False

    def _translate_aggregate(self, expr: exp.Expression, alias: str) -> str:
        """Translate an aggregate function to PRQL syntax."""
        if isinstance(expr, exp.Alias):
            expr = expr.this
        if isinstance(expr, exp.Coalesce) and self._is_aggregate(expr):
            # PRQL sum compiles to COALESCE(SUM(column), 0), including all-null groups.
            expr = expr.this

        func_name = ""
        if isinstance(expr, exp.Count):
            func_name = "count"
            arg = expr.args.get("this")
            if isinstance(arg, exp.Distinct):
                if len(arg.expressions) != 1:
                    raise ValueError("COUNT DISTINCT requires exactly one expression")
                func_name = "count_distinct"
                arg = arg.expressions[0]
        elif isinstance(expr, exp.Sum):
            func_name = "sum"
            arg = expr.args.get("this")
        elif isinstance(expr, exp.Avg):
            func_name = "average"
            arg = expr.args.get("this")
        elif isinstance(expr, exp.Max):
            func_name = "max"
            arg = expr.args.get("this")
        elif isinstance(expr, exp.Min):
            func_name = "min"
            arg = expr.args.get("this")
        else:
            return f"# aggregate: {expr.sql(dialect='sqlite')}"

        if arg is None:
            arg_str = "this"
        elif isinstance(arg, exp.Star):
            arg_str = "this"
        else:
            arg_str = self._translate_expression(arg)

        return f"{alias} = {func_name} {arg_str}"

    def _translate_order_by(self, order: exp.Expression) -> str:
        """Translate ORDER BY to PRQL sort."""
        if isinstance(order, exp.Order):
            items = []
            for expr in order.expressions:
                if isinstance(expr, exp.Ordered):
                    col = self._translate_expression(expr.this)
                    desc = expr.args.get("desc", False)
                    if desc:
                        items.append(f"-{col}")   # PRQL: sort {-col} wouldn't work inline
                    else:
                        items.append(col)           # PRQL: sort col (no + prefix!)
            if len(items) == 1:
                # For single descending sort, use tuple syntax
                item = items[0]
                if item.startswith("-"):
                    return f"sort {{{item}}}"
                return f"sort {item}"
            # Multiple sort keys
            formatted = []
            for item in items:
                if item.startswith("-"):
                    formatted.append(item)  # Keep the - prefix
                else:
                    formatted.append(f"+{item}")
            return f"sort {{{', '.join(formatted)}}}"
        return f"# ORDER BY: {order.sql(dialect='sqlite')}"

    def _translate_select_columns(
        self, columns: list[exp.Expression], sel: exp.Select, defined: set[str] | None = None
    ) -> str:
        """Translate SELECT column list to PRQL select.

        Args:
            columns: The SELECT expressions from the AST.
            sel: The full Select statement (for context).
            defined: Set of column aliases already defined by prior clauses
                     (e.g. from GROUP BY aggregates). These are skipped.
        """
        defined = defined or set()
        col_strs = []
        for c in columns:
            if isinstance(c, exp.Alias):
                alias = c.alias
                if alias in defined:
                    # Column was already defined by a prior clause
                    col_strs.append(alias)
                else:
                    expr_str = self._translate_expression(c.this)
                    if expr_str != alias:
                        col_strs.append(f"{alias} = {expr_str}")
                    else:
                        col_strs.append(expr_str)
            elif isinstance(c, exp.Star):
                continue  # Don't need explicit select * in PRQL
            else:
                expr_str = self._translate_expression(c)
                col_strs.append(expr_str)

        if not col_strs:
            return ""

        if len(col_strs) == 1:
            return f"select {col_strs[0]}"
        return "select {" + ", ".join(col_strs) + "}"

    @staticmethod
    def _is_star_select(columns: list[exp.Expression]) -> bool:
        """Check if this is a SELECT * or SELECT table.*"""
        if len(columns) == 1 and isinstance(columns[0], exp.Star):
            return True
        return False

    # ------------------------------------------------------------------
    # Expression translation
    # ------------------------------------------------------------------

    def _translate_expression(self, expr: exp.Expression) -> str:
        """Translate a SQL expression to PRQL syntax."""
        if expr is None:
            return "null"

        # --- Unwrap wrappers first ---
        # Many sqlglot types (Where, Having, From, Limit, etc.) are wrappers
        # whose real content is in .this or .expression.
        if isinstance(expr, (exp.Where, exp.Having, exp.From)):
            return self._translate_expression(expr.this)

        if isinstance(expr, exp.Limit):
            return self._translate_expression(expr.expression)

        if isinstance(expr, exp.Order):
            return self._translate_order_by(expr)

        # --- Leaf types ---

        if isinstance(expr, exp.Literal):
            return self._translate_literal(expr)

        if isinstance(expr, exp.Column):
            col = expr.name
            table = expr.table
            if table:
                # Replace SQL alias with real table name
                real_table = self._alias_map.get(table, table)
                return f"{real_table}.{col}"
            return col

        if isinstance(expr, exp.Star):
            return "*"

        # --- Compound types ---

        if isinstance(expr, exp.Alias):
            inner = self._translate_expression(expr.this)
            # If inner is a complex expression, wrap in parens for alias
            return f"{expr.alias} = {inner}"

        # Boolean
        if isinstance(expr, exp.And):
            return f"({self._translate_expression(expr.left)} && {self._translate_expression(expr.right)})"
        if isinstance(expr, exp.Or):
            return f"({self._translate_expression(expr.left)} || {self._translate_expression(expr.right)})"
        if isinstance(expr, exp.Not):
            # `!x == null` negates x before comparison in PRQL. SQLGlot
            # represents `x IS NOT NULL` as NOT(IS(x, NULL)); emit the
            # inequality explicitly so it compiles back to IS NOT NULL.
            if isinstance(expr.this, exp.Is) and isinstance(expr.this.expression, exp.Null):
                return f"{self._translate_expression(expr.this.this)} != null"
            return f"!({self._translate_expression(expr.this)})"

        # Comparisons
        if isinstance(expr, exp.EQ):
            return f"{self._translate_expression(expr.left)} == {self._translate_expression(expr.right)}"
        if isinstance(expr, exp.NEQ):
            return f"{self._translate_expression(expr.left)} != {self._translate_expression(expr.right)}"
        if isinstance(expr, exp.GT):
            return f"{self._translate_expression(expr.left)} > {self._translate_expression(expr.right)}"
        if isinstance(expr, exp.GTE):
            return f"{self._translate_expression(expr.left)} >= {self._translate_expression(expr.right)}"
        if isinstance(expr, exp.LT):
            return f"{self._translate_expression(expr.left)} < {self._translate_expression(expr.right)}"
        if isinstance(expr, exp.LTE):
            return f"{self._translate_expression(expr.left)} <= {self._translate_expression(expr.right)}"

        # Arithmetic
        if isinstance(expr, exp.Add):
            return f"{self._translate_expression(expr.left)} + {self._translate_expression(expr.right)}"
        if isinstance(expr, exp.Sub):
            return f"{self._translate_expression(expr.left)} - {self._translate_expression(expr.right)}"
        if isinstance(expr, exp.Mul):
            return f"{self._translate_expression(expr.left)} * {self._translate_expression(expr.right)}"
        if isinstance(expr, exp.Div):
            # PRQL uses `/` for float division and `//` for integer
            return f"{self._translate_expression(expr.left)} / {self._translate_expression(expr.right)}"

        # IN
        if isinstance(expr, exp.In):
            col = self._translate_expression(expr.this)
            query_expr = expr.args.get("query")
            if isinstance(query_expr, exp.Subquery):
                inner_sql = self._translate_statement(query_expr.this)
                return f"({col} | in ({inner_sql}))"
            else:
                if query_expr:
                    values = self._translate_expression(query_expr)
                else:
                    values = ", ".join(
                        self._translate_expression(e) for e in (expr.expressions or [])
                    )
                return f"{col} | in [{values}]"

        # BETWEEN
        if isinstance(expr, exp.Between):
            col = self._translate_expression(expr.this)
            lo = self._translate_expression(expr.args.get("low"))
            hi = self._translate_expression(expr.args.get("high"))
            return f"({col} | in {lo}..{hi})"

        # IS NULL / IS NOT NULL
        if isinstance(expr, exp.Is):
            inner = self._translate_expression(expr.this)
            if isinstance(expr.expression, exp.Null):
                return f"{inner} == null"
            elif isinstance(expr.expression, exp.Not):
                return f"{inner} != null"
            else:
                return f"{inner} == {self._translate_expression(expr.expression)}"

        # LIKE
        if isinstance(expr, exp.Like):
            col = self._translate_expression(expr.this)
            pattern = self._translate_expression(expr.expression).strip("'")
            return f'({col} ~= "{pattern}")'

        # DISTINCT
        if isinstance(expr, exp.Distinct):
            inner = self._translate_expression(expr.this)
            return f"distinct {inner}"

        # Aggregates
        if isinstance(expr, exp.Count):
            arg = expr.args.get("this")
            is_distinct = expr.args.get("distinct", False)
            if is_distinct:
                if arg is None or isinstance(arg, exp.Star):
                    return "count_distinct this"
                return f"count_distinct {self._translate_expression(arg)}"
            if arg is None or isinstance(arg, exp.Star):
                return "count this"
            return f"count {self._translate_expression(arg)}"
        if isinstance(expr, exp.Sum):
            arg = expr.args.get("this")
            return f"sum {self._translate_expression(arg)}" if arg else "sum this"
        if isinstance(expr, exp.Avg):
            arg = expr.args.get("this")
            return f"average {self._translate_expression(arg)}" if arg else "average this"
        if isinstance(expr, exp.Max):
            arg = expr.args.get("this")
            return f"max {self._translate_expression(arg)}" if arg else "max this"
        if isinstance(expr, exp.Min):
            arg = expr.args.get("this")
            return f"min {self._translate_expression(arg)}" if arg else "min this"

        # CASE WHEN
        if isinstance(expr, exp.Case):
            return self._translate_case(expr)

        # String concatenation
        if isinstance(expr, exp.Concat):
            left = self._translate_expression(expr.left)
            right = self._translate_expression(expr.right)
            return f"({left} + {right})"

        # CAST
        if isinstance(expr, exp.Cast):
            raise ValueError("CAST semantics are unsupported")

        # Parenthesized expression — unwrap
        if isinstance(expr, exp.Paren):
            return f"({self._translate_expression(expr.this)})"

        # Subquery
        if isinstance(expr, exp.Subquery):
            inner_sql = self._translate_statement(expr.this)
            return f"(\n{self._indent(inner_sql)}\n)"

        # Tuple/value list
        if isinstance(expr, exp.Tuple):
            items = [self._translate_expression(e) for e in expr.expressions]
            return "{" + ", ".join(items) + "}"

        # Functions (Anonymous, SQL-specific)
        if isinstance(expr, exp.Anonymous):
            func_name = expr.name.lower() if expr.name else "unknown"
            args = [self._translate_expression(a) for a in (expr.expressions or [])]
            # Map common SQL functions to PRQL
            prql_funcs = {
                "coalesce": "coalesce",
                "ifnull": "coalesce",
                "nullif": "nullif",
                "upper": "string.upper",
                "lower": "string.lower",
                "length": "string.length",
                "trim": "string.trim",
                "ltrim": "string.ltrim",
                "rtrim": "string.rtrim",
                "replace": "string.replace",
                "round": "math.round",
                "abs": "math.abs",
                "ceil": "math.ceil",
                "floor": "math.floor",
                "strftime": "string",
                "substr": "string.slice",
                "instr": "string.contains",
            }
            if func_name == "strftime":
                # strftime → PRQL s-string for date formatting
                return f's"strftime({", ".join(args)})"'
            elif func_name == "julianday":
                # julianday → s-string (SQL-specific date function)
                return f's"julianday({", ".join(args)})"'
            elif func_name in prql_funcs:
                return f"{prql_funcs[func_name]}({', '.join(args)})"
            return f"({func_name} {' '.join(args)})"

        # Default: embed as PRQL s-string (literal SQL)
        try:
            return f's"{expr.sql(dialect="sqlite")}"'
        except Exception:
            return f'# [untranslated: {type(expr).__name__}]'

    def _translate_case(self, case: exp.Case) -> str:
        """Translate CASE WHEN to PRQL case expression."""
        ifs = case.args.get("ifs", [])
        default = case.args.get("default")
        parts = []
        for if_clause in ifs:
            cond = self._translate_expression(if_clause.this)
            true_val = self._translate_expression(if_clause.args.get("true"))
            parts.append(f"  {cond} => {true_val}")
        if default:
            default_val = self._translate_expression(default)
            parts.append(f"  true => {default_val}")
        return "case [\n" + ",\n".join(parts) + "\n]"

    @staticmethod
    def _translate_literal(lit: exp.Literal) -> str:
        """Translate a SQL literal to PRQL."""
        if lit.is_string:
            return f"'{lit.this}'"
        return str(lit.this)

    # ------------------------------------------------------------------
    # Round-trip validation
    # ------------------------------------------------------------------

    def _validate_roundtrip(self, prql: str, original_sql: str) -> TranslationResult:
        """Compile PRQL back to SQL and compare with original."""
        try:
            compiled = prqlc.compile(prql)
            compiled = self._strip_prql_comment(compiled).strip()
            original = original_sql.strip().rstrip(";")

            # Some CTEs are translated faithfully by sqlglot and prqlc. Accept
            # only when both parsers produce exactly the same full SQL tree;
            # unlike the single-SELECT rules below, perform no alias or clause
            # rewriting across query scopes.
            original_tree = self._exact_sql_ast(original)
            if (original_tree.args.get("with_") or isinstance(original_tree, exp.Union)) and original_tree == self._exact_sql_ast(compiled):
                return TranslationResult.ok(compiled, validation="exact_sql_ast")
            if isinstance(original_tree, exp.Union):
                flattened = self._unwrap_union_wrapper(compiled)
                if flattened is not None:
                    expected = original_tree.copy()
                    offset = expected.args.get("offset")
                    if offset and isinstance(offset.expression, exp.Literal) and offset.expression.is_int and int(offset.expression.this) == 0:
                        expected.set("offset", None)
                    if expected == self._exact_sql_ast(flattened):
                        return TranslationResult.ok(compiled, validation="exact_sql_ast")
            if original_tree.args.get("distinct"):
                flattened = self._unwrap_distinct_wrapper(compiled)
                if flattened is not None and self._comparison_ast(flattened) == self._comparison_ast(original):
                    return TranslationResult.ok(compiled, validation="scoped_sql_ast")
            if self._comparison_ast(compiled) == self._comparison_ast(original):
                return TranslationResult.ok(compiled, validation="scoped_sql_ast")
            return TranslationResult.fail([
                "Compiled SQL differs outside the supported equivalence rules; delivery refused"
            ])
        except Exception as e:
            return TranslationResult.fail([f"Round-trip failed: {e}"])

    @staticmethod
    def _unwrap_union_wrapper(sql: str) -> str | None:
        """Recognize an identity CTE wrapping a simple UNION plus sort/limit."""
        tree = PRQLAdapter._exact_sql_ast(sql)
        if not isinstance(tree, exp.Select):
            return None
        allowed = {"expressions", "from_", "with_", "order", "limit", "offset"}
        if any(value for key, value in tree.args.items() if key not in allowed):
            return None
        if tree.args.get("offset") and not tree.args.get("limit"):
            return None
        with_ = tree.args.get("with_")
        if not with_ or len(with_.expressions) != 1 or with_.args.get("recursive"):
            return None
        cte = with_.expressions[0]
        union = cte.this
        from_ = tree.args.get("from_")
        if (not isinstance(union, exp.Union) or not isinstance(union.this, exp.Select)
                or not isinstance(union.expression, exp.Select) or not from_
                or not isinstance(from_.this, exp.Table) or from_.this.name != cte.alias_or_name
                or from_.this.alias or union.args.get("order") or union.args.get("limit")
                or union.args.get("with_") or union.args.get("offset")):
            return None
        left, right, outer = union.this.expressions, union.expression.expressions, tree.expressions
        if not left or not all(isinstance(c, exp.Column) for c in left + right + outer):
            return None
        names = [c.name for c in left]
        if names != [c.name for c in right] or names != [c.name for c in outer]:
            return None
        if any(c.table and c.table != cte.alias_or_name for c in outer):
            return None
        flattened = union.copy()
        for key in ("order", "limit", "offset"):
            if tree.args.get(key):
                flattened.set(key, tree.args[key].copy())
        return flattened.sql(dialect="sqlite")

    @staticmethod
    def _unwrap_distinct_wrapper(sql: str) -> str | None:
        """Flatten the compiler's identity CTE around a DISTINCT projection.

        Only a single table and unchanged simple columns are accepted. The
        outer query may add ordering and limit, but cannot filter, join, group,
        change the projection, or introduce another scope.
        """
        tree = PRQLAdapter._exact_sql_ast(sql)
        with_ = tree.args.get("with_")
        if not with_ or len(with_.expressions) != 1 or with_.args.get("recursive"):
            return None
        cte = with_.expressions[0]
        inner = cte.this
        if not isinstance(inner, exp.Select) or not inner.args.get("distinct"):
            return None
        allowed_outer = {"expressions", "from_", "with_", "order", "limit"}
        if any(value for key, value in tree.args.items() if key not in allowed_outer):
            return None
        if any(inner.args.get(key) for key in ("with_", "joins", "group", "having", "order", "limit", "offset")):
            return None
        from_ = tree.args.get("from_")
        inner_from = inner.args.get("from_")
        if (not from_ or not isinstance(from_.this, exp.Table) or from_.this.name != cte.alias_or_name
                or from_.this.alias or not inner_from or not isinstance(inner_from.this, exp.Table)):
            return None
        if len(list(inner.find_all(exp.Select))) != 1 or len(list(inner.find_all(exp.Table))) != 1:
            return None
        if not inner.expressions or not all(isinstance(c, exp.Column) for c in inner.expressions + tree.expressions):
            return None
        if [c.name for c in inner.expressions] != [c.name for c in tree.expressions]:
            return None
        if any(c.table and c.table != cte.alias_or_name for c in tree.expressions):
            return None
        flattened = inner.copy()
        for key in ("order", "limit"):
            if tree.args.get(key):
                flattened.set(key, tree.args[key].copy())
        return flattened.sql(dialect="sqlite")

    @staticmethod
    def _exact_sql_ast(sql: str) -> exp.Expression:
        statements = [s for s in sqlglot.parse(sql, dialect="sqlite") if s is not None]
        if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
            raise ValueError("Equivalence checking requires one SELECT or UNION")
        tree = statements[0]
        for node in tree.walk():
            node.comments = None
        return tree

    @staticmethod
    def _comparison_ast(sql: str) -> exp.Expression:
        """Compare single-table/JOIN SELECTs with limited alias normalization.

        This is a conservative structural check, not general SQL equivalence.
        Literals, casts, aggregate wrappers, and all query clauses remain intact.
        """
        statements = [s for s in sqlglot.parse(sql, dialect="sqlite") if s is not None]
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise ValueError("Equivalence checking requires one SELECT")
        tree = statements[0]
        if len(list(tree.find_all(exp.Select))) != 1 or tree.args.get("with_"):
            raise ValueError("Subqueries and CTE equivalence are unsupported")
        tables = list(tree.find_all(exp.Table))
        names = [t.name for t in tables]
        if len(set(names)) != len(names):
            raise ValueError("Self-JOIN equivalence is unsupported")
        table_aliases = {t.alias_or_name: t.name for t in tables}
        selected_aliases = {
            c.alias: c.this for c in tree.expressions if isinstance(c, exp.Alias)
        }
        for clause in (tree.args.get("order"), tree.args.get("having")):
            if clause is not None:
                clause.transform(
                    lambda node: selected_aliases[node.name].copy()
                    if isinstance(node, exp.Column) and not node.table
                    and node.name in selected_aliases else node,
                    copy=False,
                )
        for column in tree.find_all(exp.Column):
            if column.table:
                column.set("table", exp.to_identifier(table_aliases.get(column.table, column.table)))
            elif len(tables) == 1:
                column.set("table", exp.to_identifier(tables[0].name))
        for table in tables:
            table.set("alias", None)
        for node in tree.walk():
            node.comments = None
            if isinstance(node, exp.Join) and node.args.get("kind") == "INNER":
                node.set("kind", None)
            if (isinstance(node, exp.Join) and node.args.get("kind") == "OUTER"
                    and node.args.get("side") in ("LEFT", "RIGHT", "FULL")):
                # LEFT JOIN and LEFT OUTER JOIN are equivalent; retain side so
                # the comparison still rejects INNER/RIGHT/FULL substitutions.
                node.set("kind", None)
            if isinstance(node, exp.Ordered) and not node.args.get("desc"):
                node.set("desc", False)
        return tree

    @staticmethod
    def _normalize_sql(sql: str) -> str:
        """Normalize SQL for comparison."""
        # Try to parse and regenerate for canonical form
        try:
            parsed = sqlglot.parse(sql, dialect="sqlite")
            if parsed:
                normalized = " ".join(
                    p.sql(dialect="sqlite") for p in parsed if p is not None
                )
                return normalized.lower().strip()
        except Exception:
            pass
        # Fallback: manual normalization
        s = sql.lower().strip()
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r"\s*,\s*", ", ", s)
        return s

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_prql_comment(sql: str) -> str:
        """Remove the PRQL compiler version comment from SQL output."""
        return re.sub(r"-- Generated by PRQL compiler.*\n?", "", sql)

    @staticmethod
    def _indent(text: str, spaces: int = 4) -> str:
        """Indent each line of text."""
        prefix = " " * spaces
        return "\n".join(prefix + line for line in text.splitlines())
