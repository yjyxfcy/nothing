from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError

from compat_layer.adapters.eda import EdaVerilogAdapter, SV2V_BIN
from compat_layer.interface import TranslationResult
from rosetta_studio.core import Pair, experimental_pairs
from rosetta_studio.mcp_server import RosettaMCP


class DemoSafetyTests(unittest.TestCase):
    def test_passthrough_markers_must_be_present_once_in_both_adapters(self):
        from compat_layer.adapters.bash_nu.converter import _splice_passthrough as splice_nu
        from compat_layer.adapters.bash_nu.ir import ConversionError as NuError
        from compat_layer.adapters.eda import _splice_passthrough as splice_sv
        from compat_layer.adapters.eda import ConversionError as SvError

        for splice, error, marker in (
            (splice_nu, NuError, 'print "__NU_PASSTHROUGH_0__"'),
            (splice_sv, SvError, '// __SV_PASSTHROUGH_0__'),
        ):
            with self.subTest(marker=marker):
                self.assertEqual(splice(marker, ['native code']), 'native code')
                for text in ('', marker + '\n' + marker, marker + ' trailing'):
                    with self.subTest(text=text), self.assertRaises(error):
                        splice(text, ['native code'])

    @unittest.skipUnless(shutil.which('nu'), 'requires Nushell')
    def test_final_grep_matches_an_empty_line(self):
        pair = experimental_pairs()['Bash / Nushell (experimental)']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'input.log').write_bytes(b'\n')
            for command, expected in [("cat input.log | grep '^$'", b'\n'),
                                      ("cat input.log | grep -c '^$'", b'1\n'),
                                      ('cat input.log | grep -v absent', b'\n')]:
                with self.subTest(command=command):
                    result = pair.deliver(command)
                    self.assertTrue(result.success, result.errors)
                    view = pair.project(result.content)
                    self.assertTrue(view.success, view.errors)
                    self.assertNotIn('@NU-PASSTHROUGH', view.content)
                    script = root / 'script.nu'
                    script.write_text(result.content, encoding='utf-8')
                    run = subprocess.run(['nu', '--no-config-file', str(script)], cwd=root,
                                         capture_output=True, timeout=20)
                    self.assertEqual(run.stdout, expected)
                    self.assertEqual(run.returncode, 0, run.stderr)

    @unittest.skipUnless(shutil.which('nu'), 'requires Nushell')
    def test_final_grep_count_prints_zero_and_preserves_status(self):
        pair = experimental_pairs()['Bash / Nushell (experimental)']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'input.log').write_bytes(b'alpha\nbeta\nalpha\n')
            for term, expected, status in [('alpha', b'2\n', 0), ('absent', b'0\n', 1)]:
                with self.subTest(term=term):
                    result = pair.deliver('cat input.log | grep -c ' + term)
                    self.assertTrue(result.success, result.errors)
                    view = pair.project(result.content)
                    self.assertTrue(view.success, view.errors)
                    self.assertNotIn('@NU-PASSTHROUGH', view.content)
                    self.assertIn('grep -c ' + term, view.content)
                    again = pair.deliver(view.content)
                    self.assertTrue(again.success, again.errors)
                    script = root / 'script.nu'
                    script.write_text(again.content, encoding='utf-8')
                    run = subprocess.run(['nu', '--no-config-file', str(script)], cwd=root,
                                         capture_output=True, timeout=20)
                    self.assertEqual(run.stdout, expected)
                    self.assertEqual(run.returncode, status, run.stderr)

    @unittest.skipUnless(shutil.which('nu'), 'requires Nushell')
    def test_final_grep_preserves_status_and_remains_editable(self):
        pair = experimental_pairs()['Bash / Nushell (experimental)']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'input.log').write_bytes(b'alpha\nbeta\n')
            for term, expected, status in [('alpha', b'alpha\n', 0), ('absent', b'', 1)]:
                with self.subTest(term=term):
                    result = pair.deliver('cat input.log | grep ' + term)
                    self.assertTrue(result.success, result.errors)
                    view = pair.project(result.content)
                    self.assertTrue(view.success, view.errors)
                    self.assertNotIn('@NU-PASSTHROUGH', view.content)
                    self.assertIn('grep ' + term, view.content)
                    again = pair.deliver(view.content)
                    self.assertTrue(again.success, again.errors)
                    script = root / 'script.nu'
                    script.write_text(again.content, encoding='utf-8')
                    run = subprocess.run(['nu', '--no-config-file', str(script)], cwd=root,
                                         capture_output=True, timeout=20)
                    self.assertEqual(run.stdout, expected)
                    self.assertEqual(run.returncode, status, run.stderr)

    @unittest.skipUnless(shutil.which('nu'), 'requires Nushell')
    def test_wc_counts_newlines_even_without_final_terminator(self):
        pair = experimental_pairs()['Bash / Nushell (experimental)']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for data in (b'', b'alpha', b'alpha\n', b'alpha\r\nbeta', b'a\nb\n'):
                with self.subTest(data=data):
                    (root / 'input.log').write_bytes(data)
                    delivered = pair.deliver('cat input.log | wc -l')
                    self.assertTrue(delivered.success, delivered.errors)
                    view = pair.project(delivered.content)
                    self.assertTrue(view.success, view.errors)
                    self.assertNotIn('@NU-PASSTHROUGH', view.content)
                    again = pair.deliver(view.content)
                    self.assertTrue(again.success, again.errors)
                    path = root / 'script.nu'
                    path.write_text(again.content, encoding='utf-8')
                    result = subprocess.run(['nu', '--no-config-file', str(path)], cwd=root,
                                            capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, f'{data.count(bytes([10]))}\n'.encode())

    @unittest.skipUnless(shutil.which('nu'), 'requires Nushell')
    def test_wc_capture_stays_editable_and_counts_newlines(self):
        pair = experimental_pairs()['Bash / Nushell (experimental)']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'input.log').write_bytes(b'first\nlast')
            result = pair.deliver('count=$(cat input.log | wc -l)\necho "$count"')
            self.assertTrue(result.success, result.errors)
            view = pair.project(result.content)
            self.assertTrue(view.success, view.errors)
            self.assertNotIn('@NU-PASSTHROUGH', view.content)
            self.assertIn('wc -l', view.content)
            script = root / 'script.nu'
            script.write_text(result.content, encoding='utf-8')
            run = subprocess.run(['nu', '--no-config-file', str(script)], cwd=root,
                                 capture_output=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, b'1\n')

    @unittest.skipUnless(shutil.which('nu'), 'requires Nushell')
    def test_raw_cat_preserves_bytes_across_projection_and_following_command(self):
        pair = experimental_pairs()['Bash / Nushell (experimental)']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for data in (b'', b'alpha', b'alpha\n', b'alpha\r\n'):
                (root / 'input.log').write_bytes(data)
                for suffix in ('', '\necho done'):
                    with self.subTest(data=data, suffix=suffix):
                        first = pair.deliver('cat input.log' + suffix)
                        self.assertTrue(first.success, first.errors)
                        view = pair.project(first.content)
                        self.assertTrue(view.success, view.errors)
                        self.assertNotIn('@NU-PASSTHROUGH', view.content)
                        second = pair.deliver(view.content)
                        self.assertTrue(second.success, second.errors)
                        path = root / 'script.nu'
                        path.write_text(second.content, encoding='utf-8')
                        result = subprocess.run(['nu', '--no-config-file', str(path)], cwd=root,
                                                capture_output=True, timeout=20)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, data + (b'done\n' if suffix else b''))

    @unittest.skipUnless(shutil.which('nu'), 'requires Nushell')
    def test_pipeline_stdout_has_exact_newlines_with_empty_and_nonempty_results(self):
        pair = experimental_pairs()['Bash / Nushell (experimental)']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'operations.log').write_bytes(b'WARN b\nINFO skip\nWARN a\n')
            for expression, expected in [
                ('cat operations.log | grep WARN | sort', b'WARN a\nWARN b\n'),
                ('cat operations.log | grep MISSING | sort', b''),
                ('cat operations.log | grep WARN | sort\necho done', b'WARN a\nWARN b\ndone\n'),
                ('cat operations.log | grep MISSING | sort\necho done', b'done\n'),
                ('cat operations.log | grep WARN | sort | uniq -c',
                 b'      1 WARN a\n      1 WARN b\n'),
                ('cat operations.log | grep WARN | sort | uniq -c | sort -nr',
                 b'      1 WARN b\n      1 WARN a\n'),
            ]:
                with self.subTest(expression=expression):
                    delivered = pair.deliver(expression)
                    self.assertTrue(delivered.success, delivered.errors)
                    view = pair.project(delivered.content)
                    self.assertTrue(view.success, view.errors)
                    self.assertNotIn('@NU-PASSTHROUGH', view.content)
                    path = root / 'script.nu'
                    path.write_text(delivered.content, encoding='utf-8')
                    run = subprocess.run(['nu', '--no-config-file', str(path)], cwd=root,
                                         capture_output=True, timeout=20)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(run.stdout, expected)

    @unittest.skipUnless(shutil.which('nu'), 'requires Nushell')
    def test_global_dedup_remains_global_across_round_trip(self):
        pair = experimental_pairs()['Bash / Nushell (experimental)']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'input.log').write_bytes(b'alpha\nbeta\nalpha\ngamma\nbeta\n')
            first = pair.deliver("cat input.log | awk '!seen[$0]++'")
            self.assertTrue(first.success, first.errors)
            view = pair.project(first.content)
            self.assertTrue(view.success, view.errors)
            self.assertIn("awk '!seen[$0]++'", view.content)
            second = pair.deliver(view.content)
            self.assertTrue(second.success, second.errors)
            script = root / 'script.nu'
            script.write_text(second.content, encoding='utf-8')
            run = subprocess.run(['nu', '--no-config-file', str(script)], cwd=root,
                                 capture_output=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, b'alpha\nbeta\ngamma\n')

    @unittest.skipUnless(shutil.which('nu'), 'requires Nushell')
    def test_crlf_input_preserves_carriage_returns_in_shell_pipeline(self):
        pair = experimental_pairs()['Bash / Nushell (experimental)']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'input.log').write_bytes(b'WARN b\r\nINFO skip\r\nWARN a\r\n')
            for script, expected in [
                ('cat input.log | grep WARN | sort', b'WARN a\r\nWARN b\r\n'),
                ('cat input.log | grep WARN | sort | uniq -c', b'      1 WARN a\r\n      1 WARN b\r\n'),
                ('cat input.log | grep MISSING | sort', b''),
            ]:
                with self.subTest(script=script):
                    delivered = pair.deliver(script)
                    self.assertTrue(delivered.success, delivered.errors)
                    projected = pair.project(delivered.content)
                    self.assertTrue(projected.success, projected.errors)
                    self.assertNotIn('@NU-PASSTHROUGH', projected.content)
                    path = root / 'script.nu'
                    path.write_text(delivered.content, encoding='utf-8')
                    run = subprocess.run(['nu', '--no-config-file', str(path)], cwd=root,
                                         capture_output=True, timeout=20)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(run.stdout, expected)

    def test_verilog_rewrites_only_code(self):
        opaque = (
            '// reg status; always @* lexical sentinel\n'
            '/* reg; always @(posedge clk) */\n'
            'initial $display("reg status; always @*; \\"reg\\"");\n'
            'wire \\reg ;\n'
        )
        source = ('module demo(input wire clk, output reg value);\n' + opaque
                  + 'always @(posedge clk) value <= 1;\nendmodule\n')
        result = EdaVerilogAdapter().old_to_new(source)
        self.assertTrue(result.success)
        self.assertIn(opaque, result.content)
        self.assertIn('output logic value', result.content)
        self.assertIn('always_ff @(posedge clk)', result.content)

    def test_sv_include_refusal_explains_project_boundary(self):
        adapter = EdaVerilogAdapter()
        result = adapter.new_to_old('`include "defs.svh"\nmodule demo; endmodule\n')
        self.assertFalse(result.success)
        self.assertIn('project-aware', ' '.join(result.errors))

    @unittest.skipUnless(SV2V_BIN.exists(), 'requires sv2v')
    def test_sv_numeric_macro_and_timescale_survive_three_views(self):
        adapter = EdaVerilogAdapter()
        source = (
            '`timescale 1ns/1ps\n'
            '`define WIDTH 8 // configurable bus width\n'
            'module demo(input logic [`WIDTH-1:0] data,\n'
            '            output logic [`WIDTH-1:0] result);\n'
            '  always_comb result = data;\n'
            'endmodule\n'
        )
        projected = adapter.new_to_old(source)
        self.assertTrue(projected.success, projected.errors)
        self.assertTrue(projected.content.startswith(
            '`timescale 1ns/1ps\n`define WIDTH 8 // configurable bus width\n'))
        self.assertIn('[`WIDTH - 1:0]', projected.content)
        self.assertNotIn('[7:0]', projected.content)

        delivered = adapter.old_to_new(projected.content)
        self.assertTrue(delivered.success, delivered.errors)
        self.assertIn('`timescale 1ns/1ps', delivered.content)
        self.assertIn('`define WIDTH 8 // configurable bus width', delivered.content)
        self.assertIn('[`WIDTH - 1:0]', delivered.content)
        self.assertIn('always_comb', delivered.content)

        # A later agent edit to the macro remains a parameterized artifact and
        # can be projected again through the real converter.
        edited = delivered.content.replace('`define WIDTH 8', '`define WIDTH 16')
        reprojected = adapter.new_to_old(edited)
        self.assertTrue(reprojected.success, reprojected.errors)
        self.assertIn('`define WIDTH 16', reprojected.content)
        self.assertIn('[`WIDTH - 1:0]', reprojected.content)

        for width, artifact in [(8, delivered.content), (16, edited)]:
            with self.subTest(width=width), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'demo.sv'
                path.write_text(artifact, encoding='utf-8')
                run = subprocess.run([str(SV2V_BIN), str(path)], capture_output=True,
                                     text=True, timeout=20)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn(f'[{width - 1}:0]', run.stdout)

    @unittest.skipUnless(SV2V_BIN.exists(), 'requires sv2v')
    def test_sv_preprocessor_constructs_that_cannot_be_reconstructed_fail_closed(self):
        adapter = EdaVerilogAdapter()
        for source, expected in [
            ('`ifdef SYNTHESIS\nmodule demo; endmodule\n`endif\n', 'conditional preprocessor'),
            ('`define WIDTH(x) ((x) + 1)\nmodule demo; endmodule\n', 'function-like macro'),
            ('`define WIDTH W+1\nmodule demo(input logic [`WIDTH-1:0] data); endmodule\n',
             'single numeric literal'),
        ]:
            with self.subTest(expected=expected):
                result = adapter.new_to_old(source)
                self.assertFalse(result.success)
                self.assertIn(expected, ' '.join(result.errors))

    @unittest.skipUnless(SV2V_BIN.exists(), 'requires sv2v')
    def test_sv_comments_survive_conversion_and_reprojection(self):
        adapter = EdaVerilogAdapter()
        comments = [
            '// Module documentation.',
            '/* Port documentation. */',
            '// Describes the combinational assignment.',
            '// File footer.',
        ]
        source = (
            comments[0] + '\n'
            'module demo(input logic a, /* Port documentation. */ output logic y);\n'
            '  // Describes the combinational assignment.\n'
            '  always_comb y = a;\n'
            'endmodule // File footer.\n'
        )
        projected = adapter.new_to_old(source)
        self.assertTrue(projected.success, projected.errors)
        self.assertTrue(projected.warnings)
        for comment in comments:
            with self.subTest(comment=comment):
                self.assertIn(comment, projected.content)

        delivered = adapter.old_to_new(projected.content)
        self.assertTrue(delivered.success, delivered.errors)
        for comment in comments:
            with self.subTest(delivered=comment):
                self.assertIn(comment, delivered.content)
        again = adapter.new_to_old(delivered.content)
        self.assertTrue(again.success, again.errors)
        for comment in comments:
            self.assertIn(comment, again.content)

    @unittest.skipUnless(SV2V_BIN.exists(), 'requires sv2v')
    def test_sv_many_comments_survive_reprojection(self):
        count = 1000
        source = ('module chip(input logic a, output logic y);\n'
                  + ''.join(f'// signal {i:04d}\nwire w{i} = a;\n' for i in range(count))
                  + f'assign y = w{count - 1};\nendmodule\n')
        adapter = EdaVerilogAdapter()
        first = adapter.new_to_old(source)
        self.assertTrue(first.success, first.errors)
        delivery = adapter.old_to_new(first.content)
        self.assertTrue(delivery.success, delivery.errors)
        second = adapter.new_to_old(delivery.content)
        self.assertTrue(second.success, second.errors)
        for i in range(count):
            self.assertIn(f'// signal {i:04d}', first.content)
            self.assertIn(f'// signal {i:04d}', second.content)

    @unittest.skipUnless(SV2V_BIN.exists(), 'requires sv2v')
    def test_sv_package_macro_generate_and_multimodule_edit_cycle(self):
        adapter = EdaVerilogAdapter()
        source = '''// Control-path source for the generated register-bank fixture.
`timescale 1ns/1ps
`default_nettype none // implicit nets are forbidden in this fixture
`define DATA_W 24
package control_pkg;
  typedef enum logic [1:0] {IDLE=0, LOAD=1, DRAIN=2} state_t;
  typedef struct packed {logic valid; logic [`DATA_W-1:0] payload;} item_t;
endpackage
module datapath(input logic clk, rst,
                input logic [`DATA_W-1:0] d,
                output logic [`DATA_W-1:0] q);
  import control_pkg::*;
  state_t state;
  item_t item;
  always_ff @(posedge clk) begin
    if (rst) q <= '0;
    else q <= d + 1;
  end
  for (genvar g=0; g<64; g++) begin : gen_lane
    logic [`DATA_W-1:0] lane;
    always_ff @(posedge clk) lane <= d ^ g;
  end
endmodule
module top(input logic clk, rst,
           input logic [`DATA_W-1:0] input_data,
           output logic [`DATA_W-1:0] output_data);
  datapath u_datapath(.clk(clk), .rst(rst), .d(input_data), .q(output_data));
endmodule // Fixture footer.
`default_nettype wire
'''
        projected = adapter.new_to_old(source)
        self.assertTrue(projected.success, projected.errors)
        for preserved in ('`timescale 1ns/1ps', '`define DATA_W 24',
                          '`default_nettype none // implicit nets are forbidden in this fixture',
                          '`default_nettype wire',
                          '// Control-path source', '// Fixture footer.'):
            self.assertIn(preserved, projected.content)
        self.assertIn('[`DATA_W - 1:0]', projected.content)

        edited = projected.content.replace('d + 1', 'd + 3', 1)
        delivered = adapter.old_to_new(edited)
        self.assertTrue(delivered.success, delivered.errors)
        self.assertIn('d + 3', delivered.content)
        self.assertIn('state', delivered.content)
        self.assertIn('item', delivered.content)
        self.assertIn('gen_lane', delivered.content)

        reprojected = adapter.new_to_old(delivered.content)
        self.assertTrue(reprojected.success, reprojected.errors)
        self.assertIn('`define DATA_W 24', reprojected.content)
        self.assertIn('d + 3', reprojected.content)

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'datapath.sv'
            target.write_text(delivered.content, encoding='utf-8')
            run = subprocess.run([str(SV2V_BIN), str(target)], capture_output=True,
                                 text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('[23:0]', run.stdout)
            changed_width = delivered.content.replace('`define DATA_W 24',
                                                       '`define DATA_W 32')
            target.write_text(changed_width, encoding='utf-8')
            run = subprocess.run([str(SV2V_BIN), str(target)], capture_output=True,
                                 text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('[31:0]', run.stdout)

    def test_nonfinal_log_pipeline_stays_editable(self):
        pair = experimental_pairs()['Bash / Nushell (experimental)']
        source = 'cat operations.log | grep WARN | sort | head -n 3\necho end\n'
        first = pair.deliver(source)
        self.assertTrue(first.success, first.errors)
        projected = pair.project(first.content)
        self.assertTrue(projected.success, projected.errors)
        self.assertNotIn('@NU-PASSTHROUGH', projected.content)
        self.assertIn('head -n 3', projected.content)
        second = pair.deliver(projected.content.replace('head -n 3', 'head -n 5'))
        self.assertTrue(second.success, second.errors)
        self.assertIn('first 5', second.content)
        if shutil.which('nu'):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / 'operations.log').write_text('WARN b\nINFO skip\nWARN a\n')
                target = root / 'report.nu'
                target.write_text(first.content)
                run = subprocess.run(['nu', '--no-config-file', str(target)], cwd=root,
                                     capture_output=True, text=True, timeout=20)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(run.stdout, 'WARN a\nWARN b\nend\n')

    def test_mcp_refuses_known_noncompiling_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = RosettaMCP(root, root / 'events.sqlite3')
            for metadata in ({'compiles': False}, {'metadata': {'compiles': False}}):
                with self.subTest(metadata=metadata):
                    app.pairs['Test'] = Pair('Test', '.new', '.old',
                        lambda text: TranslationResult.ok(text),
                        lambda text: TranslationResult.ok('invalid output', **metadata))
                    opened = app._start_session('Test', root / 'new.new', 'view', None, False)
                    app.sessions[opened['session_id']].preview_view = 'previous preview'
                    with self.assertRaisesRegex(ToolError, 'target compiler'):
                        asyncio.run(app.server.call_tool('rosetta_preview_delivery',
                                                         {'session_id': opened['session_id']}))
                    self.assertIsNone(app.sessions[opened['session_id']].preview_view)
                    self.assertFalse((root / 'new.new').exists())


if __name__ == '__main__':
    unittest.main()
