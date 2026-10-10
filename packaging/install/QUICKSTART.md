# CodeWiki — Quick Start

Generate documentation for a codebase in a few commands.
(Assumes CodeWiki is already installed — see `INSTALL.md`.)

## 1. Confirm the install

```cmd
codewiki --version
```

If the command isn't found, open a new terminal (PATH updates only apply to
new shells after install).

## 2. Configure

Run this once to point CodeWiki at your model endpoint. Replace
`<your-api-key>` with your key.

**Windows (cmd / PowerShell) — single line:**

```cmd
codewiki config set --base-url <your-base-url> --api-key <your-api-key> --main-model kimi-k2.6 --cluster-model bedrock/global.anthropic.claude-sonnet-5-5 --fallback-model bedrock/global.anthropic.claude-haiku-5-5 --max-context-tokens 262144 --max-depth 3
```

**Git Bash / WSL — multi-line (backslash continuations):**

```bash
codewiki config set \
  --base-url <your-base-url> \
  --api-key <your-api-key> \
  --main-model kimi-k2.6 \
  --cluster-model bedrock/global.anthropic.claude-sonnet-5-5 \
  --fallback-model bedrock/global.anthropic.claude-haiku-5-5 \
  --max-context-tokens 262144 \
  --max-depth 3
```

### Model options

- **`--cluster-model`** — `bedrock/global.anthropic.claude-sonnet-5-5` (default
  here). Alternatives: `bedrock/global.anthropic.claude-haiku-5-5` (cloud,
  cheaper) or `gpt-oss-120b` (local).
- **`--fallback-model`** — `bedrock/global.anthropic.claude-haiku-5-5`.
  Alternative: `gpt-oss-120b` (local).

### Budget-friendly option

To reduce token usage, lower the per-leaf-module limit:

```cmd
codewiki config set --max-token-per-leaf-module 16000
```

## 3. Check and validate the config

```cmd
codewiki config show
codewiki config validate
```

`config show` prints your current settings; `config validate` confirms the
endpoint and models are reachable and the config is well-formed.

## 4. Generate documentation

Run this **from inside the repository** you want to document — CodeWiki
analyzes the current directory and writes to `./docs/` by default.

```cmd
cd path\to\your-repo
codewiki generate --verbose --github-pages
```

- `--verbose` — show detailed progress.
- `--github-pages` — also produce an `index.html` for GitHub Pages hosting.

Useful extras:

- `--output <dir>` — write docs somewhere other than `./docs`.
- `--include "*.cs"` / `--exclude "*Tests*"` — limit which files are analyzed.
- `--focus "src/core,src/api"` — document only specific paths (faster).

## 5. Update docs after code changes

Regenerate only what changed since the last run:

```cmd
codewiki generate --update --verbose --github-pages
```

This is much faster than a full regeneration — it only reprocesses modules
affected by your changes.

---

**Tip:** run `codewiki generate --help` for the full list of options
(token limits, doc types, incremental-update tuning, and more).
