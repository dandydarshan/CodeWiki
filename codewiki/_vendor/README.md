# Vendored packages

## caw (coding-agent-wrapper)

- Upstream: https://github.com/zzjas/caw (Apache License 2.0, see `caw/LICENSE`)
- Source: fork https://github.com/anhnh2002/caw, branch `fix/codex-exec-robustness`,
  commit `9ec22af22a32ee50392443ae00ca401c3ec4e46b` (package version 0.1.10)
- Why vendored: CodeWiki needs fixes not yet released on PyPI (latest is 0.1.9),
  and PyPI does not accept packages that depend on a git URL.

Modifications from the source commit:

- Absolute imports `from caw...` rewritten to `from codewiki._vendor.caw...`.
- Removed the command-line modules CodeWiki does not use (`cli.py`, `config_cli.py`,
  `pricing_cli.py`, `traj_cli.py`, `auth/cli.py`), which drops the `typer` dependency.
- `auth/collector.py`: escaped two backticks as `\\`` instead of `\``, which is an
  invalid escape sequence (SyntaxWarning today, an error in future Pythons). The
  resulting string is unchanged.

To update: copy `caw/` from the new upstream commit, repeat the steps above,
and update the commit hash here.
