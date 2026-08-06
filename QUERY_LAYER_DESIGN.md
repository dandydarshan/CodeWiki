# Design: CodeWiki Query Layer

**Status:** Proposed
**Author:** Darshan M U
**Date:** 2026-08-04

---

## 1. Summary

CodeWiki currently *generates* documentation but provides no way to *query* it. This
document proposes a query layer that answers developer questions from already-generated
output — module docs, `module_tree.json`, and the dependency graph — exposed to LLM
agents over MCP.

Target: answer a typical question in **~2.5k tokens and 1–2 round-trips**, versus
15–40k tokens and 3–8 round-trips for an agent doing grep-and-read against raw source.

The design is a three-tier ladder: **docs** answer *why*, the **graph** answers
*where* and *what connects*, and **live source** answers *what exactly*. Each tier is
consulted only when it is the right tool for the question shape.

> **Location note:** this file lives at the repository root, not in `docs/`.
> `docs/` is generated output — `codewiki/cli/commands/generate.py:91` globs and
> clears `*.md` there, so a design doc placed in `docs/` would be deleted on the next
> `generate --no-cache`.

---

## 2. Motivation

### 2.1 The problem

LLM coding agents explore codebases by repeated file reads and greps. Input tokens
dominate the cost of agentic coding, and structural questions — "who calls this?",
"what breaks if I change this?" — are exactly the ones text search answers worst,
because they require transitively following references, each hop costing another
round-trip.

CodeWiki already pays the expensive part of the bill: it parses the repository with
Tree-Sitter, clusters components into modules, and writes prose explaining the
architecture. That output is then used only for human reading and HTML rendering.

### 2.2 Measured baseline (this repository)

| Corpus | Measurement |
|---|---|
| Module docs | 27 files, 510,889 bytes (~128k tokens) |
| Source | 97 Python files, 23,674 lines, 893,496 bytes (~223k tokens) |
| Components in dependency graph | 438 (per `docs/metadata.json`) |
| Leaf nodes | 114 |
| Modules in `module_tree.json` | 23 (14 top-level), **67 component references** |

