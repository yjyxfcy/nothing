# Third-party notices

Rosetta's own source files are licensed under the MIT License in `LICENSE`.
The Python runtime dependencies are installed as separate distributions and
remain under their respective licenses; this repository does not bundle their
source code or the external converter executables.

| Dependency | Use | License in the reviewed distribution | Upstream |
|---|---|---|---|
| `bashlex` (0.18) | Bash parsing for the experimental Bash/Nushell adapter | GPLv3 or later | [idank/bashlex](https://github.com/idank/bashlex) |
| `mcp` (1.30.0) | MCP server SDK | MIT in the reviewed wheel metadata; check the installed distribution for its applicable notices | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) |
| `prqlc` (0.13.14) | PRQL compiler binding | Apache-2.0 | [PRQL/prql](https://github.com/PRQL/prql) |
| `sqlglot` | SQL parsing and transformation | MIT | [tobymao/sqlglot](https://github.com/tobymao/sqlglot) |

In particular, the Bash/Nushell adapter depends on the separately installed
GPL-licensed `bashlex` package. Rosetta does not relicense that dependency.
Anyone redistributing an installation or a combined application should review
the license terms of the exact dependency versions they include. Transitive
Python dependencies have their own notices as well.

External tools such as Tylax, Pandoc, Typst, `sv2v`, and Nushell are not
included. Install them from their maintainers and follow their respective
licenses.
