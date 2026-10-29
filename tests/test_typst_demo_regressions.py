"""Narrow demo regressions; run against release/src, without installing a wheel."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from compat_layer.adapters import typst_pipeline as tp
from compat_layer.adapters.typst_lists import (
    extract_latex_lists,
    extract_typst_lists,
    translate_fragments,
)


class DirectiveTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = tp.TypstPipeline(verbose=False)

    def test_multiline_strings_comments_and_brackets(self):
        directive = '''#show heading: it => {
  /* } ] /* nested ) */ */
  let label = "escaped \\\" } ]"
  // } ignored
  text(fill: red, [#it.body])
}'''
        source = "= Before\n" + directive + "\n== After\nBody."
        marked, blocks = self.pipeline.mark_unrepresentable(source)
        self.assertEqual(blocks, [directive])
        self.assertIn(tp._TYPST_MARK_START + "\n" + directive + "\n" + tp._TYPST_MARK_END, marked)
        self.assertEqual(self.pipeline.mark_unrepresentable(marked), (marked, []))

    def test_multiline_set(self):
        directive = '#set text(\n  size: 11pt,\n  font: "Arial",\n)'
        marked, blocks = self.pipeline.mark_unrepresentable(directive + "\n= Daily")
        self.assertEqual(blocks, [directive])
        self.assertTrue(marked.endswith("= Daily"))

    def test_refuses_unclosed_mismatched_raw_or_oversized_directives(self):
        for source in ['#show heading: it => {\nBody', '#set text(]',
                       '#show heading: [`literal`]', '#set text(\n' + '\n' * 256]:
            with self.subTest(source=source[:40]), self.assertRaises(tp.ConversionError):
                self.pipeline.mark_unrepresentable(source)

    def test_raw_directive_text_is_not_marked(self):
        source = '```sh\n#show heading: it => {\n```'
        self.assertEqual(self.pipeline.mark_unrepresentable(source), (source, []))

    def test_ambiguous_continuation_refuses(self):
        with self.assertRaises(tp.ConversionError):
            self.pipeline.mark_unrepresentable('#set text\n(size: 11pt)')

    def test_existing_inline_e_placement_is_unchanged(self):
        result = tp._deliver_passthrough('- TYPSTPASSTHROUGH0MARK item body', ['#text(fill: red)[E]'])
        self.assertEqual(result, '- #text(fill: red)[E] item body')

    def test_template_defaults_do_not_duplicate_identical_author_directives(self):
        directive = '#set heading(numbering: "1.")'
        template = '#set page(paper: "a4")\n' + directive + '\n#set math.equation(numbering: "(1)")'
        result = tp._deliver_passthrough(template + '\n= Before\nTYPSTPASSTHROUGH0MARK\n== After', [directive])
        self.assertEqual(result.count(directive), 1)
        self.assertNotIn('#set page', result)
        self.assertNotIn('#set math.equation', result)
        self.assertLess(result.index('= Before'), result.index(directive))
        self.assertLess(result.index(directive), result.index('== After'))


class TylaxFormattingTests(unittest.TestCase):
    def test_tylax_paragraph_prefix_is_removed_without_trimming_content(self):
        cases = (
            ('= Heading\n\n Plain paragraph', '= Heading\n\nPlain paragraph'),
            ('= Heading\n\n  Intentionally indented paragraph',
             '= Heading\n\n  Intentionally indented paragraph'),
            ('= Heading\n\nPlain paragraph', '= Heading\n\nPlain paragraph'),
            ('= Heading\n\n```typ\n code line\n```',
             '= Heading\n\n```typ\n code line\n```'),
            ('= Heading\n\n - nested item', '= Heading\n\n - nested item'),
            ('= Heading\n\n #let value = 1', '= Heading\n\n #let value = 1'),
        )
        with tempfile.TemporaryDirectory() as directory:
            fake_tylax = Path(directory) / 't2l.exe'
            fake_tylax.touch()
            for output, expected in cases:
                with self.subTest(output=output):
                    completed = SimpleNamespace(returncode=0, stdout=output)
                    with patch.object(tp, 'TYLAX_BIN', fake_tylax), \
                            patch.object(tp.subprocess, 'run', return_value=completed):
                        first = tp._translate_tylax('\\section{Heading}\nParagraph')
                        second = tp._translate_tylax('\\section{Heading}\nParagraph')
                    self.assertEqual(first, expected)
                    self.assertEqual(second, first)

    @unittest.skipUnless(tp.TYLAX_BIN.exists() and shutil.which('typst'),
                         'needs configured Tylax and Typst')
    def test_real_tylax_round_trip_does_not_add_paragraph_leading_space(self):
        pipeline = tp.TypstPipeline(verbose=False)
        source = '#set page(paper: "a4")\n= Heading\n\nPlain paragraph.\n'
        marked, _ = pipeline.mark_unrepresentable(source)
        view = pipeline.new_to_old(marked)
        self.assertTrue(view.success, view.errors)
        delivered = pipeline.old_to_new(view.content)
        self.assertTrue(delivered.success, delivered.errors)
        self.assertTrue(delivered.metadata['compiles'])
        self.assertIn('\n\nPlain paragraph.', delivered.content)
        self.assertNotIn('\n\n Plain paragraph.', delivered.content)

    @unittest.skipUnless(tp.TYLAX_BIN.exists() and shutil.which('typst'),
                         'needs configured Tylax and Typst')
    def test_tylax_repeated_round_trip_does_not_accumulate_paragraph_indent(self):
        pipeline = tp.TypstPipeline(verbose=False)
        source = (
            '#set page(paper: "a4", margin: 2.4cm)\n'
            '#show heading.where(level: 1): set text(fill: blue)\n'
            '= UI smoke baseline\n\n'
            'This is a synthetic Rosetta interface acceptance fixture.\n'
        )
        indentation_by_round = []
        for round_index in range(4):
            marked, _ = pipeline.mark_unrepresentable(source)
            view = pipeline.new_to_old(marked)
            self.assertTrue(view.success, view.errors)
            edited = view.content
            if round_index == 0:
                edited = edited.replace(
                    r'\section{UI smoke baseline}', r'\section{UI smoke verified}')
            delivered = pipeline.old_to_new(edited)
            self.assertTrue(delivered.success, delivered.errors)
            self.assertTrue(delivered.metadata['compiles'])
            self.assertIn('= UI smoke verified', delivered.content)
            paragraph = next(
                line for line in delivered.content.splitlines()
                if 'This is a synthetic Rosetta interface acceptance fixture.' in line
            )
            indentation_by_round.append(len(paragraph) - len(paragraph.lstrip(' ')))
            source = delivered.content
        self.assertEqual(
            indentation_by_round[1:], indentation_by_round[:-1],
            f'paragraph indentation grew across Tylax round trips: {indentation_by_round}',
        )


class ListScaleTests(unittest.TestCase):
    @unittest.skipUnless(tp.PANDOC_BIN.exists(), 'needs configured Pandoc')
    def test_thousands_of_list_fragments_round_trip_without_marker_loss(self):
        count = 2000
        source = ''.join(
            f'= Section {index}\n\n- item {index}\n\n'
            for index in range(count)
        )

        familiar, typst_fragments = extract_typst_lists(source)
        self.assertEqual(len(typst_fragments), count)
        latex = translate_fragments(
            familiar, typst_fragments, tp.PANDOC_BIN, 'typst', 'latex')

        masked_latex, latex_fragments = extract_latex_lists(latex)
        self.assertEqual(len(latex_fragments), count)
        restored = translate_fragments(
            masked_latex, latex_fragments, tp.PANDOC_BIN, 'latex', 'typst')
        self.assertEqual(restored.count('- item '), count)
        for index in (0, 1, count // 2, count - 1):
            self.assertIn(f'- item {index}', restored)
            self.assertIn(f'= Section {index}', restored)

    def test_thousands_of_raw_blocks_restore_in_one_pass(self):
        count = 2000
        source = '\n\n'.join(
            f'```sh\nprintf "block-{index}"\n```'
            for index in range(count)
        )
        masked, blocks = tp._extract_raw(source, latex=False)
        restored = tp._restore_raw(masked, blocks)
        self.assertEqual(len(blocks), count)
        self.assertEqual(restored.count('\\begin{verbatim}'), count)
        self.assertEqual(restored.count('\\end{verbatim}'), count)
        for index in (0, 1, count // 2, count - 1):
            self.assertIn(f'printf "block-{index}"', restored)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = tp.TypstPipeline(verbose=False)
        self.stack = []
        for name in ['_translate_tylax', '_translate_pandoc_ast',
                     '_translate_pandoc_direct', '_translate_regex']:
            mocker = patch.object(tp, name, return_value='Body\nTYPSTPASSTHROUGH0MARK')
            self.stack.append(mocker)
            mocker.start()
            self.addCleanup(mocker.stop)

    def tex(self, directive):
        return '\\begin{document}\n% @TYPST-PASSTHROUGH\n% ' + directive + '\n% @END-TYPST-PASSTHROUGH\nBody\n\\end{document}'

    def test_final_full_content_checked_and_cache_rechecked(self):
        checked = []
        def compiles(content):
            checked.append(content)
            return '#set text(size: 11pt)' in content
        with patch.object(tp, '_typst_compiles', side_effect=compiles):
            good = self.pipeline.old_to_new(self.tex('#set text(size: 11pt)'))
            bad = self.pipeline.old_to_new(self.tex('#show heading: it => {'))
        self.assertTrue(good.metadata['compiles'])
        self.assertFalse(bad.metadata['compiles'])
        self.assertTrue(bad.metadata['cached'])
        self.assertNotIn('metadata', bad.metadata)
        self.assertTrue(all('TYPSTPASSTHROUGH' not in c.replace(tp._TYPST_MARK_START, '').replace(tp._TYPST_MARK_END, '') for c in checked))
        self.assertIn('#show heading: it => {', checked[-1])

    def test_fresh_failure_and_cached_failure_are_explicit(self):
        with patch.object(tp, '_typst_compiles', return_value=False):
            first = self.pipeline.old_to_new(self.tex('#set text(size: 0pt)'))
            second = self.pipeline.old_to_new(self.tex('#set text(size: 0pt)'))
        self.assertFalse(first.metadata['compiles'])
        self.assertFalse(second.metadata['compiles'])
        self.assertTrue(second.warnings)

    def test_complete_engine_failure_is_failure(self):
        for mocker in self.stack:
            mocker.stop()
        with patch.object(tp, '_translate_tylax', return_value=''), \
             patch.object(tp, '_translate_pandoc_ast', return_value=''), \
             patch.object(tp, '_translate_pandoc_direct', return_value=''), \
             patch.object(tp, '_translate_regex', return_value=''):
            result = self.pipeline.old_to_new('\\section{Daily}')
        self.assertFalse(result.success)

    def test_lost_body_block_tries_next_engine_at_original_position(self):
        source = ('\\begin{document}\n\\section{Intro}\nBefore.\n'
                  '% @TYPST-PASSTHROUGH\n% #set text(fill: red)\n'
                  '% @END-TYPST-PASSTHROUGH\n\\section{Later}\nAfter.\n'
                  '\\end{document}')
        with patch.object(tp, '_translate_tylax', return_value='= Intro\nBefore.\n= Later\nAfter.'), \
             patch.object(tp, '_translate_pandoc_ast', return_value='= Intro\nBefore.\nTYPSTPASSTHROUGH0MARK\n= Later\nAfter.'), \
             patch.object(tp, '_typst_compiles', return_value=True):
            result = self.pipeline.old_to_new(source)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.metadata['engine'], 'pandoc_ast')
        self.assertLess(result.content.index('Before.'), result.content.index('#set text(fill: red)'))
        self.assertLess(result.content.index('#set text(fill: red)'), result.content.index('= Later'))

    def test_lost_body_block_is_not_silently_relocated(self):
        source = ('\\begin{document}\n\\section{Intro}\nBefore.\n'
                  '% @TYPST-PASSTHROUGH\n% #set text(fill: red)\n'
                  '% @END-TYPST-PASSTHROUGH\nAfter.\n\\end{document}')
        with patch.object(tp, '_translate_tylax', return_value='= Intro\nBefore.\nAfter.'), \
             patch.object(tp, '_translate_pandoc_ast', return_value='= Intro\nBefore.\nAfter.'), \
             patch.object(tp, '_translate_pandoc_direct', return_value='= Intro\nBefore.\nAfter.'), \
             patch.object(tp, '_translate_regex', return_value='= Intro\nBefore.\nAfter.'):
            result = self.pipeline.old_to_new(source)
        self.assertFalse(result.success)
        self.assertTrue(any('lost its position' in error for error in result.errors))

    def test_missing_later_leading_block_is_not_reordered(self):
        with self.assertRaises(tp.ConversionError):
            tp._deliver_passthrough('TYPSTPASSTHROUGH0MARK\n= Body',
                                    ['#set text(fill: red)', '#set text(fill: blue)'], {0, 1})

    def test_unsupported_lists_and_unprotected_flags_refuse(self):
        for source in ['curl --fail https://example.org']:
            with self.subTest(source=source), self.assertRaises(tp.ConversionError):
                self.pipeline.old_to_new(source)


class RawTests(unittest.TestCase):
    def test_projection_is_real_verbatim_and_preserves_bytes(self):
        commands = 'curl --fail https://example.org/health\n\n  kubectl logs pod --tail=80 | sort'
        extracted, blocks = tp._extract_raw('```sh\n' + commands + '\n```', latex=False)
        projected = tp._restore_raw(extracted, blocks)
        self.assertEqual(projected, '\\begin{verbatim}\n' + commands + '\n\\end{verbatim}')
        extracted, blocks = tp._extract_raw(projected, latex=True)
        delivered = tp._restore_raw(extracted, blocks)
        self.assertEqual(delivered, '```\n' + commands + '\n```')

    def test_raw_placement_loss_and_duplicate_refuse(self):
        for translated in ['Body', '- ROSETTARAWBLOCK0MARK',
                           'ROSETTARAWBLOCK0MARK\nROSETTARAWBLOCK0MARK']:
            with self.subTest(translated=translated), self.assertRaises(tp.ConversionError):
                tp._restore_raw(translated, ['```\ncurl --fail\n```'])

    def test_raw_delimiters_and_unsafe_verbatim_refuse(self):
        for source in ['```sh\ncurl --fail', '  ```sh\ncurl --fail\n```',
                       '```\n\\end{verbatim}\n```']:
            with self.subTest(source=source), self.assertRaises(tp.ConversionError):
                tp._extract_raw(source, latex=False)


@unittest.skipUnless(tp.TYLAX_BIN.exists() and shutil.which('typst'), 'needs configured Tylax and Typst')
class RuntimeDemoTests(unittest.TestCase):
    def test_tylax_dropped_preamble_block_is_preserved(self):
        source = ('\\documentclass{article}\n'
                  '% @TYPST-PASSTHROUGH\n% #set text(fill: red)\n'
                  '% @END-TYPST-PASSTHROUGH\n\\begin{document}\n'
                  '\\section{Intro}\nBody.\n\\end{document}\n')
        pipeline = tp.TypstPipeline(verbose=False)
        result = pipeline.old_to_new(source)
        self.assertTrue(result.success, result.errors)
        self.assertTrue(result.metadata['compiles'])
        self.assertEqual(result.content.count('#set text(fill: red)'), 1)
        self.assertLess(result.content.index('#set text(fill: red)'), result.content.index('= Intro'))
        self.assertNotIn('#set page(paper: "a4")', result.content)
        view = pipeline.new_to_old(result.content)
        self.assertTrue(view.success, view.errors)
        delivered = pipeline.old_to_new(view.content)
        self.assertTrue(delivered.success, delivered.errors)
        self.assertEqual(delivered.content.count('#set text(fill: red)'), 1)
    def test_nested_and_ordered_lists_use_pandoc_fragments_across_three_edits(self):
        source = ('#set page(margin: 2cm)\n= Runbook\n\n'
                  '+ Install\n+ Verify\n  - Compile\n  - Run tests\n\n'
                  '== Recovery\n\n- Restore the backup\n- Retry\n')
        expected = source.replace('Run tests', 'Run the full test suite')
        pipeline = tp.TypstPipeline(verbose=False)
        view = pipeline.new_to_old(source)
        self.assertTrue(view.success, view.errors)
        self.assertIn(r'\begin{enumerate}', view.content)
        self.assertIn(r'\begin{itemize}', view.content)
        self.assertIn('% ROSETTA-LIST-START 0 tight', view.content)
        self.assertIn('% ROSETTA-LIST-START 1 tight', view.content)
        self.assertEqual(view.metadata['list_fragments'], 2)

        current_source = source
        for before, after in [('Run tests', 'Run the full test suite'),
                              ('Install', 'Install dependencies'),
                              ('Retry', 'Retry after cleanup')]:
            current_view = pipeline.new_to_old(current_source)
            self.assertTrue(current_view.success, current_view.errors)
            self.assertIn('% ROSETTA-LIST-START 0 tight', current_view.content)
            edited_view = current_view.content.replace(before, after, 1)
            self.assertNotEqual(edited_view, current_view.content)
            expected = current_source.replace(before, after, 1)
            delivered = pipeline.old_to_new(edited_view)
            self.assertTrue(delivered.success, delivered.errors)
            self.assertTrue(delivered.metadata['compiles'])
            self.assertNotIn('ROSETTA-LIST-', delivered.content)
            self.assertEqual(self.query(delivered.content, 'enum'), self.query(expected, 'enum'))
            self.assertEqual(self.query(delivered.content, 'list'), self.query(expected, 'list'))
            current_source = delivered.content
        self.assertIn('Run the full test suite', current_source)
        self.assertIn('Install dependencies', current_source)
        self.assertIn('Retry after cleanup', current_source)

    def test_new_latex_nested_list_is_converted_by_pandoc(self):
        latex = (r'\section{Runbook}' + '\n' +
                 r'\begin{enumerate}' + '\n' + r'\item First' + '\n' +
                 r'\item Second' + '\n' + r'\begin{itemize}' + '\n' +
                 r'\item Nested' + '\n' + r'\end{itemize}' + '\n' +
                 r'\end{enumerate}' + '\n')
        result = tp.TypstPipeline(verbose=False).old_to_new(latex)
        self.assertTrue(result.success, result.errors)
        self.assertTrue(result.metadata['compiles'])
        self.assertIn('+ First', result.content)
        self.assertIn('+ Second', result.content)
        self.assertIn('- Nested', result.content)

    def test_code_fence_nested_in_list_survives_three_edits(self):
        source = ('= Service runbook\n\n'
                  '+ Inspect service\n\n'
                  '  ```sh\n'
                  '  systemctl status api\n'
                  '  journalctl -u api -n 50\n'
                  '  ```\n\n'
                  '+ Restart service\n')
        pipeline = tp.TypstPipeline(verbose=False)
        for old, new in [('50', '100'), ('Inspect', 'Check'),
                         ('Restart', 'Verify restart')]:
            view = pipeline.new_to_old(source)
            self.assertTrue(view.success, view.errors)
            self.assertIn(r'\begin{verbatim}', view.content)
            edited = view.content.replace(old, new, 1)
            self.assertNotEqual(edited, view.content)
            delivered = pipeline.old_to_new(edited)
            self.assertTrue(delivered.success, delivered.errors)
            self.assertTrue(delivered.metadata['compiles'])
            self.assertIn('```', delivered.content)
            self.assertIn(new, delivered.content)
            self.assertIn('journalctl -u api -n 100', delivered.content)
            source = delivered.content

    def test_function_form_ordered_and_bullet_lists_use_pandoc(self):
        source = ('= API\n\n#enum(start: 3, [First], [Second])\n\n'
                  '#list([alpha], [beta])\n')
        pipeline = tp.TypstPipeline(verbose=False)
        view = pipeline.new_to_old(source)
        self.assertTrue(view.success, view.errors)
        self.assertIn(r'\begin{enumerate}', view.content)
        self.assertIn(r'\setcounter{enumi}{2}', view.content)
        self.assertIn(r'\begin{itemize}', view.content)
        delivered = pipeline.old_to_new(view.content)
        self.assertTrue(delivered.success, delivered.errors)
        self.assertTrue(delivered.metadata['compiles'])
        def without_auto_item_number(value):
            if isinstance(value, list):
                return [without_auto_item_number(item) for item in value]
            if isinstance(value, dict):
                return {key: without_auto_item_number(item) for key, item in value.items()
                        if not (key == 'number' and item == 'auto')}
            return value
        self.assertEqual(without_auto_item_number(self.query(delivered.content, 'enum')),
                         without_auto_item_number(self.query(source, 'enum')))
        self.assertIn('- alpha', delivered.content)
        self.assertIn('- beta', delivered.content)

        current = delivered.content
        for before, after in [('First', 'First revised'), ('beta', 'beta revised')]:
            projected = pipeline.new_to_old(current)
            self.assertTrue(projected.success, projected.errors)
            self.assertIn('% ROSETTA-LIST-START 0 tight', projected.content)
            edited = projected.content.replace(before, after, 1)
            self.assertNotEqual(edited, projected.content)
            current = pipeline.old_to_new(edited).content
            self.assertTrue(tp._typst_compiles(current))
            if before == 'First':
                self.assertIn('First revised', current)
            else:
                self.assertIn('beta revised', current)

    def test_math_delimiters_and_following_prose_survive_three_rounds(self):
        from compat_layer.adapters.latex_preprocessor import preprocess
        def without_math_spaces(value):
            if isinstance(value, list):
                return [without_math_spaces(v) for v in value if v != {'func': 'space'}]
            if isinstance(value, dict):
                result = {k: without_math_spaces(v) for k, v in value.items()}
                if result.get('func') == 'mat' and 'delim' not in result:
                    result['delim'] = ['(', ')']
                return result
            return value
        latex = 'Before \\[x^2 + y^2 = z^2\\] after. And \\(x + y\\) end.'
        cleaned = preprocess(latex).content
        self.assertIn('\\]', cleaned)
        self.assertIn('$x + y$', cleaned)
        for formula in ('$x^2 + y^2 = z^2$', '$ sum_(i=1)^n i = (n (n+1)) / 2 $',
                        '$ mat(1, 2; 3, 4) $', '$ mat(delim: "[", 1, 2; 3, 4) $',
                        '$ mat(delim: #none, 1, 2; 3, 4) $'):
            with self.subTest(formula=formula):
                original = '= Report\n\nBefore ' + formula + ' after.\n\nReview marker alpha.\n'
                equations = without_math_spaces(self.query(original, 'math.equation'))
                source = original
                for before, after in [('alpha', 'bravo'), ('bravo', 'charlie'), ('charlie', 'delta')]:
                    pipeline = tp.TypstPipeline(verbose=False)
                    marked, _ = pipeline.mark_unrepresentable(source)
                    view = pipeline.new_to_old(marked)
                    self.assertTrue(view.success, view.errors)
                    self.assertIn('Review marker ' + before, view.content)
                    result = pipeline.old_to_new(view.content.replace('Review marker ' + before, 'Review marker ' + after))
                    self.assertTrue(result.success, result.errors)
                    self.assertTrue(result.metadata['compiles'])
                    self.assertIn('after.', result.content)
                    self.assertEqual(without_math_spaces(self.query(result.content, 'math.equation')), equations)
                    source = result.content

    def test_math_in_native_block_is_carried_through(self):
        pipeline = tp.TypstPipeline(verbose=False)
        source = ('= Report\n// @TYPST-PASSTHROUGH\n'
                  '$ x^2 + y^2 = z^2 $\n// @END-TYPST-PASSTHROUGH\n\nNotes.')
        view = pipeline.new_to_old(source)
        self.assertTrue(view.success, view.errors)
        result = pipeline.old_to_new(view.content)
        self.assertTrue(result.success, result.errors)
        self.assertIn('$ x^2 + y^2 = z^2 $', result.content)

    def test_multiple_matrices_keep_default_and_explicit_delimiters(self):
        source = '= Report\n\n$ mat(1, 2; 3, 4) + mat(delim: "[", 5, 6; 7, 8) $\n'
        pipeline = tp.TypstPipeline(verbose=False)
        view = pipeline.new_to_old(source)
        self.assertTrue(view.success, view.errors)
        self.assertIn('\\begin{pmatrix}', view.content)
        self.assertIn('\\begin{bmatrix}', view.content)
        target = pipeline.old_to_new(view.content)
        self.assertTrue(target.success, target.errors)
        self.assertTrue(target.metadata['compiles'])
        math = self.query(target.content, 'math.equation')
        children = math[0]['body'].get('children', [])
        matrices = [child for child in children if child.get('func') == 'mat']
        self.assertEqual([matrix.get('delim', ['(', ')']) for matrix in matrices], [['(', ')'], ['[', ']']])

    def test_matrix_mapping_mismatch_is_refused(self):
        with self.assertRaisesRegex(tp.ConversionError, 'Cannot reliably match'):
            tp._restore_default_matrix_delimiters('$mat(1,2;3,4)$', 'no matrix environment')

    def test_image_figure_is_preserved_as_native_source(self):
        pipeline = tp.TypstPipeline(verbose=False)
        figure = '#figure(\n  image("chart.png", width: 2cm),\n  caption: [A chart],\n)'
        marked, regions = pipeline.mark_unrepresentable('= Report\n\n' + figure + '\n\nText.')
        self.assertEqual(regions, [figure])
        projected = pipeline.new_to_old(marked)
        self.assertTrue(projected.success, projected.errors)
        delivered = pipeline.old_to_new(projected.content)
        self.assertTrue(delivered.success, delivered.errors)
        self.assertIn(figure, delivered.content)

    def test_real_identical_template_defaults_are_stable_over_six_rounds(self):
        directives = ['#set page(paper: "a4", margin: 2.4cm)',
                      '#set heading(numbering: "1.")',
                      '#set math.equation(numbering: "(1)")']
        for include_equation in (False, True):
            with self.subTest(include_equation=include_equation):
                authored = directives if include_equation else directives[:2]
                source = '\n'.join(authored) + '\n= Daily\nBody.\n== Recovery\nCheck.'
                for index in range(6):
                    pipeline = tp.TypstPipeline(verbose=False)
                    marked, _ = pipeline.mark_unrepresentable(source)
                    projected = pipeline.new_to_old(marked)
                    self.assertTrue(projected.success, projected.errors)
                    result = pipeline.old_to_new(projected.content)
                    self.assertTrue(result.success, result.errors)
                    self.assertTrue(result.metadata['compiles'])
                    self.assertEqual(result.content.count(tp._TYPST_MARK_START), len(authored))
                    for directive in authored:
                        self.assertEqual(result.content.count(directive), 1)
                    if not include_equation:
                        self.assertNotIn(directives[2], result.content)
                    if index:
                        self.assertEqual(result.content, source)
                    source = result.content

    def test_adjacent_native_directives_remain_separate(self):
        pipeline = tp.TypstPipeline(verbose=False)
        first = '#set page(paper: "a4")'
        second = '#show heading: it => {\n  text(fill: blue, it.body)\n}'
        source = first + '\n' + second + '\n= Daily\nBody.'
        marked, _ = pipeline.mark_unrepresentable(source)
        projected = pipeline.new_to_old(marked)
        self.assertTrue(projected.success, projected.errors)
        self.assertEqual(projected.content.count('% @TYPST-PASSTHROUGH'), 2)
        result = pipeline.old_to_new(projected.content)
        self.assertTrue(result.metadata['compiles'])
        self.assertEqual(result.content.count(first), 1)
        self.assertEqual(result.content.count(second), 1)

    def query(self, source, selector):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'demo.typ'
            path.write_text(source, encoding='utf-8')
            result = subprocess.run(['typst', 'query', str(path), selector],
                                    capture_output=True, encoding='utf-8', timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_daily_document_edit_and_repeated_delivery(self):
        pipeline = tp.TypstPipeline(verbose=False)
        directive = '#show heading: it => {\n  text(fill: red, it.body)\n}'
        source = ('= Daily runbook\n' + directive + '\n== Recovery\n'
                  '- Check health\n- Record result\n\n'
                  '#table(columns: 2, [Service], [Status], [Gateway], [Ready])\n\n'
                  '```sh\ncurl --fail https://example.org/health\n'
                  'kubectl logs pod --tail=80\n```\n')
        self.assertTrue(tp._typst_compiles(source))
        for round_number in range(3):
            marked, _ = pipeline.mark_unrepresentable(source)
            view = pipeline.new_to_old(marked)
            self.assertTrue(view.success, view.errors)
            self.assertIn('\\begin{verbatim}', view.content)
            self.assertNotIn('```', view.content)
            edited = view.content.replace('--tail=80', '--tail=120')
            delivered = pipeline.old_to_new(edited)
            self.assertTrue(delivered.success, delivered.errors)
            self.assertTrue(delivered.metadata['compiles'])
            self.assertTrue(tp._typst_compiles(delivered.content))
            self.assertEqual(delivered.content.count(directive), 1)
            self.assertLess(delivered.content.index('= Daily'), delivered.content.index(directive))
            self.assertLess(delivered.content.index(directive), delivered.content.index('== Recovery'))
            self.assertIn('curl --fail https://example.org/health', delivered.content)
            self.assertIn('kubectl logs pod --tail=120', delivered.content)
            self.assertIn('#table(', delivered.content)
            self.assertIn('- Check health', delivered.content)
            cached = pipeline.old_to_new(edited)
            self.assertTrue(cached.metadata['compiles'])
            self.assertEqual(cached.content, delivered.content)
            if round_number > 0:
                self.assertEqual(delivered.content, source)
            source = delivered.content
        raw = self.query(source, 'raw')
        self.assertEqual(len(raw), 1)
        self.assertTrue(raw[0]['block'])
        self.assertEqual(raw[0]['text'], 'curl --fail https://example.org/health\nkubectl logs pod --tail=120')
        self.assertEqual([h['level'] for h in self.query(source, 'heading')], [1, 2])
        self.assertEqual(len(self.query(source, 'list')), 1)
        self.assertEqual(len(self.query(source, 'table')), 1)

    def test_real_multiline_set_remains_single_and_idempotent(self):
        pipeline = tp.TypstPipeline(verbose=False)
        directive = '#set text(\n  size: 11pt,\n  fill: black,\n)'
        source = directive + '\n= Daily\nBody.'
        for index in range(2):
            marked, _ = pipeline.mark_unrepresentable(source)
            view = pipeline.new_to_old(marked)
            result = pipeline.old_to_new(view.content)
            self.assertTrue(result.metadata['compiles'])
            self.assertEqual(result.content.count(directive), 1)
            self.assertLess(result.content.index(directive), result.content.index('= Daily'))
            if index:
                self.assertEqual(source, result.content)
            source = result.content

    def test_real_invalid_preserved_code_is_never_claimed_compilable(self):
        pipeline = tp.TypstPipeline(verbose=False)
        source = '\\section{Daily}\n% @TYPST-PASSTHROUGH\n% #show heading: it => {\n% @END-TYPST-PASSTHROUGH\nBody'
        for _ in range(2):
            result = pipeline.old_to_new(source)
            self.assertFalse(result.metadata['compiles'])
            self.assertFalse(tp._typst_compiles(result.content))


if __name__ == '__main__':
    unittest.main()
