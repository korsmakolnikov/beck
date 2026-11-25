# Next: finish the presentation on the PC that holds the benchmarks

Instructions for the Claude Code session that continues this work. Talk to
the user in Italian; the slides are in Italian, this file and the code in
English. Read the repository's `CLAUDE.md` first: the repository is public.
Delete this file once everything here is done, if the user agrees.

State as of 2026-11-25, branch `docs/beck-presentation`.

## The task: the time percentages

The user wants only two figures from the beck vs minime comparison: **the
tokens and the time of beck, as percentages of minime's** (minime = 100 %).
The tokens are done. The time was never measured on the first PC (it had
neither minime's runs nor beck's later ones), so the slides say "da
misurare". Do not invent or estimate a figure: one you cannot compute stays
"da misurare".

1. **Agree on the definition with the user first.** Proposed, not yet
   approved: *active time*, from a run's first event to its last, minus the
   waits for the user and for usage limits. Ask whether to report this, the
   plain wall-clock time, or both. beck runs components in parallel, so the
   sum of the turns is a third measure: do not confuse it with elapsed time.
2. **Find the runs**: for each pilot (hello, pingpong, paddles,
   alien-attack), minime's run, beck's first round (sonnet) and the rerun on
   minime's models (opus), and the clean r2 run of hello world. They must be
   the ones behind the token percentages below: check by recomputing the
   tokens. If one is missing, say which.
3. **Compute, with a script, not by hand.** If the checkout has `beck
   benchmark` (the slides name it; the branches on the first PC did not have
   it), see whether it reports time already, and extend it there with its
   test rather than writing a throwaway script; ask before changing beck.
4. **Write the percentages** in `slides.md`, replacing every "da misurare"
   (`grep -n 'da misurare' slides.md`), and the sentence "Il tempo è ancora da
   misurare." in the notes of "Come si misura", where the definition goes.
5. `make check`, then `make open`, and let the user look.

### Where the times are

* **beck**: the event store `.beck/events/<iteration>.db`, table `event`,
  column `at` (ISO 8601, UTC). `AgentTurnEnded` has `duration_ms` in its JSON
  payload; `AgentTurnStarted` marks the start. The waits for the user lie
  between `DecisionRequested` (and the questions) and the events whose
  `actor` is `user`; the usage limits show as `AgentTurnFailed` followed by a
  gap. Checked on the hello world store of 2026-11-17 (`5ca546dd`); later
  stores may have changed. That run is **not** a benchmark: it took 09:55 to
  22:44 UTC, mostly waiting for usage limits, and held a runaway refactor
  loop (about 1,230 martin turns), since fixed.
* **minime**: the Claude Code transcripts,
  `~/.claude/projects/<project>/**/*.jsonl`: each line has a `timestamp`.
  Count each message once, by its id. The sub-agents have transcripts of
  their own. `kent-events.jsonl` may also help, under
  `~/.local/state/minime/<repository>/` or `/tmp`.
* Open the stores read-only; copy nothing private into the repository.

### Token percentages on the slides

| Pilot | First round (beck on sonnet) | Rerun (opus) |
|---|---|---|
| hello | 6 % | 11 % |
| pingpong | 21 % | 36 % |
| paddles | 6 % | 14 % |
| alien-attack | 18 % | 70 % |

hello world r2, the clean run: 15 %. They were converted from the ratios of
the first version of the slides (`git log -p slides.md`), not recomputed.
Uncomfortable figures kept in the notes: paddles 25 % without minime's kent,
19 % with the rerun's blocked attempts; alien-attack 110 % with the round its
bugs stopped; hello r2 116 % with the failed attempts. The user has not said
whether to keep them: ask.

## To verify here, then report

The first PC could not check these:

1. "hughes e ford … sono tornati il 23 novembre", `src/beck/agents/scopes.py`
   and `docs/minime-agents.md` (notes of "Perché beck" and "Gli agenti di
   beck"). The newest commit of `event_sourced_core` it saw is `fd2334d`
   (2026-11-18); the later work may exist only on this PC.
2. "argyris … per ora un log" (`.beck/argyris.log`), and the note "il double
   loop learning lo chiude ancora l'utente", which is a deduction.
3. Pingpong examples: rebuilt in minime's formats from beck's pilot (the
   domain and the architecture in its transcripts). The spec passes minime's
   `spec-check --stage domain`; the ADL has been checked only by eye. If
   minime's own `spec.yaml` and `.adl` for pingpong are here, use them.
4. Eliminated agents: "joshua, marvin, linus, unclebob-pair" is a reading of
   the docs; simon as a mode of work is a TODO in the README.
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
