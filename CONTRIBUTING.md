# Contributing

Thanks for looking. This is a small project with strong opinions about correctness, so this
page is mostly about the two or three things that will get a change accepted quickly.

## Before you write code: read SKILL.md

[`SKILL.md`](./SKILL.md) is the canonical architecture spec — ten LAWs plus the
Architecture-Correctness Gate. It is short, and it is not decoration: a change that violates a
LAW gets redesigned rather than merged with a note to fix it later. The two that catch people
out most:

- **LAW 2, permission-faithful retrieval.** Permission trimming is a mandatory default-deny
  filter at query time. No feature may bypass it, and "the caller can pass a flag to skip it"
  is not a design we will take.
- **LAW 7, ports and adapters.** Cloud-specific capability goes behind an existing port. If you
  find yourself importing an Azure or AWS SDK outside `adapters/`, stop and look for the port.

Anything one-way — a schema, a wire format, a security boundary — wants an ADR in
[`docs/ADR/`](./docs/ADR) *before* the code. There are 24 to copy the shape from.

## Running it

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build   # seeded demo
npm ci --prefix tests                                                           # once: jsdom
python3 scripts/run_tests.py                                                    # whole suite
python3 scripts/run_tests.py --selftest                                         # no browser
```

**Node is a prerequisite, not an optional extra.** Fifteen selftest files drive a real surface
through `node`, ten of them mounting it in jsdom, and they resolve jsdom out of
`tests/node_modules`. Without it they now FAIL rather than skip. If you genuinely cannot install
Node, `DBSEARCH_ALLOW_DOM_SKIP=1` turns those failures back into skips - and the runner then
counts them and prints `[PARTIAL]`, because a skip you cannot see is the thing this rule exists
to prevent. That is not hypothetical: those checks silently passed on every clean clone and in
CI from the day they were written until #792.

The runner reports one number for the whole `tests/` directory, and it is a count of FILES, not
of tests. If you narrow it with `--selftest`, `--e2e` or `-k`, or if any DOM check skipped, it
prints `[PARTIAL]` next to the number, because a green figure that quietly skipped a third of the
coverage is worse than no figure. Please do not add a path that reports a number without its
scope.

Some tests need `eval_fixtures/golden_pack_real/`, which is not in the repository — it is built
from third-party datasets we may not redistribute. Those tests skip cleanly with a message.
`scripts/build_real_pack.py` rebuilds it if you want them.

## What a good change looks like

**Tests that could fail.** The bar here is a little unusual and it is the main thing we will
push back on: a test should assert the property a user would notice, not the mechanism. If you
are fixing a leak, assert that the other person's *content* is absent from the response, not
merely that the status code was 403 — a 403 with a body still leaks. If you are adding a guard,
try breaking the code on purpose and confirm the guard goes red. A guard that cannot fail is
not a guard.

**Comments that will still be true next year.** If you change behaviour, fix every docstring
and comment that described the old behaviour, in the same commit. A stale comment outlives the
code it describes and stops the next reader from looking.

**A commit message that explains why.** Look at `git log` for the house style: what was wrong,
what the fix is, why it is safe, and how it was verified. "Fix bug" tells the next person
nothing they could not get from the diff.

## What we are unlikely to accept

- Anything that widens what a caller can see, without an ADR arguing the boundary.
- Large reformatting or dependency churn mixed into a behaviour change.
- New third-party data committed to the repository. Fixtures we redistribute must be ours or
  permissively licensed, and attributed.
- A feature with no failing-first test.

## Security

Please do not open a public issue for anything that could be used to read someone else's data.
[`SECURITY.md`](./SECURITY.md) has the disclosure process.

## Licence

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](./LICENSE), the same terms as the project.
