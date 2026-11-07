"""The generated docs viewer in a real browser: sidebar, search, diagram viewer.

Optional: runs only when Playwright and a Chromium are available
(``pip install playwright && playwright install chromium``, or a system
Chromium on PATH). CI does not install them, so it skips there.
"""

import functools
import http.server
import json
import shutil
import threading

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from codewiki.cli.html_generator import HTMLGenerator  # noqa: E402

GROUPS = 8
PAGES_PER_GROUP = 5  # 8 groups x 5 pages: a sidebar much taller than the window

DIAGRAM = "flowchart TD\n" + "\n".join(
    f"    N{i}[Stage {i} reads the input] --> N{i + 1}[Stage {i + 1} writes the output]"
    for i in range(30)
)


def _page(title: str) -> str:
    return (
        f"# {title}\n\n{title} handles one part of the pipeline.\n\n"
        f"## How it works\n\nThe {title.lower()} stage reads, checks and writes.\n\n"
        f"```mermaid\n{DIAGRAM}\n```\n"
    )


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    docs = tmp_path_factory.mktemp("viewer")
    tree = {}
    for g in range(GROUPS):
        group = f"group_{g}"
        tree[group] = {"components": [f"g{g}.py::G"], "children": {}}
        (docs / f"{group}.md").write_text(_page(f"Group {g}"), encoding="utf-8")
        for i in range(PAGES_PER_GROUP):
            name = f"{group}_page_{i}"
            tree[group]["children"][name] = {"components": [f"g{g}/p{i}.py::P"], "children": {}}
            (docs / f"{name}.md").write_text(_page(f"Topic {g} {i}"), encoding="utf-8")
    (docs / "overview.md").write_text(_page("Overview"), encoding="utf-8")
    (docs / "module_tree.json").write_text(json.dumps(tree))
    HTMLGenerator().generate(docs / "index.html", "demo", module_tree=tree, docs_dir=docs)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(docs))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:  # noqa: BLE001 — no bundled browser: try a system one
            exe = shutil.which("chromium") or shutil.which("chromium-browser")
            if not exe:
                pytest.skip("no Chromium for Playwright")
            b = p.chromium.launch(executable_path=exe)
        yield b
        b.close()


@pytest.fixture
def page(site, browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 600})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(site + "/")
    pg.wait_for_selector(".nav-item")
    pg.wait_for_timeout(500)
    yield pg
    assert not errors, errors
    ctx.close()


ACTIVE_IN_VIEW = """() => {
    const a = document.querySelector('.nav-item.active');
    const s = document.getElementById('sidebar').getBoundingClientRect();
    const r = a.getBoundingClientRect();
    return [a.dataset.file, r.top >= s.top && r.bottom <= s.bottom];
}"""


def test_search_and_cleared_filter_bring_the_page_into_the_sidebar(page):
    last = f"group_{GROUPS - 1}_page_{PAGES_PER_GROUP - 1}.md"
    page.keyboard.press("Control+k")
    page.keyboard.type(f"Topic {GROUPS - 1} {PAGES_PER_GROUP - 1}")
    page.wait_for_timeout(400)
    page.keyboard.press("Enter")
    page.wait_for_timeout(1200)
    assert page.evaluate(ACTIVE_IN_VIEW) == [last, True]

    # Filter, open a page, then clear the filter with the keyboard.
    page.evaluate("document.getElementById('sidebar').scrollTop = 0")
    page.click("#nav-filter")
    page.keyboard.type("Group 6 Page 3")
    page.click(".nav-item:visible >> text=Group 6 Page 3")
    page.wait_for_timeout(800)
    page.click("#nav-filter")
    for _ in range(len("Group 6 Page 3")):
        page.keyboard.press("Backspace")
    page.wait_for_timeout(1200)
    assert page.evaluate(ACTIVE_IN_VIEW) == ["group_6_page_3.md", True]


def test_sidebar_toggles_are_easy_to_hit(page):
    box = page.locator(".nav-toggle").first.bounding_box()
    assert box["width"] >= 32 and box["height"] >= 32
    group = page.locator(".nav-group").first
    collapsed = "el => el.classList.contains('collapsed')"
    before = group.evaluate(collapsed)
    page.locator(".nav-toggle").first.click()
    assert group.evaluate(collapsed) != before


STAGE = """() => {
    const m = document.querySelector('.lightbox-stage').style.transform
        .match(/translate\\(([-\\d.]+)px, ([-\\d.]+)px\\) scale\\(([-\\d.]+)\\)/);
    return [+m[1], +m[2], +m[3]];
}"""


