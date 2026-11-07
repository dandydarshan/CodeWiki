# Changelog

All notable changes to CodeWiki. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.1.0] - 2026-11-07

### Added

- **Ask AI.** `codewiki serve` serves the docs site with a chat window that
  answers questions from the docs and the current code, with citations to doc
  sections and source lines. The GitHub Pages viewer's chat finds a
  `codewiki serve` running for that repository on your machine (ports
  8765–8774, several repositories at once). The chat can copy, edit, retry
  and stop answers, shows progress, renders Mermaid, and can be minimised and
  resized. See [Ask AI](./guides/ask-ai.md).
- **`codewiki ask`** answers in the terminal, with `--session` follow-ups,
  `--json` output and a footer of calls, tokens, cache hits and cost.
- **MCP question tools** over generated docs: `codewiki_outline`,
  `codewiki_status`, `codewiki_search`, `codewiki_read` and `codewiki_impact`
  (no LLM), and `codewiki_ask`.
- **Concurrent module agents** (`--concurrency`, default 4; sub-modules share
  the slots), with live progress, an ETA and a run summary.
- **Context-window-aware prompts** (`--max-context-tokens`, or learned from an
  OpenAI-compatible provider's context-length error). Module, clustering and
  overview prompts are sized to the window, and agent history is trimmed to
  it, Gemini included.
- **Page summaries.** Each module page ends with a hidden
  `<!-- codewiki:summary ... -->` comment. Parent overviews fall back to these
  summaries, plus each child's outline, when the child pages do not fit.
- **Cost reporting.** Every LLM call's tokens and cost are recorded in
  `metadata.json`, priced with `codewiki config pricing set/show/remove`.
- `--main-model`, `--cluster-model` and `--fallback-model` on `generate`.
- `--min-token-per-module` (default 1,000): small groups merge into a nearby
  sibling instead of becoming a page of their own.
- `generate` says which branch and which files it documents, and asks first
  when that looks unintended (`-y` skips the question).
- Generated docs files are marked `linguist-generated`, so pull requests
  collapse them.
- A by-hand, real-model release check for Linux, Windows and macOS
  (`packaging/release_check/`).

### Changed

- **Nothing is dropped silently on large repositories.**
  - A module's prompt lists components only for its neighbourhood (parent,
    siblings, children, and the modules its code uses or is used by), instead
    of every component in the repository. On one user's repository that list
    alone was 2M of a 2.2M-character prompt.
  - A module too big for one prompt may split up to two levels past
    `--max-depth`. If it still does not fit, whole files are inlined while
    they fit and the rest are listed for the agent to read in full; files are
    no longer cut off.
- **`--no-cache` rebuilds from scratch.** It was accepted but did nothing, so
  runs resumed. The old docs stay recoverable: `git restore` if committed,
  else a `<docs>.prev-<commit>` copy. It cannot be combined with `--update`.
- **Overview pages are written in one bounded completion** from the child
  pages, validated (a heading and real prose, not a plan to read files) and
  corrected once if needed; the previous page is kept if it still fails.
- **Agents must write their page.** One that ends without writing it is
  reminded, then fails explicitly instead of leaving the page missing.
- **Mermaid validation never leaves your machine.** It runs in PythonMonkey
  on the main thread, else system Node.js, and is reported as unavailable
  otherwise; diagrams are never sent to mermaid.ink. Invalid flowchart labels
  are repaired before a page is saved.
