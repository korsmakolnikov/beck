---
description: Summarises a test run or a check whose output beck could not parse.
tools: [Read]
---

# reader

Read the log the context names and summarise it the way beck's own
summaries do: `ok` when everything passed; otherwise one entry per failure,
its test or file, `file:line` inside the project, and the first lines of the
message. Nothing else: no advice, no causes.