def test_diagram_viewer_pans_smoothly(page):
    page.wait_for_selector("#content .mermaid svg")
    page.locator("#content .mermaid").first.click()
    page.wait_for_selector(".mermaid-lightbox.open")
    page.wait_for_timeout(300)
    page.mouse.move(600, 300)
    page.mouse.wheel(0, -300)
    page.wait_for_timeout(200)
    start = page.evaluate(STAGE)
    page.mouse.down()
    for i in range(1, 41):
        page.mouse.move(600 + i * 5, 300 + i * 2)
    layer = page.evaluate("getComputedStyle(document.querySelector('.lightbox-stage')).willChange")
    page.mouse.up()
    page.wait_for_timeout(200)
    end = page.evaluate(STAGE)
    assert [round(end[0] - start[0]), round(end[1] - start[1])] == [200, 80]  # follows the mouse
    assert layer == "transform"  # own layer while dragging
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.lightbox-stage')).willChange"
    ) == ("auto")  # sharp again after
    assert page.evaluate("String(getSelection())") == ""  # dragging selects no text
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert page.locator(".mermaid-lightbox.open").count() == 0


def _open_chat(page):
    page.click("#chat-trigger")
    page.wait_for_selector(".chat-panel.open")
    page.wait_for_timeout(300)  # slide-in transition


PANEL_WIDTH = "() => document.getElementById('chat-panel').getBoundingClientRect().width"


