# Security policy

## Supported code

UnitSentinel has no published release yet. Security fixes target the current
`main` branch only.

| Version | Supported |
| --- | --- |
| `main` | Yes |
| Tagged releases | None published |

## Report a vulnerability privately

Use [GitHub private vulnerability reporting](https://github.com/omar07ibrahim/units/security/advisories/new).
Do not disclose a suspected vulnerability in a public issue, pull request,
terminal capture, generated diagram, or evidence record.

Include the affected revision, the smallest synthetic graph or model that
reproduces the problem, expected and observed behavior, and the security impact.
Do not attach proprietary computation graphs, production ONNX models, training
data, credentials, host paths, or personal information.

## Security boundaries

Graphs, certificates, comparison plans, result documents, and ONNX files are
untrusted inputs. Decoders enforce closed schemas, byte and node limits,
canonical forms, bounded solver work, and fail-closed outcomes. The ONNX adapter
checks a closed static metadata subset and never executes a model; parsing an
untrusted format is still part of the attack surface.

A reproduced certificate proves current semantic reproduction under the
recorded registry and limits. It does not authenticate an issuer or establish
artifact provenance. Evidence and benchmark fixtures are synthetic and must
remain free of secrets and personal data.

## Disclosure process

Reports are triaged through the private advisory. A coordinated fix should add
an adversarial regression test, update affected contracts or evidence, pass the
complete Python/distribution/evidence matrix, and be disclosed only after the
fix is available. Licensing decisions are tracked separately and are not
security reports.
