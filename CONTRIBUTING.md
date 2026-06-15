# Contributing to Engram

Thanks for your interest in improving Engram. This is a solo-maintained,
open-source project; contributions are welcome and reviewed on a best-effort
basis.

## Development setup

Engram uses [uv](https://docs.astral.sh/uv/) for environment and dependency
management. The `dev` optional-dependency group installs `pytest`,
`pytest-asyncio`, and `ruff`.

Run the lint and unit-test suite the same way CI does:

```bash
# Lint
uv run --extra dev ruff check

# Hermetic unit tests (sqlite + httpx.MockTransport — no network, no models)
uv run --extra dev pytest tests/sources tests/research tests/mcp_server tests/common tests/rag tests/reactor -q
```

CI pins Python 3.11 and runs against the committed `uv.lock`
(`uv run --locked --python 3.11 --extra dev ...`); matching that locally is the
safest way to reproduce a green build.

## Tests

This repository is test-driven. **New features and bug fixes should come with
tests.** The hermetic suites (`tests/sources`, `tests/research`,
`tests/mcp_server`, `tests/common`, `tests/rag`, `tests/reactor`) run without
network access, embedding
models, or an LLM, and are what CI gates on — prefer adding coverage there so
your change is verifiable on a hosted runner. (`tests/integration` is excluded
from CI until its fixtures are wired.)

## Code style

Code is linted with [ruff](https://docs.astral.sh/ruff/). The configuration
lives in `pyproject.toml` under `[tool.ruff]` (line length 110, target
`py311`, rule set `E`/`F`/`W`/`I`/`UP`). Run `ruff check` before opening a PR;
keep imports sorted (`I`) and avoid introducing new lint failures.

## Opening a pull request

1. Fork the repository and create a topic branch.
2. Make your change with accompanying tests.
3. Ensure `ruff check` and the hermetic test suites pass locally.
4. Open a PR against `main` with a clear description of the change and its
   motivation.

## Commits & releases

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/).
While the project is on `0.x` it uses **conservative** bumping — features and
fixes both ship as patches, and a minor is a deliberate choice:

- `feat: …` — a new feature → **patch** bump (while `0.x`).
- `fix: …` — a bug fix → **patch** bump.
- `feat!: …` or a `BREAKING CHANGE:` footer → **minor** bump (while `0.x`; it
  will bump **major** once the project reaches `1.0.0`).
- `docs:` / `test:` / `chore:` / `build:` / `ci:` / `refactor:` / `deps:` — no
  release. (`ci:` and `deps:` are also emitted automatically by Dependabot.)
- To cut a minor for a notable-but-non-breaking change, add `Release-As: 0.X.0`
  to the commit.

Releases are automated by
[release-please](https://github.com/googleapis/release-please): every merge to
`main` updates a standing **release PR** that accumulates the changelog and the
version bump computed from these commits. **Cutting a release is just merging
that PR** — release-please then tags the repo, bumps the version everywhere
(`pyproject.toml`, `src/engram/__init__.py`, the plugin manifest), and publishes
the GitHub release. No manual version edits.

## License

Engram is licensed under [AGPL-3.0-or-later](LICENSE). By contributing, you
agree that your contributions are licensed under the same terms.
