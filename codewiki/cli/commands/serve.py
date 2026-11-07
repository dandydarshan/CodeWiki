"""``codewiki serve``: browse the docs and chat with them, locally."""

from __future__ import annotations

import logging
import sys
import time

import click

from codewiki.src.config import DEFAULT_QA_PORT, QA_PORT_COUNT


@click.command(name="serve")
@click.option(
    "--docs",
    "docs_dir",
    default=None,
    help="Generated docs folder (default: $CODEWIKI_DOCS_DIR, else ./docs)",
)
@click.option(
    "--port",
    type=int,
    default=None,
    help=f"Port to listen on (default: the first free one of {DEFAULT_QA_PORT}-"
    f"{DEFAULT_QA_PORT + QA_PORT_COUNT - 1}, where the docs site looks for it)",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Interface to listen on. Anything but localhost exposes your code and LLM quota.",
)
@click.option(
    "--allow-origin",
    "allow_origins",
    multiple=True,
    help="Extra site origin allowed to use the chat API (repeatable), e.g. a custom Pages domain",
)
@click.option("--model", default=None, help="Model for answers (default: main_model)")
@click.option("--no-log-questions", is_flag=True, help="Keep question text out of the query log")
def serve_command(docs_dir, port, host, allow_origins, model, no_log_questions):
    """Serve the docs site with a local "Ask AI" chat over the docs and code.

    Open the printed URL, or keep using the GitHub Pages site: its chat
    window finds this repo's server on your machine, even with several repos
    served at once. Answers use your CodeWiki config (`codewiki config set`);
    nothing leaves your machine except the LLM calls themselves.
    """
    # Before the imports below, which take seconds on some machines (compiled wheel on Windows).
    click.echo("Starting CodeWiki serve…")
    from codewiki.src.be.qa import DocsNotFoundError, get_index, resolve_docs_dir
    from codewiki.src.be.qa.server import default_origins, find_free_port, origin_of, serve

    # Pick the port before indexing, so a busy port fails fast and the printed URL is right.
    last = DEFAULT_QA_PORT + QA_PORT_COUNT - 1
    if port is None:
        port = find_free_port(host, DEFAULT_QA_PORT, QA_PORT_COUNT)
        if port is None:
            raise click.ClickException(
                f"Ports {DEFAULT_QA_PORT}-{last} are all in use (other codewiki serve "
                "processes?). Stop one, or choose a port with --port."
            )
        if port != DEFAULT_QA_PORT:
            click.echo(f"Port {DEFAULT_QA_PORT} is in use (another codewiki serve?); using {port}.")
    elif find_free_port(host, port, 1) is None:
        raise click.ClickException(
            f"Port {port} is in use. Stop the process using it, or leave out --port to use a "
            f"free one of {DEFAULT_QA_PORT}-{last}."
        )
    elif not DEFAULT_QA_PORT <= port <= last:
        click.secho(
            f"Note: the GitHub Pages site looks for ports {DEFAULT_QA_PORT}-{last}; on port "
            f"{port} only the site served here ({host}:{port}) has chat.",
            fg="yellow",
            err=True,
        )

    try:
        docs = resolve_docs_dir(docs_dir)
    except DocsNotFoundError as e:
        raise click.ClickException(str(e)) from e
    if host not in ("127.0.0.1", "localhost", "::1"):
        click.secho(
            f"Warning: listening on {host} lets other machines use this API "
            "(your code and your LLM quota).",
            fg="yellow",
            err=True,
        )
    from rich.console import Console

    qa_logger = logging.getLogger("codewiki.src.be.qa")
    qa_logger.setLevel(logging.INFO)
    click.echo(f"Loading {docs} …")
    started = time.monotonic()
    # Indexing can take a while (re-reading code changed since the docs): show
    # each step the index logs as it starts it.
    with Console(stderr=True).status("Reading the docs…") as status:
        step = _StatusHandler(status)
        qa_logger.addHandler(step)
        try:
            index = get_index(docs)
            status.update("Starting the server…")
            origins = sorted(
                default_origins(host, port, docs) | {origin_of(o) or o for o in allow_origins}
            )
        finally:
            qa_logger.removeHandler(step)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    qa_logger.addHandler(handler)

    click.secho(f"CodeWiki docs + chat: http://{host}:{port}/", fg="green", bold=True)
    click.echo(f"  Docs:      {docs}")
    click.echo(
        f"  Indexed:   {len(index.sections)} doc sections, {len(index.code)} code components "
        f"in {time.monotonic() - started:.1f}s"
    )
    click.echo(f"  Freshness: {index.freshness.summary()}")
    if index.stale_pages:
        click.secho(
            f"  {len(index.stale_pages)} page(s) are older than the code; answers use the "
            "current code. Refresh them with `codewiki generate --update`.",
            fg="yellow",
        )
    click.echo(f"  Chat allowed from: {', '.join(origins)}")
    click.echo("  Ctrl+C to stop.")
    serve(docs, host, port, allow_origins, model=model, log_questions=not no_log_questions)


class _StatusHandler(logging.Handler):
    """Shows each log message as the spinner's text."""

    def __init__(self, status) -> None:
        super().__init__(logging.INFO)
        self.status = status

    def emit(self, record: logging.LogRecord) -> None:
        self.status.update(record.getMessage())
