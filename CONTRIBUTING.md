# Contributing to xmlstreamer

Thanks for your interest. A few honest notes before you invest time, so
your effort lands well.

## The most valuable contribution: broken-feed reports

This library exists to survive malformed real-world feeds, so a
reproducible report of a feed it mishandles is worth more than most
code. When filing one:

- **Never paste full feed URLs.** A URL can carry credentials in its
  query string. Strip it, or describe the source instead.
- Reduce the feed to the smallest snippet that reproduces the problem
  (a handful of items is usually enough), and redact any private data.
- Include what you expected, what happened, and the WARNING lines from
  the `xmlstreamer` logger if any.

## Pull requests

External pull requests are welcome and reviewed manually. Two things
to expect:

- Review may take a while, and small, focused changes are far easier to
  land than large ones. For anything non-trivial, open an issue first
  so the approach is agreed before you write code, and read
  `ARCHITECTURE.md`: it explains the two-layer design and the
  invariants your change must preserve.
- Changes may land rebased as individual commits rather than through
  the merge button; your authorship and credit are preserved.

Before submitting:

- Add tests for what you change (or say why none apply). Keep backward
  compatibility unless the change is explicitly justified.
- Run the test suite on both supported floors:
  `uv run pytest tests/ -q` and
  `uv run --isolated -p 3.11 pytest tests/ -q`.
- Lint gate: `uvx ruff check --select F401,F811,F841 xmlstreamer/ tests/`.
- If you touch `COOKBOOK.md`, its snippets are executed by
  `tests/test_cookbook.py`; if you touch `benchmarks/`, run
  `uv run --group bench python benchmarks/run_all.py --quick`.
- Match the surrounding style: ASCII source, short present-tense
  comments, and synthetic example data (the suite uses book catalogs).

## Developer Certificate of Origin

External contributions must be signed off. By adding a

    Signed-off-by: Your Name <your@email>

trailer to your commits (`git commit -s`), you certify the Developer
Certificate of Origin 1.1 (https://developercertificate.org): in short,
that you wrote the change or otherwise have the right to submit it
under this project's license. Forgot it? `git commit --amend -s` and
force-push your branch. Maintainer commits do not carry the trailer;
the requirement applies to external contributions.

Two related points:

- If an employer or organization could hold rights over your
  contribution, obtain their authorization before contributing. Do not
  submit proprietary code, confidential information, credentials,
  personal data, or any material you are not authorized to share.
- `Signed-off-by` is a statement, not a cryptographic signature. If you
  sign your commits with GPG, `git commit -s -S` is welcome - but only
  `-s` is required.

## Licensing

This project is MIT licensed. By contributing, you agree that your
contribution is licensed under the same terms (see `LICENSE`). You
retain the copyright to your contribution.
