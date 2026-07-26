# Verified unit-annotation repair v1

The repair boundary can propose one mechanical annotation replacement. It does
not edit a graph, choose a physical model, infer scientific intent, or claim
that a computation is scientifically appropriate.

## Acceptance protocol

For one immutable graph and one content-addressed registry snapshot, the
implementation:

1. freshly verifies the source graph and requires a conflict with a minimal
   public core;
2. considers only exact `declaration/<value>/unit` witnesses for values that
   already have an explicit canonical unit annotation;
3. creates an in-memory graph with exactly that annotation removed;
4. requires the relaxed graph to be `verified`, meaning every value's
   dimension, quantity kind, scale, and offset is unique and replayed by the
   verifier;
5. enumerates canonical registry entries whose dimension, kind, scale, and
   offset exactly equal the relaxed value contract;
6. creates each one-annotation replacement in memory and freshly verifies it
   until either the bounded set is exhausted or a second verified candidate
   has already proved ambiguity;
7. returns a proposal only when exactly one candidate is verified.

Aliases, symbols, dimensional similarity, approximate scales, and names are
not matching evidence. The previous annotation is excluded because retaining
it would not be a replacement.

The proposal contains the relaxed and repaired graphs plus the corresponding
verification results. Their content digests form an auditable
conflict-to-relaxed-to-verified lineage. The caller must explicitly decide
whether to use the returned graph.

A reproducible production-CLI example is committed as
[canonical JSON](evidence/captures/repair.json) and an
[exact transcript](evidence/captures/repair.txt). Its
[source-derived lineage visual](assets/unit-repair-lineage.svg) and
[cross-bound provenance](evidence/repair-provenance.json) show the non-applied
wheel-anomaly annotation proposal without equating dimensional verification
with scientific intent.

## Fail-closed outcomes

`abstained` is a completed negative answer: for example, the source was already
verified, multiple verified replacements existed, no eligible declaration was
in the core, or one removed annotation left another conflict.

`indeterminate` means the bounded search could not establish a complete
answer. Unknown verifier outcomes, non-minimal cores, malformed verifier
responses, internal verifier exceptions, and exhausted site, candidate, work,
verifier-call, or wall-clock bounds use closed reason codes. Exception text is
never copied into a public result. Unexpected non-verifier failures use the
equally redacted `internal-failure` reason.

## Read-only CLI report

`unitsentinel repair GRAPH` runs the same bounded search and always writes one
`unitsentinel.cli.repair/v1` canonical JSON record to stdout when the search
returns a closed result. There is no apply or output-file option. The source
file is never rewritten, and the record states
`"application":"not-performed"`.

The envelope binds the source graph, registry, repair-result record, and their
content digests. Available source-verification evidence is included as a full
record. For `proposed`, `proposal` additionally contains the complete canonical
relaxed and repaired graph and verification-result records, their digests, and
the candidate digest. For `abstained` and `indeterminate`, `proposal` is
`null`. The proposed graph is data for a caller to inspect; emitting it is not
permission to install it.

Process exits distinguish all three result states:

| Repair status | Exit | Meaning |
| --- | ---: | --- |
| `proposed` | `0` | exactly one replacement was freshly verified |
| `abstained` | `6` | the completed search did not establish one unique repair |
| `indeterminate` | `3` | a bound or verifier outcome prevented a complete answer |

Input, usage, and internal boundary failures retain the shared CLI exits `4`,
`64`, and `70` and emit no JSON. Error details are redacted. The graph uses the
shared one-mebibyte input limit: it is opened without following the final
symlink, held by descriptor, checked as a regular file, and read with a second
stream-growth bound.

The CLI exposes every aggregate `RepairLimits` field:
`--max-sites` (`1..64`), `--max-candidates` (`1..512`),
`--max-verifier-calls` (`1..1024`), `--max-work-items` (`1..8192`), and
`--total-timeout-ms` (`1..60000`). Values must be canonical unsigned decimal
integers and are rejected before the graph is opened when malformed or out of
range.

## Aggregate bounds

`RepairLimits` independently bounds eligible sites, semantic candidates,
verifier calls, deterministic work items, and elapsed time. A work item is
reserved for each verifier call, graph materialization, and registry-entry
comparison, as well as each conflict witness inspected for site eligibility.
Every verifier call receives the same deterministic fair-share of the
aggregate deadline, capped by the caller's `SolverLimits`; the wall clock is
also checked between operations.

Source objects are exact-type validated before work begins. Every returned
model is frozen and content-addressed, and revalidation detects nested graph,
registry, verification-result, or lineage mutation.
