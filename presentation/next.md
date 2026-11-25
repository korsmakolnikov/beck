# Next: finish the presentation on the PC that holds the benchmarks

Instructions for the Claude Code session that continues this work. Talk to
the user in Italian; the slides are in Italian, this file and the code in
English. Read the repository's `CLAUDE.md` first: the repository is public.
Delete this file once everything here is done, if the user agrees.

State as of 2026-11-25, branch `docs/beck-presentation`.

## Done: tokens and time, as percentages of minime's

The slides give beck's **tokens and agents' minutes** as percentages of
minime's (minime = 100 %). The user settled the definition: only the agents
count, so the minutes are what the models took to generate, read from the
Claude Code transcripts of both, the same way. The commands, the tests, the
waits and whatever beck itself runs are left out; agents in parallel are
summed, like their tokens. Only complete runs are compared; the sums with
the stopped runs were removed.

`beck benchmark <iteration> --minime <transcripts>` computes both figures
(branch `feat/benchmark-time`, on `event_sourced_core`; its time needs the
transcripts of beck's agents in `~/.claude/projects`). Open copies of the event
stores: the command opens them read-write. It gives exactly these:

| Pilot | First round, tokens | First round, minutes | Rerun, tokens | Rerun, minutes |
|---|---|---|---|---|
| hello | 6 % | 8 % | 11 % | 19 % |
| pingpong, run 2 | 21 % | 22 % | 36 % | 54 % |
| pingpong, run 3 | 17 % | 19 % | - | - |
| paddles | 6 % | 16 % | 14 % | 49 % |
| alien-attack | 34 % | 34 % | 70 % | 84 % |

hello world r2, the clean run: 15 % of the tokens, 31 % of the minutes.
Paddles with kent left out of both (by a throwaway script, not by `beck
benchmark`): 25 % and 24 % in the first round, 59 % and 83 % in the rerun.

The first round had kent on opus, unclebob and martin on sonnet; the rerun had
all of them on opus. minime ran on Opus 5.5 (hello: joshua and simon on Sonnet
5.5), and was never rerun. New runs of beck (r2) change nothing on the slides
until one closes: then run the command and add its figures.

## To verify, then report

1. "hughes e ford … sono tornati il 23 novembre": confirmed on
   `event_sourced_core` (`docs/minime-agents.md`, dated 2026-11-23;
   `src/beck/agents/scopes.py` exists).
2. "argyris … per ora un log", and the user closing the double loop:
   confirmed by `src/beck/argyris.py` on `event_sourced_core`.
3. Pingpong examples: the slides show reconstructed ones. minime's own
   `spec.yaml` and `system.adl` exist in its pingpong pilot (`.sources/`). They
   differ: the ADL has only the cli and actors components, and the properties
   are of kind `model`. To decide: replace the examples, or keep them.
4. Eliminated agents: "joshua, marvin, linus, unclebob-pair" is a reading of
   the docs; simon as a mode of work is a TODO in the README. Not verified.
5. Argyris: 1923-2013, HBR 1977 and the thermostat come from memory (20
   degrees is an adaptation). Verify them before they are presented.
6. A likely objection: "I limiti di minime" says the tokens end before one
   component is done, yet the benchmark compares completed minime runs. The
   pilots are small; a clarifying sentence was suggested, not decided.

Also suggested, not decided: moving the two example slides right after "Perché
ha funzionato: i DSL" (they follow the double loop learning slide now).

## The build

Needs `pandoc` (3.11 was used) and `python3` (standard library only). An
office suite is needed only to look: `make install-onlyoffice` or
`make install-libreoffice`, then `make open`.

* `make` builds `build/beck.pptx` (ignored by git); `make check` also verifies
  layouts, titles, notes, footers, code and text that fits its box.
* `template.pptx`: the original; never edit it. `adapt_template.py` turns it
  into `build/reference.pptx`, and stops if the template is not the expected
  one.
* `slides.lua`: the `::: footer` line and the 18 pt code blocks (64 columns, at
  most about 20 lines).
* `check_deck.py`: the fit is an estimate from average glyph widths, without
  fonts. Claude has never seen the slides rendered by an office suite, only an
  approximate drawing of some of them.

Pitfalls already met:

* a `::: footer` after `:::: {.columns}` makes pandoc split the slide: use it
  only on single-column slides;
* a column with text followed by a table or an image needs the Comparison
  layout, which the template lacks: the build stops on purpose;
* content titles are 50 pt, at most about 43 characters on one line;
* use "double loop learning", never "double loop" alone; mention Copilot CLI
  without any reference to the user's job.
* CI does not run these checks (it has no pandoc).