def test_chat_drawer_resizes_and_remembers_its_width(page):
    _open_chat(page)
    assert page.evaluate(PANEL_WIDTH) == 440
    messages_before = page.evaluate("document.getElementById('chat-messages').clientWidth")

    handle = page.locator("#chat-resize").bounding_box()
    x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 260, y, steps=10)
    page.mouse.up()
    width = page.evaluate(PANEL_WIDTH)
    assert abs(width - (1280 - (x - 260))) <= 1, width
    # The content follows the drawer: no fixed widths inside it.
    assert page.evaluate("document.getElementById('chat-messages').clientWidth") > (
        messages_before + 250
    )

    page.reload()
    page.wait_for_selector(".nav-item")
    _open_chat(page)
    assert abs(page.evaluate(PANEL_WIDTH) - width) <= 1  # remembered

    page.focus("#chat-resize")
    page.keyboard.press("ArrowRight")
    assert abs(page.evaluate(PANEL_WIDTH) - (width - 20)) <= 1

    # Clamped to [320 px, 90% of the window].
    for target, expected in ((5, 1152), (1275, 320)):
        handle = page.locator("#chat-resize").bounding_box()
        x = handle["x"] + handle["width"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(target, y, steps=5)
        page.mouse.up()
        assert page.evaluate(PANEL_WIDTH) == expected

    page.dblclick("#chat-resize")
    assert page.evaluate(PANEL_WIDTH) == 440  # reset


def test_ask_button_keeps_its_height_when_the_input_grows(page):
    _open_chat(page)
    page.evaluate("document.getElementById('chat-input').style.height = '140px'")
    send = page.locator("#chat-send").bounding_box()
    box = page.locator("#chat-input").bounding_box()
    assert box["height"] == 140
    assert send["height"] == 40
    assert abs((send["y"] + send["height"]) - (box["y"] + box["height"])) <= 1  # bottom-aligned


# Re-render the page's diagram, optionally narrowing one label box in the first render
# (what Chrome on Windows produced when it measured before Mermaid's font was ready).
RERENDER = """async (clipFirst) => {
    let calls = 0;
    const real = mermaid.render.bind(mermaid);
    mermaid.render = async (id, src) => {
        const out = await real(id, src);
        if (++calls === 1 && clipFirst) {
            out.svg = out.svg.replace(/(<foreignObject height="[\\d.]+" width=")[1-9][\\d.]*/, '$15');
        }
        return out;
    };
    try {
        const el = document.querySelector('#content .mermaid');
        el.innerHTML = '';
        await renderMermaidDiagrams();
        return [calls, mermaidLabelsClipped(el)];
    } finally {
        mermaid.render = real;
    }
}"""


def test_diagram_labels_use_the_page_font(page):
    page.wait_for_selector("#content .mermaid svg")
    fonts = page.evaluate(
        "[getComputedStyle(document.querySelector('#content .mermaid .nodeLabel')).fontFamily,"
        " getComputedStyle(document.body).fontFamily]"
    )
    assert fonts[0] == fonts[1]


def test_clipped_diagram_labels_are_rendered_again(page):
    page.wait_for_selector("#content .mermaid svg")
    assert page.evaluate(RERENDER, False) == [1, False]  # fits first time: no extra render
    assert page.evaluate(RERENDER, True) == [2, False]  # clipped: rendered again, now fits


ESCAPES = [
    ("A -->|contains <cpm-sdk-app-root>| B", "A -->|contains #lt;cpm-sdk-app-root#gt;| B"),
    ('A["List<String> items"]', 'A["List#lt;String#gt; items"]'),
    ("A[Map<String, List<Integer>> cache]", "A[Map#lt;String, List#lt;Integer#gt;#gt; cache]"),
    ('A["one<br/>two<br>three <b>bold</b>"]', 'A["one<br/>two<br>three <b>bold</b>"]'),
    ("A --> B\nB <--> C\nC -.-> D\nD ==> E", "A --> B\nB <--> C\nC -.-> D\nD ==> E"),
    (
        "class Shape{\n  <<interface>>\n}\nShape <|-- Circle",
        "class Shape{\n  <<interface>>\n}\nShape <|-- Circle",
    ),
    ("Alice->>Bob: hi", "Alice->>Bob: hi"),
]


def test_mermaid_tag_like_text_is_escaped_not_dropped(page):
    page.wait_for_selector("#content .mermaid svg")
    for src, expected in ESCAPES:
        assert page.evaluate("escapeMermaidTags", src) == expected


RENDERED = """async (src) => {
    const box = document.createElement('div');
    box.className = 'mermaid';
    box.textContent = src;
    document.getElementById('content').appendChild(box);
    await renderMermaidDiagrams(document.getElementById('content'));
    const text = box.querySelector('.mermaid-error') ? 'ERROR' : box.querySelector('svg').textContent;
    box.remove();
    return text;
}"""


def test_angle_brackets_survive_into_the_diagram(page):
    page.wait_for_selector("#content .mermaid svg")
    text = page.evaluate(
        RENDERED,
        "flowchart TD\n A -->|contains <app-root>| B[Map<String, List<Integer>>]\n"
        ' B --> C["one<br/>two"]',
    )
    assert "contains <app-root>" in text
    assert "Map<String, List<Integer>>" in text
    assert "onetwo" in text  # <br/> stayed a line break, not literal text

    stereo = page.evaluate(
        RENDERED, "classDiagram\n class Shape{\n  <<interface>>\n }\n Shape <|-- Circle"
    )
    assert "interface" in stereo and "<interface>" not in stereo


# Through the real path a docs page takes: markdown -> renderMarkdown -> renderMermaidDiagrams.
FROM_MARKDOWN = """async (md) => {
    const box = document.createElement('div');
    box.innerHTML = renderMarkdown(md);
    document.getElementById('content').appendChild(box);
    await renderMermaidDiagrams(box);
    await new Promise(r => setTimeout(r, 300));  // let any injected <img> fire its onerror
    const svg = box.querySelector('svg');
    const text = svg ? svg.textContent : 'NO SVG';
    box.remove();
    return [text, window.__probe === 1];
}"""


def test_markdown_diagrams_keep_angle_brackets_and_run_no_html(page):
    page.wait_for_selector("#content .mermaid svg")
    md = (
        "```mermaid\nflowchart TD\n"
        "  A -->|contains <cpm-sdk-app-root>| B[Map<String, List<Integer>>]\n"
        "  B --> C[Read & write]\n"
        "  C --> D[<img src=x onerror=window.__probe=1>]\n```\n"
    )
    text, probe_ran = page.evaluate(FROM_MARKDOWN, md)
    assert "contains <cpm-sdk-app-root>" in text
    assert "Map<String, List<Integer>>" in text
    assert "Read & write" in text
    assert not probe_ran  # HTML inside a diagram is shown as text, never executed


# Which `codewiki serve` the site's chat may use when several repos are served (ports 8765+)
# and every GitHub Pages site of one user shares an origin.
OURS = """([url, health, base]) => {
    CONFIG.repository_url = url;
    return chatServerIsOurs({ base: base || 'http://127.0.0.1:8766', health: health });
}"""


def test_chat_only_uses_the_server_for_its_own_repo(page):
    gh = "https://github.com/acme/demo"
    cases = [
        ([gh, {"repo": "demo", "repository_url": gh + ".git"}], True),  # .git/case don't matter
        ([gh, {"repo": "demo", "repository_url": "https://github.com/acme/other"}], False),
        ([None, {"repo": "other", "repository_url": None}], False),  # no remote: compare names
        ([None, {"repo": "demo"}], True),
        ([gh, {"repo": "other"}], False),  # older server without repository_url
        ([gh, {"repo": "other", "repository_url": gh}, "SAME"], True),
    ]
    for args, expected in cases:
        if len(args) == 3:
            args[2] = page.evaluate("location.origin")  # opened from that server itself
        assert page.evaluate(OURS, args) is expected, args
