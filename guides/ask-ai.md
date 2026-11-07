# Ask AI

Once the docs are generated, CodeWiki answers questions about the code from
those docs and the current code, with citations to doc sections and source
lines. There are three ways to ask:

| Where | Command | LLM |
| --- | --- | --- |
| The docs site's chat window | `codewiki serve` | your CodeWiki provider |
| The terminal | `codewiki ask "..."` | your CodeWiki provider |
| An IDE agent, over MCP | `codewiki mcp` | the IDE's model; CodeWiki's tools use none, except `codewiki_ask` |

All three use the provider set with `codewiki config set`, including the
Claude Code and Codex subscriptions. Nothing leaves your machine except the
LLM calls themselves.

## In the docs site: `codewiki serve`

```bash
cd /path/to/your/project
codewiki generate        # once
codewiki serve           # prints the URL of the docs site with its chat
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--docs PATH` | `$CODEWIKI_DOCS_DIR`, else `./docs` | The generated docs folder |
| `--port N` | the first free port of 8765–8774 | Where it listens. The GitHub Pages site looks for it on these ports |
| `--host HOST` | `127.0.0.1` | Interface to listen on. Anything but localhost exposes your code and your LLM quota to the network |
| `--allow-origin URL` | none | Another site allowed to use the chat API, e.g. a custom Pages domain (repeatable) |
| `--model NAME` | `main_model` | Model for answers |
| `--no-log-questions` | off | Keep question text out of the query log |

The docs published with `--github-pages` have the same chat window. On GitHub
Pages it finds `codewiki serve` running for that repository on your machine,
even when you serve several repositories at once. If the browser blocks the
site from reaching localhost (Chrome asks for local-network access), the chat
says so and how to allow it.

In the chat you can copy, edit or retry a question, stop an answer, follow its
progress, and minimise or resize the window. Answers render Mermaid diagrams.

## In the terminal: `codewiki ask`

```bash
codewiki ask "where is the signature verified?"
codewiki ask "what breaks if I remove max_age from TimestampSigner.unsign?"
codewiki ask --session <id> "and where is it called from?"   # follow-up
codewiki ask --json "..."                                     # answer, sources and stats
```

| Flag | Meaning |
| --- | --- |
| `--docs PATH` | The generated docs folder (default `$CODEWIKI_DOCS_DIR`, else `./docs`) |
| `--session ID` | Continue an earlier conversation; each answer prints its session id |
| `--max-context-tokens N` | Cap on the context read for one answer |
| `--model NAME` | Model for the answer (default `main_model`) |
| `--json` | Print the full result: answer, sources and stats |
| `--no-log-questions` | Keep question text out of the query log |

Each answer ends with a footer: time, LLM calls, tokens (with the share served
from the prompt cache), context read, and cost.

## In an IDE: the MCP tools

`codewiki mcp` (see [MCP / IDE-driven mode](mcp-ide-mode.md)) also gives the
IDE agent these tools over generated docs:

| Tool | What it does | LLM |
| --- | --- | --- |
| `codewiki_outline` | The module tree with a one-line summary per page: a cheap map of the codebase | none |
| `codewiki_status` | Whether the docs are current: the commit they were generated at, commits and files changed since, stale pages | none |
| `codewiki_search` | Where something is documented or implemented, one compact line per doc section or code component | none |
| `codewiki_read` | Exact doc sections and component source by id, within a token budget | none |
| `codewiki_impact` | Every definition and usage of a symbol, labelled from the AST, for "what breaks if I change X?" | none |
| `codewiki_ask` | A cited answer, using CodeWiki's own provider | CodeWiki's |

The IDE agent usually does best with the no-LLM tools, reasoning over what
they return with its own model.

## How an answer is found

1. **Plan.** A search over the doc sections and code components returns
   metadata only: titles, signatures and sizes. One LLM call picks what to
   read and how much context the question needs.
2. **Scout and refine.** Lexical search, grep over the working tree and
   call-graph neighbours add candidates. Change-impact questions also scan
   the code for every reference. Further rounds adjust the selection until the
   model is confident or the budget is spent.
3. **Answer.** Only the chosen doc sections and function or class bodies are
   read, within the token budget. A body too long for it is cut to the lines
   that match the question.

Code always comes from the current working tree, with each line's number in
the file, so citations point at exact lines. Doc sections whose code changed
since the docs were generated are marked stale, and the answer prefers the
code. A typical answer takes 2 to 4 LLM calls.

## Logs and privacy

Each answer appends one line to `~/.codewiki/logs/qa-YYYY-MM-DD.jsonl`
(outside the docs folder, which is usually committed): the time, the docs
folder, the question, and the answer's stats (calls, tokens, timings, cost). `--no-log-questions` leaves out the question text,
`CODEWIKI_QA_LOG=0` turns the log off, and `CODEWIKI_QA_LOG_DIR` moves it.
