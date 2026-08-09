# Closed-subset ONNX metadata contract v1

Status: implemented in UnitSentinel 0.1.0.

This document defines the only ONNX byte envelope accepted by the current
adapter. It is intentionally narrower than ONNX as a format. Unknown versions,
operators, types, metadata, or graph features are rejected instead of inferred.

## Purpose and non-authority

ONNX supplies tensor and graph structure, while this metadata document supplies
the explicit mapping into UnitSentinel's canonical graph and unit registry.
The adapter does not infer units from tensor or node names.

An import receipt is content-addressed but unsigned. It proves which bytes,
metadata, checker configuration, operator mappings, and canonical graph were
used by this implementation. It does not authenticate an exporter, attest a
deployment, prove model quality or scientific correctness, or establish that
the imported graph was later verified.

## Source envelope

| Property | Accepted value |
| --- | --- |
| Source | One path-backed regular `ModelProto`, at most 8,388,608 bytes |
| Parser/checker package | Exactly `onnx==1.22.0` |
| IR version | Exactly 8 |
| Opset imports | Exactly one default-domain import at version 13 |
| Graph form | One main graph; no functions or training graphs |
| Tensor data | No initializers, sparse initializers, or external data |
| Quantization | No quantization annotations |
| Node domain | Default domain only |
| Attributes | None |
| Node outputs | Exactly one |
| Dimensions | Static positive integers only |
| Maximum rank | 8 |
| Element types | float16, float32, float64, bfloat16 |

Before lowering, the adapter invokes `onnx.checker.check_model` with
`full_check=True`, compatibility checking enabled, and custom-domain checking
enabled. Passing that checker is necessary but not sufficient. UnitSentinel also
applies the source-envelope and contract restrictions below; preflight checks
that make the official call safe may reject earlier.

The adapter uses the ONNX package for parsing and checking. It does not create
an ONNX Runtime session, execute nodes, load external tensor payloads, call a
URL, or import model-provided code.

## Metadata location and bytes

The model must contain exactly one metadata property with this key:

```text
io.github.omar07ibrahim.unitsentinel.contract
```

Its UTF-8 value must be canonical JSON under the same duplicate-key,
non-finite-number, depth, item, integer, and string bounds used by the adapter.
The encoded document may not exceed 131,072 bytes. The root schema identifier
is:

```text
unitsentinel.onnx-contract/v1
```

Canonical JSON means the compact sorted-key representation accepted by
UnitSentinel's strict JSON boundary. The formatted example below is for
readability; the metadata value itself is compact canonical JSON.

```json
{
  "graph_id": "onnx-speed-contract",
  "nodes": [
    {
      "node_id": "derive-speed",
      "onnx_name": "derive-speed"
    }
  ],
  "schema": "unitsentinel.onnx-contract/v1",
  "values": [
    {
      "onnx_name": "distance",
      "unit_id": "meter",
      "value_id": "distance"
    },
    {
      "onnx_name": "duration",
      "unit_id": "second",
      "value_id": "duration"
    },
    {
      "onnx_name": "speed",
      "unit_id": "meter-per-second",
      "value_id": "speed"
    }
  ]
}
```

## Closed contract fields

The root has exactly four fields:

| Field | Meaning |
| --- | --- |
| `schema` | Exact contract schema above |
| `graph_id` | Canonical UnitSentinel graph identifier |
| `values` | Complete ONNX-value to canonical-value/unit bindings |
| `nodes` | Complete named-ONNX-node to canonical-node bindings |

A value binding has exactly `onnx_name`, `value_id`, and `unit_id`.
`unit_id` is either a canonical identifier in the built-in immutable registry
or JSON `null` for an intentionally unannotated value. A node binding has
exactly `onnx_name` and `node_id`.

The `values` and `nodes` arrays are sorted by `onnx_name` and contain no
duplicate source names. Canonical value IDs are unique; canonical node IDs are
unique; and node IDs may not collide with value IDs. Every graph input,
intermediate value, output, and named node is bound exactly once. Extra,
missing, alias, partial, or noncanonical bindings fail closed.

Source names are bounded to 128 characters and use the reviewed portable name
grammar. Canonical IDs follow the ordinary graph-format rules.

## Reviewed lowering table

| ONNX operator | Arity | Canonical UnitSentinel operation |
| --- | ---: | --- |
| `Add` | 2 | `add` |
| `Div` | 2 | `divide` |
| `Exp` | 1 | `exp` |
| `Identity` | 1 | `identity` |
| `Log` | 1 | `log` |
| `MatMul` | 2 | `matmul` |
| `Max` | 2 | `maximum` |
| `Min` | 2 | `minimum` |
| `Mul` | 2 | `multiply` |
| `Sigmoid` | 1 | `sigmoid` |
| `Softmax` | 1 | `softmax` |
| `Sub` | 2 | `subtract` |

Inputs must reference graph inputs or earlier node outputs, every non-input
value has one producer, and all graph outputs must be produced. The lowered
graph then passes through the existing immutable `ComputationGraph`
constructor, so disconnected/dead nodes, invalid topology, bad identifiers, or
unsupported shape metadata cannot be smuggled past the core.

This adapter preserves static shape and scalar-type metadata. UnitSentinel does
not claim to validate ONNX broadcasting rules or matrix-shape compatibility;
those remain outside the dimensional verifier.

## Import receipt v1

A successful import produces a canonical record with schema
`unitsentinel.onnx-import/v1`. Its digest binds:

- UnitSentinel name and version;
- official checker name, exact ONNX version, and checker options;
- source size and SHA-256;
- IR and opset values;
- `model_executed: false` and `external_data: false`;
- metadata key, schema, and SHA-256;
- every ONNX-node to canonical-operation/node mapping; and
- canonical graph schema, ID, counts, and SHA-256.

The CLI JSON wrapper uses `unitsentinel.cli.import-onnx/v1` and also records
whether graph publication completed. The receipt is not a proof certificate.
Run `unitsentinel verify` on the published graph to establish a current
dimensional result.

## CLI filesystem transaction

```bash
unitsentinel import-onnx MODEL.onnx --graph NEW.graph.json
```

The command:

1. refuses stdin, symlink leaves, FIFOs, non-regular descriptors, and inputs
   that exceed the byte limit or grow beyond it during the bounded read;
2. validates and lowers fully before opening the final output transaction;
3. requires a fresh output path and refuses overwrite;
4. writes a private temporary file, checks durability, and atomically publishes
   the canonical graph; and
5. emits a success receipt only after graph publication.

The text and `--json` modes publish identical canonical graph bytes. Stable
domain rejection exits do not publish a partial graph.

## Reproducible example and limits

The committed [synthetic model](evidence/models/speed-contract.onnx) is 593
bytes and is generated by official `onnx.helper` APIs. Its
[actual import capture](evidence/captures/onnx-import.txt),
[lowered graph](evidence/contracts/onnx-speed.graph.json),
[verification capture](evidence/captures/onnx-verify.txt),
[rejection records](evidence/captures/onnx-rejections.txt), and
[provenance](evidence/onnx-provenance.json) regenerate offline after the pinned
dependencies are installed.

The positive example contains one `Div` node; it is not evidence that arbitrary
ONNX exports are supported. The recorded symbolic-shape, `Pow`, and initializer
failures are representative boundary cases, not an exhaustive compatibility
matrix. Network access is not required by import; the provenance does not claim
that its CI process observed host network traffic.

The repository intentionally has no license yet. Licensing remains Omar's
decision before third-party reuse is invited.
