"""`codewiki serve`: the local Q&A API and the docs site's chat window."""

import json

import pytest
from fastapi.testclient import TestClient

from codewiki.src.be.qa.engine import QAEngine
from codewiki.src.be.qa.index import QAIndex
from codewiki.src.be.qa.server import create_app, origin_of
from codewiki.src.be.qa.sessions import SessionStore
from qa_fixture import build
from test_qa_engine import UNSIGN, ScriptedBackend

PAGES = "https://acme.github.io"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEWIKI_QA_LOG_DIR", str(tmp_path / "logs"))
    _, docs_dir = build(tmp_path)
    (docs_dir / "index.html").write_text("<html>docs</html>")
    backend = ScriptedBackend({"keep": [UNSIGN], "confidence": 95})
    sessions = SessionStore()  # like get_engine, which reuses one engine

    def factory():
        return QAEngine(QAIndex(str(docs_dir)), backend, sessions=sessions)

    app = create_app(str(docs_dir), {"http://127.0.0.1:8765", PAGES}, engine_factory=factory)
    return TestClient(app), backend


def _events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_health_reports_freshness(client):
    c, _ = client
    data = c.get("/api/health").json()
    assert data["ok"] and data["repo"] == "repo"
    assert "up to date" in data["freshness"]["summary"]
    assert data["freshness"]["stale_pages"] == []


def test_static_site_served_but_not_internals(client):
    c, _ = client
    assert c.get("/").text == "<html>docs</html>"
    assert "# Timed" in c.get("/Timed.md").text
    assert c.get("/temp/dependency_graphs/repo_dependency_graph.json").status_code == 404
    assert c.get("/metadata.json").status_code == 200
    assert c.get("/../pkg/signer.py").status_code == 404
    assert c.get("/%2e%2e/pkg/signer.py").status_code == 404


def test_search_endpoint(client):
    c, _ = client
    results = c.post("/api/search", json={"query": "unsign expiry", "k": 5}).json()["results"]
    assert any(r["id"] == UNSIGN for r in results)
    assert c.post("/api/search", json={"query": "x", "kinds": "bogus"}).status_code == 400


def test_ask_streams_status_then_answer(client):
    c, backend = client
    response = c.post("/api/ask", json={"question": "How does expiry work?"})
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = _events(response)
    assert events[0]["type"] == "status"
    answer = events[-1]
    assert answer["type"] == "answer" and "It checks the age." in answer["answer"]
    assert answer["sources"][0]["id"] == UNSIGN
    assert answer["stats"]["llm_calls"] == 2 and "Answered in" in answer["stats"]["footer"]
    # follow-up keeps the session
    events = _events(
        c.post("/api/ask", json={"question": "And?", "session_id": answer["session_id"]})
    )
    assert events[-1]["session_id"] == answer["session_id"]
    assert c.post("/api/ask", json={"question": "  "}).status_code == 400


def test_ask_passes_keep_turns_and_returns_the_turn_count(client):
    c, backend = client
    backend.answer = "Answer.\n<SUMMARY>expiry uses MAX_AGE</SUMMARY>"
    first = _events(c.post("/api/ask", json={"question": "How does expiry work?"}))[-1]
    assert first["turns"] == 1
    sid = first["session_id"]
    second = _events(c.post("/api/ask", json={"question": "And?", "session_id": sid}))[-1]
    assert second["turns"] == 2
    # Retrying the first question: the server forgets both earlier turns first.
    redo = _events(
        c.post("/api/ask", json={"question": "Again?", "session_id": sid, "keep_turns": 0})
    )[-1]
    assert redo["turns"] == 1
    assert c.post("/api/ask", json={"question": "q", "keep_turns": -1}).status_code == 422