- **Smaller, branch-reusable docs folder.** The dependency graph is no longer
  kept as a scratch copy (it was half of one user's 85 MB docs folder) and is
  rebuilt from the docs' commit when needed, so docs generated on one branch
  update on another. Several repositories can be served at once.
- `--update` backs up uncommitted docs before editing them, and never clears
  a repository root.
- The docs no longer publish the checkout's absolute path.
- The CLI starts in 0.08 s (`codewiki --version`), and `serve`, `ask` and
  `config validate` say what they are doing before slow steps.
  `config validate` fails fast and says why.
- `config show --json` reports `"api_key_storage": "file"` instead of
  `"encrypted_file"`: the fallback credentials file is plain JSON with
  owner-only permissions, not encrypted, and every message now says so.
- tree-sitter 0.26.

### Fixed

- Ask AI cited the wrong source lines: code is now read with its line numbers.
- An empty `old_str` in the editor crashed the agent and failed its module.
- A failed agent's half-written page counted as done on resume, so the module
  was never retried.
- A path that started with the docs folder's own name created a nested
  `docs/docs/` page.
- Ask AI stats counted cached prompt tokens twice.
- Streamed completions reported the requested model instead of the one that
  answered, so cost was priced on the wrong model.
- Windows: Go, Zig and Vue component ids used `\`; MCP doc edits never matched
  after the first write (`\n` saved as `\r\n`); UTF-8 pages could be read
  as cp1252 and saved garbled; Ask AI survives files named `NUL`.
- Rejected tool calls are reported again with pydantic-ai 2.x.
- Overview generation blocked other agents while it waited for the model.
- CodeWiki's own output in the docs folder is no longer analysed as code.
- Tool-call text no longer leaks into published parent pages.
- Viewer: diagram labels are no longer cut off; the Ask AI drawer is
  resizable.
- C and C++ functions returning pointers or references, and a macro before
  the name of a `(void)` function, are kept in the graph. Constructors are
  components like any method.
- Analyzers no longer swallow the per-file parse timeout. Doc-comment edits
  count as component changes in `--update`.
- Module clustering size estimates and source-file coverage; every file now
  belongs to exactly one module, and a run fails if one is left out.
- Dart: `*.config.dart` is no longer ignored, exact names match before short
  ones, and name matching works without a `pubspec.yaml`.
- Google: the Gemini fallback model resets when switching provider, and the
  client stays alive during checks.
- mermaid-py's "IPython is not installed" notice no longer reaches the
  terminal.

### Security

- **XSS in the docs viewer** through Mermaid blocks is fixed; angle brackets
  in labels are kept as text.
- Vulnerable dependencies were bumped, and CI scans dependencies (pip-audit),
  workflows (zizmor) and code (Bandit rules, through ruff) on every pull request,
  with Dependabot updates.
- The credentials file's storage is described accurately (see Changed).

## [2.0.2] - 2026-10-11

### Fixed

- **Compiled wheels crashed in dependency analysis** with `TypeError:
  Expected dict, got collections.defaultdict`. Cython turned type
  annotations such as `x: dict = ...` into exact-type checks, which reject
  subclasses like `defaultdict`; plain Python never checks them. The wheels
  are now built with `annotation_typing=False`.
- **Linux and macOS wheels failed PyPI's RECORD check.** RECORD listed the
  directory entries that auditwheel and delocate add (`codewiki/`, ...), so
  it did not match the wheel's files. `strip_wheel.py` now drops directory
  entries and fails the build if RECORD and the wheel contents differ.
- **Windows path handling.** The repository scan built paths with backslashes, so
  default ignore rules written with `/` (e.g. `.github` issue templates)
  stopped matching and analyzed file paths differed from Linux/macOS. Paths
  are now POSIX on every OS, and `find_doc` returns normalized paths.

### Changed

- The wheel build now runs the test suite against each installed wheel on
  every platform and Python version (`packaging/build/test_wheel.sh`), not
  just `codewiki --help`, so compiled-only failures block publishing.

## [2.0.1] - 2026-10-10

First release published to PyPI, as **`nc-codewiki`**
(`pip install nc-codewiki` or `uv tool install nc-codewiki`). The command is
still `codewiki`.

### Added

- **Prebuilt, compiled wheels on PyPI** for Windows x86_64, Linux x86_64
  (manylinux) and macOS (universal2: Apple Silicon and Intel), each for Python
  3.12, 3.13 and 3.14.
  Modules are compiled with Cython and the wheels ship without the Python
  source. `.github/workflows/build-wheels.yml` builds and smoke-tests them on
  every push to `main` and publishes them on `v*` tags.
- **Installers** for Windows, Linux and macOS (`packaging/install/`). They
  check git and Node.js/npm, then install with uv, or with pip if uv isn't
  wanted.
- **Backstage TechDocs output.** `codewiki generate --techdocs` (or
  `codewiki techdocs DOCS_DIR OUT_DIR` on existing docs, with no LLM calls)
  writes an MkDocs project that `techdocs-cli generate` builds as is:
  `mkdocs.yml` with the `techdocs-core` plugin and the module tree as the nav,
  plus `docs/` with the overview as `index.md`. Each page gets front matter, a
  provenance banner (commit, date, model) and a collapsible list of the
  components it documents, linked to the source at the documented commit.
  JSON files and `temp/` are left out
  ([#145](https://github.com/FSoft-AI4Code/CodeWiki/issues/145)).
- **Google provider.** `google` (Gemini API key) and `google-vertex` (Vertex AI
  with Google credentials: ADC or Workload Identity Federation, plus
  `--google-project` and `--google-location`) run on Gemini without a base
  URL. Agent loops use pydantic-ai's native `GoogleModel`, so Gemini 3 thought
  signatures survive tool-call turns, and the cost summary prices Gemini runs
  from genai-prices ([#20](https://github.com/FSoft-AI4Code/CodeWiki/issues/20)).
- **Rust analyzer.** Structs, unions, enums, traits, free functions, and
  impl/trait methods become components, with trait-impl, field-type,
  struct-literal, and call edges. `#[cfg(test)]` modules are skipped, and
  `build.rs` is classified as a build artifact.
- **Dart / Flutter analyzer.** Classes, mixins, extensions, enums, top-level
  functions and getters, and methods become components, with inheritance,
  field-type, instantiation and call edges. `package:`/relative imports,
  exports and `part` files decide which definition a name resolves to, and
  library-private names never resolve outside their library; names not
  visible through imports are dropped rather than guessed. Without a
  `pubspec.yaml` in the analyzed tree, `package:` imports cannot be resolved,
  so Dart calls fall back to name matching across Dart files (with a
  warning). Flutter-aware:
  widgets and State classes are typed, instantiations in `build()` become
  widget-to-widget edges, `ref.watch`/`ref.read` link widgets to Riverpod
  providers (including `@riverpod` codegen), and GoRouter routes link to their
  screens. Generated `*.g.dart`/`*.freezed.dart` files and `.dart_tool/` are
  ignored, and Dart modules get Flutter-specific documentation guidance.
- **Go analyzer.** Functions, methods, structs, interfaces (and their method
  signatures), and other defined types (`type Kind int`) become components.
  Methods are keyed by their receiver type (`Buffer.Push`, pointer or value
  receiver alike), whichever file of the package declares them. Edges cover
  calls (plain, `pkg.F()`, and method calls on receivers, parameters and
  locals whose type can be inferred), struct field and embedded types,
  composite literals, types named in signatures and local `var`s, and types
  that implement an interface of their own package. Names resolve inside the
  package (directory) they name; imports of the repository's own packages map
  to directories through the module path in `go.mod` (and local `replace`
  directives), and standard-library or third-party calls are dropped.
  `_test.go` files and `vendor/` are skipped.
- **Zig analyzer.** Functions, containers (`struct`, `enum`, `union`,
  `opaque`), their member functions (`Point.add`), and generic type functions
  (`fn List(comptime T: type) type { return struct {...}; }`) become
  components. Edges cover calls (plain, `alias.fn()`, and method calls on
  `Self`, parameters, and locals whose type can be inferred), `T{...}`
  literals, and types named in parameters, return types, fields, and
  annotated locals. `@import("x.zig")` aliases and re-exports
  (`const T = @import("x.zig").T;`) resolve to the file that declares the
  name, as do module names that the root `build.zig` registers with
  `addModule`; `std`, `builtin`, and third-party packages are dropped. `test`
  blocks and `zig-cache/`, `.zig-cache/`, `zig-out/` are skipped, `build.zig`
  is also a build artifact, and `build.zig.zon` is a manifest.
- **Vue single-file components.** Each `.vue` file becomes a component
  (`ArticleList.vue::ArticleList`), and its options-API members (`methods`,
  `computed`, `watch`, lifecycle hooks such as `mounted` or `setup`) become
  its methods. `<script>` and `<script setup>` blocks go through the
  JavaScript or TypeScript analyzer (`lang="ts"`), keeping the `.vue` file's
  line numbers. Edges link a component to the `.vue` files and modules it
  imports (relative paths and the `@/` alias), to the components its template
  uses (`<article-preview>` or `<ArticlePreview>`), and to the members and
  script functions its template expressions call or bind.
- **Autotools build files.** `configure.ac`, `Makefile.am` (and other `*.am`),
  `acinclude.m4`, and macros under `m4/` are build artifacts. A `Makefile.am`
  gets a unit per library or program it builds (`libfoo.la`), linked to the
  sources in its `_SOURCES` lists, and `SUBDIRS` links to the sub-directory
  `Makefile.am`s. A `configure.ac` gets a unit per `AC_ARG_ENABLE` /
  `AC_ARG_WITH` option and links to the `Makefile.am`s that `AC_CONFIG_FILES`
  generates.

### Changed

- **Docs mirror the module tree** ([#125](https://github.com/FSoft-AI4Code/CodeWiki/issues/125)).
  A module's page now sits next to the folder that holds its sub-modules
  (`auth.md`, `auth/login.md`) instead of every page sharing one flat folder.
  Links between pages are relative. After each run, pages saved in the wrong
  folder are moved and wrong links are repaired. `--flat` keeps the old
  layout for small models. The layout is recorded in `metadata.json`, and
  `--update` keeps it, so existing flat docs stay flat. The GitHub Pages
  viewer, the web app and the MCP tools (`doc_path` in
  `processing_order.json`) follow the layout.
- **caw is vendored** as `codewiki._vendor.caw`, from the
  `fix/codex-exec-robustness` branch of the caw fork, because PyPI does not
  accept git-URL dependencies. `coding-agent-wrapper` is no longer a
  dependency, and `mcp` is capped below 2.0, which caw requires.

### Fixed

- Running flake8 from the editor tool no longer passes the file path through
  a shell (Bandit B602).

## [2.0.0] - 2026-09-18

The first release since 1.0.1. Everything that landed on `main` in between is
listed here.

### Added

- **Artifact-aware generation** (#106). Build, CI, container, packaging,
  manifest, configuration, schema, and script files become nodes in the
  dependency graph, with heuristic edges to the code they reference. A
  deterministic artifact index goes into every module prompt, and a
  guaranteed **Build, Deployment and Configuration** module is inserted when
  clustering drops most artifact nodes. Flags: `--artifacts/--no-artifacts`,
  `--artifact-token-budget`, `--with-prose`, `--artifact-exclude`.
- **Component-level incremental updater** (#111). `--update` now diffs the
  saved dependency graph against the current code, repairs the module tree
  (renames, deletions, routing of new components, re-clustering of grown
  modules), builds a change report per leaf module, and runs one agent per
  affected module that patches its page and the pages that describe it. Falls
  back to a full build when too much changed. Every decision is written to
  `update_record.json`. Flags: `--update-rung`, `--tau-ren`, `--tau-nb`,
  `--tau-grow`, `--tau-full`, `--tau-tree`, `--k-hop`, `--max-diff-tokens`.
- `--compare-to <commit>` to set the base commit for an update (#67).
- **More complete code graphs.** Free functions become documentation units in
  function-centric projects. Scope-, namespace-, include-, and import-aware
  call resolution for C, C++, Java (#64, #68), and C# raised to the Java
  standard. An external symbol table keeps library calls out of the graph.
- **Language analyzers**: Kotlin (#41), PHP, Ruby (#97), Scala (#108).
- **Subscription providers** `claude-code` and `codex` (#60, #70): every LLM
  call goes through the local CLI via `caw`, no API key needed.
- **Atlas Cloud** provider (#69), **Azure OpenAI** (#49), **AWS Bedrock**
  (#40).
- **MCP server** (#9, #66) with eight LLM-free tools (`analyze_repo`,
  `read_code_components`, `write_doc_file`, `edit_doc_file`,
  `save_module_tree`, `get_processing_order`, `get_prompt`, `close_session`)
  plus the legacy `generate_docs` and `get_module_tree`; file side-channel
  workspace; module-level change detection; `codewiki mcp` command; a Claude
  skill under `skills/`.
- **Prompt caching** for agentic calls, `--prompt-caching/--no-prompt-caching`
  (#81).
- `.gitignore` handling, `--use-gitignore/--no-gitignore` (#80).
- Configurable `--max-tokens`, `--max-token-per-module`,
  `--max-token-per-leaf-module`, `--max-depth`; `--fallback-model`;
  `--instructions` and `codewiki config agent` defaults.
- `SECURITY.md` and `LICENSE` (#107). CI workflow: pytest and ruff on
  Python 3.12.
- `CHANGELOG.md` (this file) and the `guides/` folder.

### Changed

- `--update` runs the component-level updater. The 1.x file-level
  invalidation is still available as `--update-rung 0`.
- Artifacts are documented by default. `--no-artifacts` restores the code-only
  behaviour of 1.x.
- `generator_version` in `metadata.json` is read from the package version
  instead of a hardcoded string.
- Hand-written guides moved from the top level and `docker/` into `guides/`:
  `DEVELOPMENT.md` -> `guides/development.md`, `IDE_DRIVEN_GUIDE.md` ->
  `guides/mcp-ide-mode.md`, `docker/DOCKER_README.md` -> `guides/docker.md`.
  New: `guides/cli-reference.md`, `guides/providers.md`,
  `guides/artifact-aware-generation.md`, `guides/incremental-updates.md`.
- README rewritten around 2.0. The benchmark section reports the 2.0
  rebenchmark on the seven CodeWikiBench repositories, with DeepWiki and 1.0
  re-run under the same setting.
- Component ids reformatted to `path::name` (#48). Ruff rule set pinned;
  Node.js requirement clarified as install-time only.
- Project URLs in `pyproject.toml` point at the real repository.

### Fixed

- Overview and module prompts exceeding the Codex input cap on large
  repositories (#92, #94).
- Resume skipping modules whose documentation was missing (#94).
- `--update` in whole-repository mode not regenerating invalidated modules
  (#100).
- The CLI adapter not passing `commit_id`, which made `--update` behave like
  a full build.
- Leaf-node filtering dropping identifiers such as `handleInvalidInput`
  (#102); leaf-node reduction now logged at INFO (#103).
- C codebases producing zero leaf nodes.
- `node_modules` excluded from analysis; missing runtime dependencies added;
  non-standard responses from OpenAI-compatible proxies handled.
- Mermaid validation hang in the MCP server; MCP path-traversal guards and
  edit-history caps.
- Star history chart in the README (#95); mermaid-py behaviour comments (#110).

## [1.0.1] - 2025-10-18

- CLI release: `codewiki config set`, `codewiki config show`,
  `codewiki config validate`, `codewiki generate` with `--output`,
  `--create-branch`, `--github-pages`, `--no-cache`, `--include`,
  `--exclude`, `--focus`, `--doc-type`, `--verbose`.
- API keys stored in the system keychain with a file fallback.

## [1.0.0] - 2025-09

- Initial public release with the paper: hierarchical decomposition,
  recursive agents, Mermaid diagrams, seven languages (Python, Java,
  JavaScript, TypeScript, C, C++, C#), web application, Docker image,
  CodeWikiBench.

[2.0.2]: https://github.com/dandydarshan/CodeWiki/releases/tag/v2.0.2
[2.0.1]: https://github.com/dandydarshan/CodeWiki/releases/tag/v2.0.1
[2.0.0]: https://github.com/FSoft-AI4Code/CodeWiki/releases/tag/v2.0.0
