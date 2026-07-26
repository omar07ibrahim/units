# Canonical graph format

The v1 graph document is a closed, content-addressed contract. It describes
tensor metadata and computation topology; successfully decoding it does **not**
mean that the graph is dimensionally consistent. That decision belongs to the
verifier.

## Byte boundary

`decode_graph()` accepts exact `bytes`, not text or a Python dictionary. Before
constructing a JSON tree it enforces:

- a 1 MiB document limit;
- valid UTF-8 without a BOM;
- at most eight nested containers;
- at most 1,024 items in one object or array;
- a 32,768-token structural budget.

The preflight scanner understands quoted strings and escapes, so brackets and
commas inside a JSON string do not affect structural counts. A second validation
pass repeats the limits on the parsed tree.

The remaining byte rules are deliberately strict:

- duplicate object keys are rejected;
- `NaN`, infinities, and all floating-point JSON numbers are rejected;
- integer tokens are limited to ten digits before conversion;
- keys are sorted and separators are compact;
- Unicode is emitted directly as UTF-8 rather than optional `\u` escapes;
- no leading/trailing whitespace, BOM, or trailing newline is accepted;
- re-encoding the semantic graph must reproduce every input byte.

Exact rational power exponents use reduced strings such as `"2"` or `"1/2"`.
Physical values are never smuggled through an imprecise JSON float.

## Root schema

Every document has exactly six fields. This is the real speed-contract example
formatted for readability (the generator emits its compact canonical bytes):

```json
{
  "graph_id": "speed-contract",
  "inputs": ["raw-speed"],
  "nodes": [
    {
      "attributes": {"unit_id": "meter-per-second"},
      "inputs": ["raw-speed"],
      "node_id": "normalize-speed",
      "operation": "convert",
      "output": "si-speed"
    }
  ],
  "outputs": ["si-speed"],
  "schema": "unitsentinel.graph/v1",
  "values": [
    {
      "dtype": "float32",
      "shape": ["batch"],
      "unit_id": "kilometer-per-hour",
      "value_id": "raw-speed"
    },
    {
      "dtype": "float32",
      "shape": ["batch"],
      "unit_id": "meter-per-second",
      "value_id": "si-speed"
    }
  ]
}
```

The executable generator in `examples/build_speed_contract.py` is the
authoritative byte representation.

### Values

Each entry has exactly:

| Field | Contract |
| --- | --- |
| `value_id` | canonical lowercase ASCII identifier |
| `dtype` | `float16`, `bfloat16`, `float32`, or `float64` |
| `shape` | zero to eight positive integer or canonical symbolic axes |
| `unit_id` | canonical registry identifier or `null` |

The values array is sorted by `value_id`. This removes one irrelevant source of
digest variation while preserving meaningful public input/output ordering.

### Nodes

Each node has one canonical `node_id`, one output, an ordered input array, and a
closed attributes object:

| Operation family | Arity | Attributes |
| --- | ---: | --- |
| `identity`, `exp`, `log`, `sigmoid`, `softmax` | 1 | none |
| `add`, `subtract`, `minimum`, `maximum` | 2 | none |
| `multiply`, `divide`, `matmul` | 2 | none |
| `power` | 1 | exact rational `exponent` |
| `convert` | 1 | canonical target `unit_id` |

Unknown operations or attributes are rejected. There is no URL, import path,
callback, executable expression, or extension hook.

## Topology invariants

The immutable graph constructor revalidates all nested values and requires:

- at least one input, value, and output;
- unique value, node, input, and output identifiers;
- node identifiers disjoint from value identifiers;
- every node input already available earlier in topological order;
- exactly one producer for every non-input value;
- every declared output to exist;
- every value and node to contribute to at least one public output.

Those rules reject cycles, forward references, overwritten inputs, unproduced
declarations, and dead subgraphs without executing graph code.

Unit annotations are checked separately against one exact registry snapshot.
Aliases are useful at an ingestion boundary, but canonical graph contracts must
name the registry's canonical unit identifiers.

## Reproducible example

After the local setup from the README:

```bash
mkdir -p .unitsentinel
.venv/bin/python -I examples/build_speed_contract.py \
  > .unitsentinel/speed-contract.json
sha256sum .unitsentinel/speed-contract.json
```

Expected SHA-256:

```text
aecbeff2ce89cfd7b2aab6a0414ec307a5061577f4b8b0d1c53298f896569546
```

`tests/test_examples.py` runs the generator in an isolated interpreter,
requires empty stderr and no trailing newline, decodes the exact stdout bytes,
and pins the same graph digest.
