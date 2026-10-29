Change the familiar Bash filter so the count reports POST requests instead of
GET requests. Do not alter the Nushell-only method grouping block; it must
remain present and unchanged in the delivered target file.

This is a small experimental R/E example. R is the grep/count pipeline visible
to the agent. E is the marked Nushell grouping block, which is carried without
being translated. Check the final Nushell file before treating it as correct.
