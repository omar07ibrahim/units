# Proof certificate and replay contract

UnitSentinel certificates are detached, canonical positive-verification claims.
Factory issuance records one completed bounded verification and preserves
enough source identity and semantic evidence for an independent current replay.
The bytes remain deliberately unsigned.

The current schema is:

```text
unitsentinel.proof-certificate/v1
```

## Security posture

A factory-issued certificate records:

- a SHA-256 content identity that a caller can compare with an externally
  trusted expected digest;
- a claimed binding to one canonical graph digest and one registry snapshot;
- a complete source-labelled constraint catalog;
- exact inferred contracts and the verified result digest;
- the verifier, solver, limits, and check count reported by issuance.

It does **not** provide:

- an issuer identity or signature;
- proof that the graph is scientifically correct;
- proof that tensor payloads were executed;
- authority to load code, plugins, registries, or graphs from a URL;
- permission for replay to trust the resource limits claimed by the issuer.

Callers that need identity must authenticate the certificate bytes in a
separate envelope. Human and JSON CLI outputs keep this distinction visible as
`authentication: not-provided`; that explanatory field is not part of the
closed seven-field certificate document.

## Canonical byte boundary

`decode_certificate()` accepts exact `bytes` and applies the same closed JSON
discipline as the graph decoder:

- at most 2,097,152 bytes;
- valid UTF-8 without a BOM;
- no duplicate fields, floats, `NaN`, or infinities;
- at most eight nested containers, 2,112 items in one container, and 65,536
  total JSON values;
- at most 192 characters per string and ten digits per JSON integer;
- exact root and nested field sets;
- compact key-sorted encoding with no trailing newline;
- byte-for-byte equality after semantic decode and re-encode.

Semantic construction additionally caps the catalog at 2,112 constraints,
contracts at 576, checks performed at 1,025, and the solver-version string at
32 characters.

The SHA-256 digest is computed over the complete canonical certificate bytes.
It is a property of the `ProofCertificate` value rather than a self-referential
field inside the JSON document.

Successful decode establishes a canonical, internally coherent unsigned claim,
including its embedded verification-result digest. It does not authenticate an
issuer, prove that issuance happened, or replay the claimed graph semantics.
Direct `ProofCertificate` construction is likewise an untrusted claim.

## Closed root fields

The root contains exactly seven fields:

| Field | Bound evidence |
| --- | --- |
| `schema` | Certificate schema identity |
| `graph` | Graph schema and SHA-256 |
| `registry` | Registry schema, version, and SHA-256 |
| `verifier` | Implementation, semantic contract, and version |
| `solver` | Solver implementation and version |
| `run` | Checks performed and exact issuance limits |
| `proof` | Positive outcome, result digest, contracts, and constraints |

Every constraint record contains exactly:

```text
constraint_id, rule, source, source_id
```

Every inferred contract contains exactly:

```text
value_id, dimension, kind, scale, offset
```

Dimensions contain only nonzero, sorted SI-base terms with canonical reduced
rational exponents. Scale and offset are canonical reduced rational strings;
they never cross the certificate boundary as JSON floats.

## Positive-only issuance

`create_certificate()` issues only when all of these conditions hold:

1. graph, registry, and solver limits are exact validated runtime values;
2. one bounded verification attempt returns an exact `VerificationResult`;
3. status is `verified`;
4. graph and registry digests match the supplied sources;
5. inferred contracts cover every declared graph value exactly once;
6. the ordered constraints equal the compiler's current source catalog;
7. the graph, registry, limits, result, and constructed certificate still
   validate after construction.

Conflict, underconstrained, and unknown results cannot be represented as a
positive certificate. An exception, source mutation, partial coverage, stale
binding, or unexpected result subtype fails issuance closed.

The certificate records issuance limits as provenance. They are not executable
instructions for a later replay.

## Detached replay

Replay requires the certificate and the caller-supplied canonical graph. The
CLI uses the built-in registry snapshot; the Python API can receive an explicit
validated registry and fresh solver limits.

`replay_certificate()` performs the checks in a deterministic order:

1. revalidate exact certificate, graph, registry, and limit values;
2. compare graph digest, registry digest, and registry version;
3. rebuild and compare the complete source-labelled constraint catalog;
4. require exact contract coverage for all graph values;
5. apply the optional strict verifier/solver version policy;
6. replay every claimed contract through independent Python semantics;
7. run one fresh bounded verification using the replay caller's limits;
8. compare the fresh status, bindings, and exact contracts with the claim.

Early binding, catalog, coverage, witness, and strict-toolchain mismatches stop
before the fresh solver run. The report itself is canonical and
content-addressed under:

```text
unitsentinel.certificate-replay/v1
```

## Replay outcomes

| Status | Meaning |
| --- | --- |
| `reproduced` | Pure witness replay and fresh unique verification agree with the claim |
| `mismatch` | A binding, catalog, coverage, witness, toolchain, or fresh semantic result disagrees |
| `indeterminate` | The fresh verifier returned `unknown`; replay refuses to convert it into a mismatch or success |

Mismatch reports contain one stable first reason. Public reasons distinguish
graph/registry identity, catalog, coverage, witness, toolchain, fresh conflict,
fresh underconstraint, and fresh contract disagreement. Raw solver diagnostics
and host paths do not enter the report.

## CLI reproduction

The committed positive claim can be pinned and replayed without network access:

```bash
.venv/bin/python -m unitsentinel replay \
  docs/evidence/claims/wheel-anomaly.cert.json \
  --graph docs/evidence/contracts/wheel-anomaly-verified.json \
  --expect-sha256 \
  e93cc87cd72c6ede9cf8d324bfb41b2eb2bdcea6cb0aa6fea7aed4696009ab1a \
  --strict-toolchain
```

Argparse validates the syntax of `--expect-sha256` before either input is
opened. The CLI then bounded-reads and decodes the certificate, compares its
actual content digest with the expected value, and rejects a mismatch before it
opens the graph or performs solver work.

The exact example certificate, replay JSON, terminal transcript, lineage
diagram, and cross-bound digests are indexed in the
[evidence ledger](evidence/README.md).