| Approach | Tokens/query | Round-trips |
|---|---|---|
| Dump all docs into context | ~128k | 1 |
| Grep/read agent (today's default) | ~15–40k | 3–8 |
| **This design (estimated)** | **~2.5k** | **1–2** |

The ~2.5k figure is a construction estimate, not a measurement. Phase 4 exists to
turn it into a real number, and to kill the project if the gain is under ~3×.

### 2.3 Are the docs actually usable as an answer source?

This was measured rather than assumed. Reproduction scripts are in Appendix A.

| Measurement | Result | Consequence for design |
|---|---|---|
| `module_tree` symbols mentioned in doc prose | **53/56 (95%)** | Docs genuinely cover indexed components |
| Home-module doc contains its own symbol | **51/53** | `module_tree.json` is a reliable component→doc map |
| File paths mentioned in prose | **17/42 (40%)** | Docs discuss *classes*, not files — path lookup must come from the graph |
| H2/H3 sections across 27 docs | **1,130** | Retrieval unit is the section, never the file |
| Median section size | **328 bytes (~82 tokens)** | A 5-section answer costs ~400 tokens |
| Section size p90 / max | 925 / 5,604 bytes | Long tail exists; cap section returns by budget |
| Unique headings appearing exactly once | **578/712 (81%)** | Headings are strong routing signal |
| Symbol text-match spread across docs | median **4**, p90 **17**, max **24** | **Do not route by grepping symbol names** |

Two conclusions follow directly:

1. **Route via `module_tree.json`, not via text matching.** Because docs cross-reference
   each other heavily (`Related Modules`, `Integration Points`), a symbol name appears
   in a median of 4 documents. `module_tree.json` gives the authoritative mapping and
   is verified correct for 51 of 53 symbols. Text search is reserved for natural-language
   queries that name no symbol.
2. **Heading noise is small and concentrated.** Only ~12 headings repeat meaningfully
   (`Overview` ×22, `Integration Points` ×18, `Performance Characteristics` ×15,
   `Core Components` ×15). A 12-entry stoplist is sufficient; the remaining 81% of
   headings are discriminative.

---

## 3. Prior art

Vogel, Meyer-Eschenbach, Kohler, Grünewald, Balzer. *Codebase-Memory: Tree-Sitter-Based
Knowledge Graphs for LLM Code Exploration via MCP*. arXiv:2603.27277, March 2026.
Source: `github.com/DeusData/codebase-memory-mcp` (MIT, v0.5.5).

A pure structural system — Tree-Sitter → typed knowledge graph → SQLite → 14 MCP tools,
no prose and no LLM in the index path. Reported: 83% answer quality versus 92% for a
file-exploration agent, at ~10× fewer tokens (~1k vs ~10k per query), 2.1× fewer tool
calls, sub-millisecond query latency. Scales to 2.1M nodes (Linux kernel) in ~3 minutes.

### What we adopt

- Treating code structure as a first-class queryable graph rather than text to search.
- Typed edges as the basis for filtered traversal.
- A typed MCP tool surface, with the agent selecting tools rather than a server-side
  query classifier.
- Their tool taxonomy (Indexing / Query / Analysis / Code) maps almost 1:1 onto
  Section 6 below.

### What we do not adopt

| Their choice | Why not here |
|---|---|
| 66 vendored Tree-Sitter grammars in a C binary | CodeWiki hand-writes one analyzer per language; 9 exist. Expanding to 66 is a multi-person-year effort. |
| SQLite with recursive CTEs | Unnecessary at 438 nodes. Revisit near ~10k (Section 9). |
| Single statically-linked binary, 8-layer CI audit, VirusTotal gating | That hardening exists because they ship an opaque binary. A PyPI package has a different trust model. |
| LSP-style hybrid type resolution (Go/C/C++) | Disproportionate cost for the accuracy gain here. |
| Louvain community detection | CodeWiki's LLM clustering plus `overview.md` is strictly more readable. |

### Where CodeWiki can exceed it

The paper's §5.1 concedes that its graph agent loses on "queries requiring full source
context or exhaustive pattern matching" (the file explorer wins in 16 of 31 languages),
and attributes the 83-vs-92 quality gap to the graph storing relationships but not
source-level meaning. Their stated conclusion:

> "the optimal architecture is a hybrid: graph-based retrieval for structural queries,
> with fallback to file exploration for source-level tasks."

CodeWiki already has the missing semantic layer — 510 KB of LLM-written prose. The
hybrid the authors name as future work is the system this document specifies. That is
a hypothesis, not a result: the prose tier may close their quality deficit, or may
simply add staleness risk. Phase 4 tests it.

**Caveats on their numbers:** single LLM backend, responses graded by the first author,
one repository per language. The authors flag all three as validity threats. Treat
"10× fewer tokens" as directional and "83% quality" as soft.

---

## 4. Architecture

```
   codewiki generate   (UNCHANGED)
   parse → cluster → LLM docs
        │
        ├── docs/*.md · module_tree.json · metadata.json
        └── temp/*_dependency_graph.json
        │
        ▼
┌──────────────────────────────────────────────────────────┐
│  NEW   codewiki index        offline · no LLM · seconds  │
│                                                           │
│   graph slimmer      doc sectioner      symbol + BM25     │
│   drop source_code   split on H2/H3     module_tree first │
│   keep file+span     record byte range  path-prefix       │
│   invert edges       extract xrefs      fallback          │
└──────────────────────────────────────────────────────────┘
        │
        ▼   docs/.memory/
        graph.json · sections.jsonl · symbols.json
        postings.json · manifest.json
        │
        ▼
┌──────────────────────────────────────────────────────────┐
│  NEW   memory_server.py     MCP · read-only · stateless  │
│                                                           │
│   conceptual  → doc sections                              │
│   locational  → symbols.json → card + doc anchor          │
│   structural  → degree-capped graph walk                  │
│                        ↓                                  │
│              hydrate spans from working tree              │
└──────────────────────────────────────────────────────────┘
```

### 4.1 Design principles

1. **Generation is untouched.** The query layer is a pure consumer. No change to
   clustering, prompting, or doc writing.
2. **The agent routes, not the server.** The consumer is an LLM that understands intent
   better than any regex over "who calls" / "what breaks". Expose typed tools; skip the
   classifier.
3. **Prose is cached, code is live.** Source spans are always read from the working
   tree at query time, never from the index. Doc prose is labelled with the commit it
   describes. This is the primary defence against stale answers.
4. **Every traversal is budgeted.** Unbounded graph walks are worse than grep
   (Section 5.3).
5. **Return small, spill rarely.** Inverts the existing `SessionWorkspace` convention
   (`codewiki/mcp/workspace.py:56`), which writes large payloads to disk for the agent
   to read. A *query* result that forces a file read defeats its own purpose.

---

## 5. Phase 0 — Typed edges

**This phase blocks every other phase.** It is the only change to existing analysis code.

### 5.1 Problem

`codewiki/src/be/dependency_analyzer/ast_parser.py:135` collapses every relationship —
call, import, inheritance, type reference — into one undifferentiated set:

```python
self.components[caller_component_id].depends_on.add(callee_component_id)
```

`CallRelationship` (`models/core.py:50`) carries `call_line`, and it is **discarded at
this line**. The result is a graph with untyped, forward-only edges and no call sites.

This breaks filtered traversal. Call-path tracing cannot be separated from the import
closure, and — critically — edge typing is what makes the degree cap in Section 5.3
work, because it allows expanding `CALLS` while refusing to expand `IMPORTS`.

The data is already extracted at parse time and thrown away. This is a plumbing fix,
not new analysis.

### 5.2 Blast radius

Verified small. `depends_on` has exactly two real consumers:

| Site | Role |
|---|---|
| `topo_sort.py:268` (`build_graph_from_components`) | Graph construction for leaf detection |
| `ast_parser.py:162-163` | Set→list conversion during serialization |

Relationships already flow analyzer → `call_graph_analyzer.py:125` (`model_dump()`) →
`ast_parser.py:120`. Because that is a pydantic dump, **a new model field propagates
automatically** with no plumbing change.

### 5.3 Changes

**0.1 — Extend the model** (`models/core.py:50`). Additive; the default preserves
current behaviour exactly:

```python
class CallRelationship(BaseModel):
    caller: str
    callee: str
    call_line: Optional[int] = None
    is_resolved: bool = False
    edge_type: str = "calls"   # NEW
```

**0.2 — Tag construction sites.** 41 sites across 9 analyzers:

| Analyzer | Sites | Analyzer | Sites |
|---|---|---|---|
| `cpp.py` | 8 | `csharp.py` | 4 |
| `kotlin.py` | 7 | `javascript.py` | 4 |
| `php.py` | 7 | `c.py` | 2 |
| `java.py` | 6 | `python.py` | 2 |
| | | `typescript.py` | 1 |

Edge kinds reliably derivable across all 9 today:

| Type | Source |
|---|---|
| `CALLS` | Existing call-site extraction (current default) |
| `IMPORTS` | `python.py:128-148` `module_imports`/`from_imports`; equivalents per language |
| `INHERITS` | `Node.base_classes`, already on the model |
| `CONTAINS` | Derivable from `Node.class_name` |

`IMPLEMENTS` and `USES_TYPE` are per-language and partial (Java/C#/Kotlin interfaces).
Capture where cheap; **do not block the phase on them**.

**0.3 — Stop the collapse** (`ast_parser.py:120-137`). Continue writing `depends_on`
for backward compatibility *and* accumulate a typed edge list:

```python
edges.append({
    "src": caller_component_id,
    "dst": callee_component_id,
    "type": rel_dict.get("edge_type", "calls"),
    "line": rel_dict.get("call_line"),
})
```

`topo_sort.py`, leaf selection, and clustering continue to work unmodified.

**0.4 — Serialize** both `depends_on` and `edges` in `save_dependency_graph`
(`ast_parser.py:158`).

### 5.4 Acceptance criteria

- All existing tests pass unmodified (`tests/test_gitignore_filtering.py`,
  `tests/test_incremental_update.py`, `tests/smoke_test_mcp.py`).
- A new fixture repository per language asserts, for at least one component each:
  correct `edge_type`, and a non-null `call_line` on `CALLS` edges.
- Re-running `codewiki generate` on this repository produces an unchanged
  `module_tree.json` — proving the change is behaviour-preserving upstream.

**Estimate: 2–3 days.**

---

## 6. Phase 1 — Durable index

### 6.1 Promote the graph out of scratch

The dependency graph is currently written under `output_dir/temp/`
(`codewiki/src/config.py:214`, `:219`) and treated as disposable —
`docs/temp/dependency_graphs/` is empty in the committed output. Query-time use
requires a durable, versioned artifact.

New location: `docs/.memory/`. **Add `docs/.memory/` to `.gitignore`**, alongside the
existing `docs/.cache/` entry.

### 6.2 Artifacts

```
docs/.memory/
  manifest.json    schema_version · commit · built_at · counts · source_fingerprint
  graph.json       nodes[]: id, name, type, file, start_line, end_line, lang,
                            module, degree_in, degree_out
                   edges[]: src, dst, type, line          (forward AND reverse)
  sections.jsonl   id, doc, heading_path, byte_start, byte_end, symbols[], links[]
  symbols.json     name → [{component_id, home_module, doc_anchor}]
  postings.json    term → [(section_id, tf)], doclen, idf
```

### 6.3 Builders

**Graph slimmer.** Drop `source_code` (retain `file` + `start_line`/`end_line` for live
hydration), build the reverse edge index, precompute in/out degree for the hub cap.
Dropping source is what makes the index small enough to load eagerly on every query.

**Doc sectioner.** Split the 27 docs on H2/H3 into the measured 1,130 sections, recording
byte ranges for O(1) slicing. Extract `Related Modules` / `Integration Points` targets
as a document cross-reference graph — a free secondary routing signal, since the docs
already hyperlink each other.

**Symbol + BM25 index.** Component→module resolution in priority order:

1. `module_tree.json` exact membership — authoritative, covers 67 components.
2. Longest path-prefix match against each module's `path` field
   (`"cli_core": {"path": "codewiki/cli"}`) — recovers most of the remaining ~371
   components by directory.
3. Unresolved — the node is returned with structure and no prose.

BM25 postings are built over section text with the 12 boilerplate headings stoplisted.

### 6.4 Incremental refresh

Reuse `incremental.detect_changed_files` (`codewiki/src/be/incremental.py:158`) —
git-based with mtime fallback. Re-parse only changed files; recompute postings only for
sections in affected docs.

**Blocker to fix:** `docs/metadata.json` currently has `"commit_id": null`, so staleness
is undetectable on the present snapshot. The generation path must stamp it. The MCP
close-session handler already does this (`codewiki/mcp/server.py:567`
`_write_generation_metadata`); the CLI path needs the equivalent.

### 6.5 Acceptance criteria

- `codewiki index` on this repository produces all five artifacts.
- `graph.json` is at least 5× smaller than the raw dependency graph (source removed).
- Reverse-edge lookup for a known component returns its known callers.
- Touching one source file and re-indexing rebuilds only affected entries.

**Estimate: 3–4 days.**

---

## 7. Phase 2 — Query engine

Module: `codewiki/memory/query.py`. Pure library, no MCP dependency, independently
testable.

### 7.1 Three retrieval paths

| Question shape | Path | Est. cost |
|---|---|---|
| Conceptual — *"how does clustering work"* | BM25 → 3–5 sections | ~400 tok |
| Locational — *"where is X implemented"* | `symbols.json` exact → card + doc anchor | ~200 tok |
| Structural — *"who calls X"*, *"what breaks"* | Degree-capped graph walk; docs annotate only | ~1.5k tok |

Structural queries **skip the doc tier entirely** — that is where docs are weakest
(40% file-path coverage) and the graph strongest.

### 7.2 Guard: the degree cap

Dependency graphs are power-law. In this repository `Config` and `Node` are depended on
by nearly everything; an uncapped `used_by` walk at depth 2 returns most of the 438
nodes — strictly worse than grep, because traversal is paid *and* everything is still
dumped.

Rule: when a node's `degree_in` exceeds a threshold, do not expand it. Return a summary
instead:

```
hub: Config — 87 dependents across cli_core(12), llm_backends(9), … 
```

This is only possible because Phase 0 typed the edges: `CALLS` can be expanded while
`IMPORTS` is refused.

### 7.3 Guard: live hydration

Source spans are read from the working tree using `file` + `start_line`/`end_line`,
never from the index. Doc sections are returned annotated `as of <commit>`. When the
index is stale, the code is still correct and the prose is explicitly dated.

### 7.4 Ranking

Seeds for a structural walk are the union of components named in matched doc sections
and components matching the query lexically. Doc-mentioned components are ranked higher:
the clustering step already encoded which components matter architecturally, and that
judgement is signal a raw graph does not carry.

Rank by hop distance, then in-matched-module, then degree centrality.

### 7.5 Acceptance criteria

- Ten hand-written questions across the three shapes return correct sections/nodes.
- Every traversal respects its node budget.
- A hub query returns a summary, not a closure.
- Editing a source file changes the hydrated span without re-indexing.

**Estimate: 3–4 days.**

---

## 8. Phase 3 — MCP query server

New entry point `codewiki/mcp/memory_server.py`. **Read-only and stateless**, deliberately
separate from the existing generation server (`codewiki/mcp/server.py`), which is
session-based and mutates output. Different lifecycle, different process.

### 8.1 Tool surface

| Category | Tool | Description |
|---|---|---|
| Lifecycle | `memory_status` | Index present? built at which commit? stale vs HEAD? counts |
| | `build_index` | Build or incrementally refresh `docs/.memory/` |
| | `module_map` | Module tree summary with doc filenames — orientation |
| Query | `search_docs(query, k)` | BM25 over sections → text + anchors |
| | `find_symbol(name, kind?, lang?)` | Exact/fuzzy → component cards + doc anchor |
| | `get_doc_section(module, heading?)` | Precise slice via byte offsets |
| | `get_component(id, include_source?)` | Node card + optionally a live span |
| Structural | `trace_dependencies(id, direction, edge_types[], depth, budget)` | Degree-capped walk |
| | `impact_of(files \| component_ids)` | Changed files → affected modules → docs to reread |

`impact_of` is nearly free: `incremental.find_affected_modules`
(`codewiki/src/be/incremental.py:111`) already implements exactly this and is currently
used only to decide what to regenerate.

### 8.2 Response conventions

- Small results inline; spill to disk only when a traversal exceeds budget.
- Every doc-derived payload carries `as_of_commit`.
- Every truncated result carries an explicit `truncated: true` and the applied budget.

### 8.3 Registration

```json
{
  "mcpServers": {
    "codewiki-memory": {
      "command": "python",
      "args": ["-m", "codewiki.mcp.memory_server"]
    }
  }
}
```

### 8.4 Acceptance criteria

- All nine tools callable via MCP stdio; extend `tests/smoke_test_mcp.py`.
- Server starts with no LLM configuration present.
- Missing index yields an actionable error naming `build_index`, not a traceback.

**Estimate: 3–4 days.**

---

## 9. Phase 4 — Validation

The ~2.5k token estimate is a construction estimate. This phase makes it a measurement.

**Method.** Borrow the paper's benchmark shape: ~12 question categories spanning hub
detection, caller ranking, dependency chains, impact analysis, and code retrieval. Run
each against this repository twice — once through the query layer, once with a
grep-and-read agent. Record tokens consumed, tool calls, and wall-clock.

**Grading.** Blind, against manually verified reference answers. The paper's own results
were graded by its first author, which the authors flag as an internal-validity threat;
this evaluation should not repeat that.

**Kill criterion.** If the token reduction versus the grep baseline is under ~3×, or
answer quality drops more than ~10 points, stop and reconsider. A slower grep with extra
staleness risk is a worse product than grep.

**Estimate: 2–3 days.**

---

## 10. Sequencing

```
Phase 0 ──► Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4
 2-3d        3-4d        3-4d        3-4d        2-3d
                                                       ≈ 2.5–3.5 weeks solo
```

Strictly sequential — each phase consumes the previous phase's output.

**Recommended de-risking spike (~4 days):** Phase 0 plus a minimal `trace_dependencies`.
This proves the typed-edge migration holds across all 9 analyzers and that degree-capped
traversal returns useful results. If the spike lands, the remainder is mostly mechanical.

---

## 11. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| ~85% of components have no doc (67 of 438 indexed) | Medium | Path-prefix fallback; graph-only answers remain valid, just less narrated |
| Stale docs produce confident wrong answers | **High** | Live source hydration; explicit `as_of_commit` labelling |
| `metadata.json.commit_id` is `null` today | Medium | Fix generation to stamp it (Phase 1); staleness is otherwise undetectable |
| Hub blowup makes traversal worse than grep | **High** | Degree cap + edge-type filtering (Phase 0 is the enabler) |
| Misroute costs an extra round-trip | Medium | Typed tools with agent-side routing instead of a brittle classifier |
| Efficiency claim unproven | **High** | Phase 4 measures it; explicit kill criterion |
| Typed-edge migration regresses clustering | Low | Additive field with default; acceptance criterion requires identical `module_tree.json` |

---

## 12. Deferred

| Item | Trigger to revisit |
|---|---|
| SQLite backing store | ~10k components; JSON load is fine at 438 |
| Embedding/vector retrieval | If BM25 recall proves inadequate in Phase 4 |
| Additional language analyzers | Independent of this work |
| Multi-repository indexing | After single-repo is validated |
| LSP-style type resolution | Only if call resolution accuracy blocks real queries |
| Runtime/dynamic-dispatch edges | Out of scope; the graph is static-only by construction |

---

## Appendix A — Reproducing the measurements

Run from the repository root with generated output present in `docs/`.

**Doc-to-code grounding** (Section 2.3, rows 1–3):

```python
import json, glob, os
tree = json.load(open('docs/module_tree.json'))
comps = []
def walk(t):
    for k, v in t.items():
        for c in v.get('components', []):
            comps.append((k, c))
        walk(v.get('children', {}) or {})
walk(tree)
names = {c.split('::', 1)[1] for _, c in comps if '::' in c}
paths = {c.split('::', 1)[0] for _, c in comps if '::' in c}
alltext = '\n'.join(open(p, encoding='utf-8').read() for p in glob.glob('docs/*.md'))
print(f"symbols: {sum(n in alltext for n in names)}/{len(names)}")
print(f"paths:   {sum(p in alltext for p in paths)}/{len(paths)}")
```

**Section granularity and heading noise** (rows 4–7):

```python
import glob, re, statistics, collections
sizes, headings = [], collections.Counter()
for p in glob.glob('docs/*.md'):
    lines = open(p, encoding='utf-8').read().split('\n')
    idx = [(i, l) for i, l in enumerate(lines) if re.match(r'^#{2,3} ', l)]
    for j, (i, l) in enumerate(idx):
        end = idx[j + 1][0] if j + 1 < len(idx) else len(lines)
        sizes.append(len('\n'.join(lines[i:end])))
        headings[l.strip('# ').strip()] += 1
print(f"sections={len(sizes)} median={statistics.median(sizes)}")
print(f"unique-once={sum(1 for _, c in headings.items() if c == 1)}/{len(headings)}")
print(headings.most_common(12))
```

**Edge construction sites** (Section 5.3):

```bash
for f in codewiki/src/be/dependency_analyzer/analyzers/*.py; do
  echo "$(basename $f): $(grep -c 'CallRelationship(' $f)"
done
```

---

## Appendix B — Key file references

| Path | Relevance |
|---|---|
| `codewiki/src/be/dependency_analyzer/ast_parser.py:135` | The untyped edge collapse — Phase 0's target |
| `codewiki/src/be/dependency_analyzer/ast_parser.py:158` | `save_dependency_graph` serialization |
| `codewiki/src/be/dependency_analyzer/models/core.py:7` | `Node` model |
| `codewiki/src/be/dependency_analyzer/models/core.py:50` | `CallRelationship` model |
| `codewiki/src/be/dependency_analyzer/topo_sort.py:244` | `build_graph_from_components` — `depends_on` consumer |
| `codewiki/src/be/dependency_analyzer/analysis/call_graph_analyzer.py:125` | `model_dump()` — carries new fields for free |
| `codewiki/src/be/incremental.py:111` | `find_affected_modules` — reused by `impact_of` |
| `codewiki/src/be/incremental.py:158` | `detect_changed_files` — reused by incremental indexing |
| `codewiki/src/config.py:214` | `temp/` output base — graph currently written here |
| `codewiki/mcp/server.py` | Existing generation MCP server (separate lifecycle) |
| `codewiki/mcp/server.py:567` | `_write_generation_metadata` — commit stamping reference |
| `codewiki/mcp/workspace.py:56` | `SessionWorkspace` — spill convention being inverted |
| `codewiki/cli/commands/generate.py:91` | Clears `*.md` in output dir — why this doc is not in `docs/` |
