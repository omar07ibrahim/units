# Contributing to UnitSentinel

UnitSentinel is maintained as a fail-closed dimensional verification system.
Changes should keep claims narrow, inputs bounded, and evidence reproducible.

The repository is intentionally unlicensed while Omar selects and documents a
licensing boundary. Public availability does not grant reuse rights. External
contributions are not currently solicited; open an issue before preparing a
substantial patch or contributing third-party code, models, or datasets.

## Development setup

Use a supported Python version (3.11 through 3.14) and install the locked
evidence renderer without lifecycle scripts:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --no-input -e '.[dev]'
npm --prefix tools/evidence ci --ignore-scripts
```

Run the core quality gates:

```bash
ruff check .
ruff format --check .
python -m mypy
python -m coverage run --branch -m unittest discover -s tests -v
python -m coverage report --show-missing --fail-under=95
python -m build
python -m pip_audit
npm --prefix tools/evidence audit --audit-level=high
```

Run every committed evidence check:

```bash
PYTHONPATH=src python -B -m tools.evidence.onnx_evidence --check
PYTHONPATH=src python -B -m tools.evidence.generate --check
PYTHONPATH=src python -B -m tools.evidence.distribution_visuals --check
npm --prefix tools/evidence run check
PYTHONPATH=src python -B -m tools.evidence.repair_evidence --check
npm --prefix tools/evidence run check:repair
PYTHONPATH=src python -B -m tools.evidence.comparison_evidence --check
PYTHONPATH=src python -B -m tools.evidence.comparison_visuals --check
npm --prefix tools/evidence run check:comparison
```

## Contract and evidence changes

- Preserve closed decoders, explicit resource limits, deterministic ordering,
  exact arithmetic, and fail-closed outcomes.
- Use only fixed synthetic graphs, models, captures, and benchmark fixtures.
  Never publish production data, credentials, host paths, or personal details.
- Do not hand-edit generated SVG, PNG, GIF, transcript, JSON evidence, or the
  closed manifest.
- Regenerate through the narrow recorder for the affected slice, render from
  the recorded sources, inspect the actual assets at original size, and rebuild
  the manifest.
- Commit source changes, regenerated output, and provenance records together.
- Label measured snapshots with their environment and avoid generalizing them
  into cross-machine performance claims.
- Keep `REPRODUCED`, compatibility, repair, and ONNX-import claims within the
  boundaries documented in the README and format specifications.

## Pull requests

Keep history linear and give each commit one meaningful responsibility. Describe
the contract effect, adversarial cases, commands run, evidence changed, and any
new nonclaims. Maintainer commits must have Omar Ibrahim as both author and
committer using the repository noreply address.

Report vulnerabilities through a
[private advisory](https://github.com/omar07ibrahim/units/security/advisories/new),
not a public pull request.
