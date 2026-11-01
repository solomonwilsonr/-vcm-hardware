# Contributing

Thanks for helping. VCM is an early MVP; small, tested changes are the easiest to review.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
make install        # pip install -e ".[dev]"
make test           # pytest
make lint           # ruff
make demo           # end-to-end demo on examples/gpu-node
```

Python 3.11+. The core packages use only the standard library; FastAPI/httpx are needed only for `services/api` and its tests.

## Layout

Each package lives in its own folder with a single importable top-level module (`packages/core/vcm_core`, `services/ai-engine/vcm_ai`, ...). `pyproject.toml` lists the source roots for pytest and packaging. Dependencies point downwards: core, parsers, diff, graph, validation, ai, trace, workflow, then cli/api.

## Guidelines

- Add a test with every behaviour change. Tests must pass offline.
- Every impact finding must carry one `confidence_class` (`verified`, `rule_based`, `speculative`) plus evidence links. Do not label a heuristic as `verified`.
- Keep docs honest: if a feature is not implemented, mark it as planned. Update the implemented-vs-planned table in `README.md` when you change scope.
- Keep functions small and typed; add docstrings where they explain something non-obvious.
- No network access at runtime by default. Anything that calls out must be opt-in.

## Commits and pull requests

Use short imperative commit subjects. Run `make lint test` before pushing.
