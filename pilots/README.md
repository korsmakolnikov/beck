# Pilots

The material of beck's pilot runs, kept for the benchmarks
(`docs/benchmark.md`). Pilots run outside `/tmp`: a reboot wipes it.

* `hello/`: the hello world pilot (iteration `5ca546dd`, branch
  `hello-world-cli`) as the run left it, with its `.beck/` folder (the
  event store under `.beck/events/`, the reports, the configuration). Its
  git history is `hello/hello.git.bundle`; the virtual environment and the
  caches are left out.
* `hello_world_refinement.md`: the first analysis of the hello world run,
  in Italian; `docs/benchmark.md` holds it in English.
* `transcripts/claude-sessions.tar.xz`: the Claude Code transcripts of the
  hello world and ping/pong runs (the agents' turns, with their token
  usage). The request blobs and the attachments are stripped: they carried
  private context and nothing the measures need.

## Restore

```
git clone pilots/hello/hello.git.bundle ~/code/pilots/hello
rsync -a --exclude hello.git.bundle pilots/hello/ ~/code/pilots/hello/
cd ~/code/pilots/hello && uv sync

mkdir -p ~/code/pilots/transcripts
tar -C ~/code/pilots/transcripts -xJf pilots/transcripts/claude-sessions.tar.xz
```

The transcripts' folders are named after the pilots' old paths
(`-tmp-pilots-hello`, `-tmp-pilots-pingpong--beck-worktrees-ping`, …);
read them there rather than under `~/.claude/projects/`.
