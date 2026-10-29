"""Small SQLite demonstrations of the supported PRQL delivery repairs."""
import sqlite3
import unittest
from unittest.mock import patch

import prqlc

from compat_layer.adapters.prql import PRQLAdapter
from compat_layer.interface import TranslationResult
from rosetta_studio.core import _prql_pair


class PRQLRuntimeRepairs(unittest.TestCase):
    def setUp(self):
        self.adapter = PRQLAdapter()
        self.pair = _prql_pair()
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(
            "CREATE TABLE orders(row_id INTEGER, customer_id INTEGER, amount INTEGER, status TEXT);"
            "CREATE TABLE customers(id INTEGER, customer_name TEXT);"
            "CREATE TABLE strings(row_id INTEGER, value TEXT);"
        )
        self.db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", [
            (1, 1, None, "completed"), (2, 1, None, "completed"),
            (3, 2, 5, "completed"), (4, 2, None, "completed"),
            (5, 2, 7, "completed"), (6, 3, 0, "completed"),
            (7, 3, 0, "cancelled"), (8, 999, 99, "completed"),
        ])
        self.db.executemany("INSERT INTO customers VALUES (?, ?)",
                            [(1, "Atlas"), (2, "Beacon"), (3, "Cedar")])
        self.db.executemany("INSERT INTO strings VALUES (?, ?)",
                            [(1, "007"), (2, "abc"), (3, None)])

    def equivalent(self, sql, expected=None):
        original = self.db.execute(sql).fetchall()
        if expected is not None:
            self.assertEqual(original, expected)
        delivered = self.pair.deliver(sql)
        self.assertTrue(delivered.success, (delivered.errors, sql))
        compiled = prqlc.compile(delivered.content)
        self.assertEqual(self.db.execute(compiled).fetchall(), original, compiled)
        self.assertEqual(delivered.metadata["validation"], "scoped_sql_ast")
        return delivered

    def test_compiler_emitted_sum_and_no_edit_cycles(self):
        source = ("from orders\ngroup customer_id (aggregate {revenue = sum amount})\n"
                  "sort {-revenue, customer_id}\nselect {customer_id, revenue}")
        for _ in range(3):
            sql = self.pair.project(source)
            self.assertTrue(sql.success, sql.errors)
            self.assertEqual(sql.metadata["method"], "prqlc")
            source = self.equivalent(sql.content, [(999, 99), (2, 12), (1, 0), (3, 0)]).content

    def test_exact_cte_translation_executes_like_original_and_rejects_changed_filter(self):
        queries = [
            'WITH recent AS (SELECT row_id, amount FROM orders WHERE amount > 0) '
            'SELECT row_id, amount FROM recent WHERE amount < 20 ORDER BY row_id',
            'WITH stage_a AS (SELECT row_id, amount FROM orders WHERE amount > 0), '
            'stage_b AS (SELECT row_id, amount FROM stage_a WHERE amount < 20) '
            'SELECT row_id, amount FROM stage_b ORDER BY row_id',
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                result = self.pair.deliver(sql)
                self.assertTrue(result.success, result.errors)
                self.assertEqual(result.metadata['validation'], 'exact_sql_ast')
                self.assertEqual(self.db.execute(prqlc.compile(result.content)).fetchall(),
                                 self.db.execute(sql).fetchall())
                changed = result.content.replace('amount > 0', 'amount > 1', 1)
                self.assertNotEqual(changed, result.content)
                self.assertFalse(self.adapter._validate_roundtrip(changed, sql).success)

    def test_distinct_simple_columns_deduplicates_and_keeps_null(self):
        for sql in ('SELECT DISTINCT amount FROM orders',
                    'SELECT DISTINCT customer_id, amount FROM orders'):
            with self.subTest(sql=sql):
                result = self.pair.deliver(sql)
                self.assertTrue(result.success, result.errors)
                expected = self.db.execute(sql).fetchall()
                actual = self.db.execute(prqlc.compile(result.content)).fetchall()
                self.assertEqual(set(actual), set(expected))
                self.assertEqual(len(actual), len(expected))
                self.assertTrue(any(None in row for row in actual))
                self.assertFalse(self.adapter._validate_roundtrip(
                    result.content.replace('group ', '# group ', 1), sql).success)

    def test_distinct_order_limit_and_wrapper_guards(self):
        self.db.execute("INSERT INTO orders VALUES (9, 3, NULL, NULL)")
        for sql in ('SELECT DISTINCT status FROM orders ORDER BY status LIMIT 3',
                    'SELECT DISTINCT customer_id, amount FROM orders WHERE row_id >= 3 '
                    'ORDER BY customer_id DESC, amount LIMIT 5'):
            with self.subTest(sql=sql):
                result = self.pair.deliver(sql)
                self.assertTrue(result.success, result.errors)
                self.assertEqual(self.db.execute(prqlc.compile(result.content)).fetchall(),
                                 self.db.execute(sql).fetchall())
        for query in (
            'WITH x AS (SELECT DISTINCT customer_id FROM orders) SELECT customer_id FROM x WHERE customer_id > 1',
            'WITH x AS (SELECT DISTINCT customer_id FROM orders) SELECT 1 FROM x',
            'WITH x AS (SELECT DISTINCT customer_id FROM orders) SELECT customer_id FROM x OFFSET 1',
            'WITH x AS (SELECT DISTINCT customer_id FROM orders) SELECT customer_id FROM x JOIN customers ON x.customer_id = customers.id',
        ):
            with self.subTest(unsafe=query):
                self.assertIsNone(self.adapter._unwrap_distinct_wrapper(query))

    def test_distinct_order_limit_survives_repeated_edits(self):
        template = 'SELECT DISTINCT customer_id, amount FROM orders ORDER BY customer_id, amount LIMIT {n}'
        result = self.pair.deliver(template.format(n=5))
        self.assertTrue(result.success, result.errors)
        for before, after in ((5, 4), (4, 3), (3, 2)):
            with self.subTest(limit=after):
                projected = self.pair.project(result.content)
                self.assertTrue(projected.success, projected.errors)
                view = projected.content.replace('LIMIT\n  ' + str(before), 'LIMIT\n  ' + str(after), 1)
                self.assertNotEqual(view, projected.content)
                result = self.pair.deliver(view)
                self.assertTrue(result.success, result.errors)
                self.assertEqual(self.db.execute(prqlc.compile(result.content)).fetchall(),
                                 self.db.execute(template.format(n=after)).fetchall())

    def test_count_distinct_global_and_grouped_with_nulls(self):
        for sql in ('SELECT COUNT(DISTINCT customer_id) AS n FROM orders',
                    'SELECT customer_id, COUNT(DISTINCT amount) AS n FROM orders '
                    'GROUP BY customer_id ORDER BY customer_id'):
            with self.subTest(sql=sql):
                result = self.pair.deliver(sql)
                self.assertTrue(result.success, result.errors)
                self.assertEqual(self.db.execute(prqlc.compile(result.content)).fetchall(),
                                 self.db.execute(sql).fetchall())
                unsafe = result.content.replace('count_distinct', 'count', 1)
                self.assertNotEqual(unsafe, result.content)
                self.assertFalse(self.adapter._validate_roundtrip(unsafe, sql).success)

    def test_union_and_union_all_keep_duplicates_and_nulls(self):
        self.db.execute("INSERT INTO orders VALUES (9, 3, NULL, 'completed')")
        for operator in ('UNION', 'UNION ALL'):
            sql = ('SELECT amount FROM orders WHERE customer_id = 1 ' + operator +
                   ' SELECT amount FROM orders WHERE customer_id = 2')
            with self.subTest(operator=operator):
                result = self.pair.deliver(sql)
                self.assertTrue(result.success, result.errors)
                self.assertEqual(result.metadata['validation'], 'exact_sql_ast')
                self.assertIn('target:sql.sqlite', result.content)
                actual = self.db.execute(prqlc.compile(result.content)).fetchall()
                expected = self.db.execute(sql).fetchall()
                self.assertEqual(sorted(actual, key=repr), sorted(expected, key=repr))
                self.assertIn((None,), actual)
                if operator == 'UNION':
                    unsafe = result.content.rsplit('\ngroup ', 1)[0]
                    self.assertFalse(self.adapter._validate_roundtrip(unsafe, sql).success)

    def test_three_branch_union_all_preserves_multiplicity(self):
        sql = ('SELECT amount FROM orders WHERE customer_id = 1 UNION ALL '
               'SELECT amount FROM orders WHERE customer_id = 2 UNION ALL '
               'SELECT amount FROM orders WHERE customer_id = 1')
        result = self.pair.deliver(sql)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.metadata['validation'], 'exact_sql_ast')
        actual = self.db.execute(prqlc.compile(result.content)).fetchall()
        expected = self.db.execute(sql).fetchall()
        self.assertEqual(sorted(actual, key=repr), sorted(expected, key=repr))
        self.assertEqual(len(actual), len(expected))

    def test_union_offset_and_limit_preserve_rows(self):
        for operator in ('UNION', 'UNION ALL'):
            for offset in (0, 1, 3):
                sql = ('SELECT amount FROM orders WHERE customer_id = 1 ' + operator +
                       ' SELECT amount FROM orders WHERE customer_id = 2 '
                       f'ORDER BY amount LIMIT 3 OFFSET {offset}')
                with self.subTest(operator=operator, offset=offset):
                    result = self.pair.deliver(sql)
                    self.assertTrue(result.success, result.errors)
                    self.assertEqual(self.db.execute(prqlc.compile(result.content)).fetchall(),
                                     self.db.execute(sql).fetchall())

    def test_is_not_null_in_union_preserves_null_semantics(self):
        sql = ('SELECT amount FROM orders WHERE amount IS NULL UNION ALL '
               'SELECT amount FROM orders WHERE amount IS NOT NULL')
        result = self.pair.deliver(sql)
        self.assertTrue(result.success, result.errors)
        actual = self.db.execute(prqlc.compile(result.content)).fetchall()
        expected = self.db.execute(sql).fetchall()
        self.assertEqual(sorted(actual, key=repr), sorted(expected, key=repr))
        self.assertIn((None,), actual)
        self.assertEqual(len(actual), len(expected))

    def test_union_order_limit_repeated_edits_and_wrapper_guards(self):
        for operator in ('UNION', 'UNION ALL'):
            sql = ('SELECT amount FROM orders WHERE customer_id = 1 ' + operator +
                   ' SELECT amount FROM orders WHERE customer_id = 2 ORDER BY amount LIMIT 5')
            result = self.pair.deliver(sql)
            self.assertTrue(result.success, result.errors)
            for before, after in ((5, 4), (4, 3), (3, 2)):
                view = self.pair.project(result.content)
                self.assertTrue(view.success, view.errors)
                edit = view.content.replace('LIMIT\n  ' + str(before), 'LIMIT\n  ' + str(after), 1)
                self.assertNotEqual(edit, view.content)
                result = self.pair.deliver(edit)
                self.assertTrue(result.success, result.errors)
                expected = sql.rsplit('LIMIT ', 1)[0] + 'LIMIT ' + str(after)
                self.assertEqual(self.db.execute(prqlc.compile(result.content)).fetchall(),
                                 self.db.execute(expected).fetchall())
        for query in (
            'WITH x AS (SELECT amount FROM orders UNION SELECT amount FROM orders) SELECT amount FROM x WHERE amount > 1',
            'WITH x AS (SELECT amount FROM orders UNION SELECT amount FROM orders) SELECT 1 FROM x',
            'WITH x AS (SELECT amount FROM orders UNION SELECT amount FROM orders) SELECT amount FROM x OFFSET 1',
        ):
            with self.subTest(unsafe=query):
                self.assertIsNone(self.adapter._unwrap_union_wrapper(query))

    def test_nullable_sum_count_having_and_join(self):
        self.equivalent(
            "SELECT c.id, c.customer_name, COALESCE(SUM(o.amount), 0) AS revenue, "
            "COUNT(*) AS n FROM orders o JOIN customers c ON o.customer_id = c.id "
            "WHERE o.status = 'completed' GROUP BY c.id, c.customer_name "
            "HAVING COALESCE(SUM(o.amount), 0) >= 0 "
            "ORDER BY revenue DESC, c.id LIMIT 8",
            [(2, "Beacon", 12, 3), (1, "Atlas", 0, 2), (3, "Cedar", 0, 1)],
        )

    def test_left_join_keeps_unmatched_rows_across_edits(self):
        template = ('SELECT o.row_id, c.customer_name FROM orders o LEFT JOIN customers c '
                    'ON o.customer_id = c.id WHERE o.row_id >= {threshold} ORDER BY o.row_id')
        first = self.pair.deliver(template.format(threshold=1))
        self.assertTrue(first.success, first.errors)
        target = first.content
        for before, after in ((1, 3), (3, 5), (5, 8)):
            with self.subTest(threshold=after):
                projected = self.pair.project(target)
                self.assertTrue(projected.success, projected.errors)
                sql = projected.content.replace('>= ' + str(before), '>= ' + str(after), 1)
                self.assertNotEqual(sql, projected.content)
                delivered = self.pair.deliver(sql)
                self.assertTrue(delivered.success, delivered.errors)
                rows = self.db.execute(prqlc.compile(delivered.content)).fetchall()
                self.assertEqual(rows, self.db.execute(template.format(threshold=after)).fetchall())
                self.assertIn((8, None), rows)
                target = delivered.content
        # Normalizing the optional OUTER keyword must not accept an INNER join.
        left = template.format(threshold=1)
        inner = left.replace('LEFT JOIN', 'INNER JOIN')
        self.assertNotEqual(self.adapter._comparison_ast(left), self.adapter._comparison_ast(inner))

    def test_having_selected_alias(self):
        self.equivalent(
            "SELECT customer_id, COALESCE(SUM(amount), 0) AS revenue FROM orders "
            "GROUP BY customer_id HAVING revenue >= 12 ORDER BY revenue DESC, customer_id",
            [(999, 99), (2, 12)],
        )

    def test_sort_selected_alias_nullable_and_string(self):
        self.equivalent("SELECT row_id, amount AS revenue FROM orders ORDER BY revenue, row_id")
        self.equivalent("SELECT row_id, value AS label FROM strings ORDER BY label, row_id",
                        [(3, None), (1, "007"), (2, "abc")])

    def test_cast_refused_with_real_string_and_numeric_data(self):
        queries = [
            ("SELECT row_id, CAST(value AS INTEGER) AS n FROM strings ORDER BY row_id",
             [(1, 7), (2, 0), (3, None)]),
            ("SELECT row_id, CAST(amount AS TEXT) AS label FROM orders WHERE customer_id = 2 ORDER BY row_id",
             [(3, "5"), (4, None), (5, "7")]),
        ]
        for sql, expected in queries:
            self.assertEqual(self.db.execute(sql).fetchall(), expected)
            for translate in (self.pair.deliver, self.adapter.old_to_new):
                with patch("urllib.request.urlopen") as urlopen:
                    result = translate(sql)
                    urlopen.assert_not_called()
                self.assertFalse(result.success)
                self.assertEqual(result.content, "")
                self.assertIn("CAST", " ".join(result.errors))

    def test_join_detail_cumulative_edits(self):
        for condition, direction, limit in [
            ("o.amount >= 0", "DESC", 8),
            ("o.amount >= 0 AND o.status = 'completed'", "DESC", 8),
            ("o.amount >= 7 AND o.status = 'completed'", "ASC", 3),
        ]:
            sql = ("SELECT o.row_id, o.customer_id, c.customer_name, o.amount, o.status "
                   "FROM orders o JOIN customers c ON o.customer_id = c.id WHERE "
                   f"{condition} ORDER BY o.amount {direction}, o.row_id LIMIT {limit}")
            source = self.equivalent(sql).content
            self.equivalent(prqlc.compile(source))

    def test_empty_grouped_input(self):
        self.db.execute("DELETE FROM orders")
        self.equivalent("SELECT customer_id, COALESCE(SUM(amount), 0) AS revenue "
                        "FROM orders GROUP BY customer_id ORDER BY customer_id", [])

    def test_semantic_changes_refused_despite_compilation(self):
        for original, source in [
            ("SELECT SUM(amount) AS revenue FROM orders", "from orders\naggregate {revenue = sum amount}"),
            ("SELECT value FROM strings WHERE value = 'ABC'", "from strings\nfilter value == 'abc'\nselect value"),
        ]:
            self.assertFalse(self.adapter._validate_roundtrip(source, original).success)
        for sql in [
            "SELECT customer_id, COALESCE(SUM(amount), 7) AS revenue FROM orders GROUP BY customer_id",
            "SELECT customer_id, SUM(amount) + 1 AS revenue FROM orders GROUP BY customer_id",
        ]:
            self.assertFalse(self.pair.deliver(sql).success, sql)

    def test_core_preserves_validation_warnings(self):
        with patch.object(PRQLAdapter, "_validate_roundtrip", return_value=TranslationResult.ok(
            "SELECT * FROM orders", warnings=["Scoped check note"]
        )):
            result = self.pair.deliver("SELECT * FROM orders")
        self.assertTrue(result.success)
        self.assertIn("Scoped check note", result.warnings)


if __name__ == "__main__":
    unittest.main()