def test_closing_the_stream_stops_the_engine(tmp_path, monkeypatch):
    """The chat's Stop button aborts the fetch; no further LLM call may start."""
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    monkeypatch.setenv("CODEWIKI_QA_LOG_DIR", str(tmp_path / "logs"))
    _, docs_dir = build(tmp_path)

    class SlowBackend(ScriptedBackend):
        def complete(self, prompt, **kw):
            time.sleep(0.4)
            return super().complete(prompt, **kw)

    backend = SlowBackend(
        {"keep": [], "confidence": 10}, rounds=[{"keep": [], "confidence": 20}] * 5
    )
    app = create_app(
        str(docs_dir),
        set(),
        engine_factory=lambda: QAEngine(QAIndex(str(docs_dir)), backend, sessions=SessionStore()),
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        with httpx.stream(
            "POST", f"http://127.0.0.1:{port}/api/ask", json={"question": "How?"}, timeout=10
        ) as r:
            first = next(r.iter_lines())
            assert json.loads(first)["type"] == "status"
        # The plan call was running when the client left; let it finish.
        time.sleep(1.5)
        assert len(backend.calls) == 1
    finally:
        server.should_exit = True
        thread.join(5)


def test_ask_errors_are_streamed(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEWIKI_QA_LOG_DIR", str(tmp_path / "logs"))
    _, docs_dir = build(tmp_path)

    class Broken:
        def ask(self, *a, **k):
            raise RuntimeError("usage limit reached")

    c = TestClient(create_app(str(docs_dir), set(), engine_factory=Broken))
    events = _events(c.post("/api/ask", json={"question": "q"}))
    assert events == [{"type": "error", "message": "usage limit reached"}]

    def no_config():
        raise RuntimeError("CodeWiki is not configured")

    c = TestClient(create_app(str(docs_dir), set(), engine_factory=no_config))
    r = c.post("/api/ask", json={"question": "q"})
    assert r.status_code == 503 and "not configured" in r.json()["detail"]


def test_only_allowed_origins_may_use_the_api(client):
    c, _ = client
    ok = c.get("/api/health", headers={"Origin": PAGES})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == PAGES
    preflight = c.options(
        "/api/ask",
        headers={
            "Origin": PAGES,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-private-network"] == "true"
    evil = c.post("/api/ask", headers={"Origin": "https://evil.example"}, json={"question": "q"})
    assert evil.status_code == 403
    assert "access-control-allow-origin" not in evil.headers
    # a page fetching a doc file cross-origin gets no CORS grant either
    page = c.get("/Timed.md", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in page.headers


def test_origin_of():
    assert origin_of("https://acme.github.io/repo/") == "https://acme.github.io"
    assert origin_of("not a url") is None


def test_generated_site_has_the_chat_window(tmp_path):
    from codewiki.cli.html_generator import HTMLGenerator

    out = tmp_path / "index.html"
    HTMLGenerator().generate(
        out,
        "demo",
        module_tree={},
        repository_url="https://github.com/acme/repo",
        metadata={"generation_info": {"commit_id": "abc123"}},
    )
    html = out.read_text(encoding="utf-8")  # Windows defaults to cp1252
    assert 'id="chat-trigger"' in html and 'id="chat-panel"' in html
    assert "dompurify@3.2.4" in html and "DOMPurify.sanitize" in html
    config = html.split("const CONFIG = ", 1)[1].split(";\n", 1)[0]
    data = json.loads(config)
    # Every port `codewiki serve` may pick, and the repo the site must match.
    assert data["chat"]["endpoints"] == [f"http://127.0.0.1:{p}" for p in range(8765, 8775)]
    assert data["chat"]["repo"] == "demo"
    assert data["repository_url"] == "https://github.com/acme/repo"
    assert data["commit_id"] == "abc123"


# ---------------------------------------------------------------- several repos at once


def test_health_names_the_repository(tmp_path, monkeypatch):
    import codewiki.src.be.qa.server as server

    monkeypatch.setattr(
        server, "repository_info", lambda d: {"url": "https://github.com/acme/repo"}
    )
    _, docs_dir = build(tmp_path)
    data = TestClient(create_app(str(docs_dir), set())).get("/api/health").json()
    # The docs site connects only to the server whose repository_url matches its own.
    assert data["repository_url"] == "https://github.com/acme/repo"


def _listening(port):
    import socket

    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen()
    return s


def test_free_port_skips_listening_ports_but_not_just_freed_ones():
    import os
    import socket
    import time

    from codewiki.src.be.qa.server import find_free_port

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        base = probe.getsockname()[1]
    busy = _listening(base)
    try:
        assert find_free_port("127.0.0.1", base, 1) is None
        assert find_free_port("127.0.0.1", base, 3) == base + 1
        # Leave the port in TIME_WAIT, as a just-stopped `codewiki serve` does.
        client = socket.create_connection(("127.0.0.1", base))
        conn, _ = busy.accept()
        conn.close()
        client.close()
    finally:
        busy.close()
    time.sleep(0.1)
    if os.name != "nt":
        assert find_free_port("127.0.0.1", base, 1) == base  # restartable right away


def test_serve_takes_the_next_free_port_and_says_so(tmp_path, monkeypatch):
    from click.testing import CliRunner

    import codewiki.src.be.qa.server as server
    from codewiki.cli.commands.serve import serve_command

    _, docs_dir = build(tmp_path)
    used = {}
    monkeypatch.setattr(server, "serve", lambda docs, host, port, *a, **k: used.update(port=port))
    monkeypatch.setattr(server, "find_free_port", lambda host, start, count: start + 1)
    result = CliRunner().invoke(serve_command, ["--docs", str(docs_dir)])
    assert result.exit_code == 0, result.output
    assert used["port"] == 8766
    assert "Port 8765 is in use" in result.output
    assert "http://127.0.0.1:8766/" in result.output


def test_serve_refuses_a_busy_explicit_port_before_indexing(tmp_path, monkeypatch):
    from click.testing import CliRunner

    import codewiki.src.be.qa as qa
    import codewiki.src.be.qa.server as server
    from codewiki.cli.commands.serve import serve_command

    _, docs_dir = build(tmp_path)
    monkeypatch.setattr(server, "find_free_port", lambda host, start, count: None)
    monkeypatch.setattr(qa, "get_index", lambda d: pytest.fail("indexed before the port check"))
    result = CliRunner().invoke(serve_command, ["--docs", str(docs_dir), "--port", "9123"])
    assert result.exit_code != 0
    assert "Port 9123 is in use" in result.output


def test_missing_graph_is_rebuilt_from_the_repo(tmp_path):
    import shutil

    _, docs_dir = build(tmp_path)
    shutil.rmtree(docs_dir / "temp")  # a fresh clone: docs/temp/ is git-ignored
    index = QAIndex(str(docs_dir))
    assert index.code, "code search is off without a graph"
