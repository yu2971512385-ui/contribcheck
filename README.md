# contribcheck

Look a repository over before spending an evening on a pull request.

Three questions, in the order that saves the most time.

```console
$ contribcheck BurntSushi/ripgrep
# BurntSushi/ripgrep
  ripgrep recursively searches directories for a regex pattern while respecting your gitignore
  69118 stars, last pushed 2026-08-04

## Rules
  [ai] CONTRIBUTING.md: mentions AI assistance
        ## Use of AI
  [ai] AI_POLICY.md: refuses autonomous agents
        AI. **Autonomous agents are not allowed to be used for contributing to this

## Pull requests
  26 merged since 2026-04-14 (bots excluded), 78 open now
  median 0.2 days from opening to merge, over the 26 most recent
```

## 1. Will this project take the contribution?

Projects increasingly have rules that only turn up after a pull request is
closed: an AI policy, a CLA, a requirement to open an issue first, a commit
convention. They live in `CONTRIBUTING.md`, `AGENTS.md`, `AI_POLICY.md`, the
pull request template — nine files, and nobody reads nine files.

`contribcheck` reads them and prints the line it matched, so you can judge the
wording yourself rather than trust a label.

## 2. Do pull requests from outside get merged?

The merge count excludes Dependabot and friends, because otherwise every
repository looks busy. Next to the open count it answers the question you
actually have:

```
  3 merged since 2026-04-14 (bots excluded), 412 open now
  The backlog is much larger than the merge rate.
```

The median time from opening to merge comes from the thirty most recent, which
is enough to tell "same day" from "next quarter".

## 3. Has somebody already reported it?

```console
$ contribcheck uutils/coreutils --dup "date E modifier"
## Already reported?  (date E modifier)
  5 matching issues
    #10958 [open] date: missing modifiers E and O
    #11656 [closed] date: strftime O modifier leaks as literal in C locale
  18 matching pull requests
    #15237 [open] date: drop the E modifier in the C locale
    #10994 [open] date: implement E and O locale modifiers
  Search both: the fix is often already in a pull request.
```

Both halves matter. The usual mistake is to search the issues, find nothing,
and discover afterwards that the fix has been sitting in a pull request for
eight months.

## Install

```console
$ pipx install git+https://github.com/yu2971512385-ui/contribcheck
```

One file, standard library only, Python 3.10 or newer.

Authentication is whatever `gh auth token` gives, or `--token`, or
`GITHUB_TOKEN`. Without any it still works, on the anonymous rate limit.
The search endpoint has a burst limit separate from the hourly one; it is
retried rather than reported, because it clears in seconds.

## Flags

| flag | |
| --- | --- |
| `--dup "TERMS"` | search the issues and the pull requests |
| `--months N` | window for the merge count, default 6 |
| `--token` | GitHub token, if you do not use the `gh` CLI |

Several repositories can be given at once.

## What it does not do

It reports what the files say; it does not decide for you. The wording around
AI assistance in particular ranges from "disclose it" through "a human must
write the review replies" to "these will be closed", and the difference
matters more than any flag could carry — so the matched line is printed and
the reading is yours.

## Licence

MIT.
