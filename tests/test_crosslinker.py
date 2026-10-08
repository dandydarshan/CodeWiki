"""Tests for the post-generation cross-linking pass."""

from types import SimpleNamespace

from codewiki.src.be.crosslinker import (
    RELATED_START,
    crosslink_docs,
    github_slug,
    parse_headings,
)

TREE = {
    "Engine": {
        "components": ["src/engine/core.py::SearchEngine", "src/engine/cfg.py::Config"],
        "children": {
            "Engine_index": {"components": ["src/engine/core.py::SearchEngine"], "children": {}},
        },
    },
    "Web_UI": {"components": ["src/web/app.py::render_page"], "children": {}},
}


def write_docs(tmp_path, pages):
    for stem, text in pages.items():
        (tmp_path / f"{stem}.md").write_text(text, encoding="utf-8")


def read(tmp_path, stem):
    return (tmp_path / f"{stem}.md").read_text(encoding="utf-8")


def base_pages(**overrides):
    pages = {
        "overview": "# Repo\n\nSee [Engine](Engine.md) and [Web UI](Web_UI.md).\n",
        "Engine": "# Engine\n\nIndexing lives in [Engine_index](Engine_index.md).\n\n"
        "## Config\n\nSettings object.\n",
        "Engine_index": "# Engine index\n\n## SearchEngine\n\nRanks documents.\n",
        "Web_UI": "# Web UI\n\n## render_page\n\nRenders HTML.\n",
    }
    pages.update(overrides)
    return pages


class TestSlugs:
    def test_github_slug(self):
        assert github_slug("1. `Node` (`models/core.py`)") == "1-node-modelscorepy"
        assert github_slug("Build & Deploy") == "build--deploy"

    def test_duplicate_headings_get_suffixes_and_fences_are_ignored(self):
        text = "# A\n## Usage\n```\n## Usage\n```\n## Usage\n"
        assert [s for _, _, s in parse_headings(text)] == ["a", "usage", "usage-1"]


class TestRepair:
    def test_absolute_path_and_name_variant_are_fixed(self, tmp_path):
        text = "# Web UI\n\n[a](/abs/docs/Engine.md) [b](engine_index.md)\n"
        write_docs(tmp_path, base_pages(Web_UI=text))
        report = crosslink_docs(str(tmp_path), TREE)
        out = read(tmp_path, "Web_UI")
        assert "[a](Engine.md)" in out and "[b](Engine_index.md)" in out
        assert report.links_repaired == 2

    def test_stale_anchor_dropped_and_dangling_link_unwrapped(self, tmp_path):
        text = "# Web UI\n\n[x](Engine.md#gone) [y](missing.md) [z](../src/app.py)\n"
        write_docs(tmp_path, base_pages(Web_UI=text))
        report = crosslink_docs(str(tmp_path), TREE)
        out = read(tmp_path, "Web_UI")
        assert "[x](Engine.md)" in out
        assert "missing.md" not in out and " y " in out
        assert "[z](../src/app.py)" in out
        assert report.links_removed == 1


class TestInlineLinks:
    def test_module_names_linked_once_per_section(self, tmp_path):
        text = (
            "# Repo\n\nThe Engine feeds the Web UI. The Engine again.\n\n"
            "## More\n\nEngine here too.\n"
        )
        write_docs(tmp_path, base_pages(overview=text))
        crosslink_docs(str(tmp_path), TREE)
        out = read(tmp_path, "overview")
        assert out.count("[Engine](Engine.md)") == 2
        assert "[Web UI](Web_UI.md)" in out
        assert "The Engine again" in out

    def test_code_headings_and_existing_links_untouched(self, tmp_path):
        text = "# Repo\n\n## Engine\n\n```\nEngine\n```\n\nUse [the engine](Engine.md). Engine.\n"
        write_docs(tmp_path, base_pages(overview=text))
        crosslink_docs(str(tmp_path), TREE)
        out = read(tmp_path, "overview")
        assert "## Engine\n" in out and "```\nEngine\n```" in out
        assert out.count("(Engine.md)") == 1

    def test_components_link_to_their_section(self, tmp_path):
        text = "# Web UI\n\nCalls SearchEngine and `Config` but Config in prose stays.\n"
        write_docs(tmp_path, base_pages(Web_UI=text))
        crosslink_docs(str(tmp_path), TREE)
        out = read(tmp_path, "Web_UI")
        assert "[SearchEngine](Engine_index.md#searchengine)" in out
        assert "[`Config`](Engine.md#config)" in out
        assert "but Config in prose" in out

    def test_page_never_links_itself(self, tmp_path):
        text = "# Web UI\n\nThe Web UI calls render_page.\n\n## render_page\n\nRenders.\n"
        write_docs(tmp_path, base_pages(Web_UI=text))
        crosslink_docs(str(tmp_path), TREE)
        out = read(tmp_path, "Web_UI")
        assert "(Web_UI.md)" not in out
        assert "[render_page](#render_page)" in out  # intro -> its own section

    def test_bare_filename_becomes_link(self, tmp_path):
        text = "# Web UI\n\nDetails in Engine_index.md.\n"
        write_docs(tmp_path, base_pages(Web_UI=text))
        crosslink_docs(str(tmp_path), TREE)
        assert "[Engine_index.md](Engine_index.md)." in read(tmp_path, "Web_UI")


class TestRelatedPages:
    def test_missing_structural_links_are_appended(self, tmp_path):
        pages = base_pages(overview="# Repo\n\nNothing linked.\n", Engine="# Engine\n\nText.\n")
        write_docs(tmp_path, pages)
        crosslink_docs(str(tmp_path), TREE)
        overview = read(tmp_path, "overview")
        assert RELATED_START in overview
        assert "- **Modules:** [Engine](Engine.md), [Web UI](Web_UI.md)" in overview
        assert "- **Sub-modules:** [Engine index](Engine_index.md)" in read(tmp_path, "Engine")
        assert "- **Parent:** [Engine](Engine.md)" in read(tmp_path, "Engine_index")

    def test_dependency_neighbours_from_graph(self, tmp_path):
        write_docs(tmp_path, base_pages())
        components = {
            "src/web/app.py::render_page": SimpleNamespace(
                depends_on={"src/engine/core.py::SearchEngine"}, component_type="function"
            ),
        }
        crosslink_docs(str(tmp_path), TREE, components)
        assert "- **Depends on:** [Engine index](Engine_index.md)" in read(tmp_path, "Web_UI")
        assert "- **Used by:** [Web UI](Web_UI.md)" in read(tmp_path, "Engine_index")

    def test_rerun_is_idempotent_and_block_drops_when_satisfied(self, tmp_path):
        write_docs(tmp_path, base_pages(overview="# Repo\n\nNothing linked.\n"))
        crosslink_docs(str(tmp_path), TREE)
        first = {p.name: p.read_text() for p in tmp_path.glob("*.md")}
        report = crosslink_docs(str(tmp_path), TREE)
        assert report.pages_changed == [] and report.related_added == 0
        assert first == {p.name: p.read_text() for p in tmp_path.glob("*.md")}

        (tmp_path / "overview.md").write_text(
            read(tmp_path, "overview").replace("Nothing linked.", "See Engine and Web_UI.")
        )
        crosslink_docs(str(tmp_path), TREE)
        assert RELATED_START not in read(tmp_path, "overview")


class TestHierarchicalLayout:
    def write_nested(self, tmp_path):
        pages = base_pages(
            Engine="# Engine\n\nIndexing lives in [Engine_index](Engine/Engine_index.md).\n\n"
            "## Config\n\nSettings object.\n",
            Engine_index="# Engine index\n\n## SearchEngine\n\nRanks documents for Web_UI.\n",
            Web_UI="# Web UI\n\n## render_page\n\nCalls SearchEngine.\n",
        )
        (tmp_path / "Engine").mkdir()
        rel = {"Engine_index": "Engine/Engine_index.md"}
        for stem, text in pages.items():
            (tmp_path / rel.get(stem, f"{stem}.md")).write_text(text, encoding="utf-8")

    def test_nested_pages_are_linked_with_relative_paths(self, tmp_path):
        self.write_nested(tmp_path)
        report = crosslink_docs(str(tmp_path), TREE)
        assert report.links_removed == 0
        assert not (tmp_path / "Engine_index.md").exists()
        engine = read(tmp_path, "Engine")
        index = (tmp_path / "Engine" / "Engine_index.md").read_text(encoding="utf-8")
        web = read(tmp_path, "Web_UI")
        assert "[Engine_index](Engine/Engine_index.md)" in engine
        assert "[Web_UI](../Web_UI.md)" in index
        assert "- **Parent:** [Engine](../Engine.md)" in index
        assert "[SearchEngine](Engine/Engine_index.md#searchengine)" in web

    def test_broken_nested_link_is_repaired_and_rerun_is_idempotent(self, tmp_path):
        self.write_nested(tmp_path)
        (tmp_path / "Engine.md").write_text(
            "# Engine\n\nIndexing lives in [index](Engine_index.md).\n", encoding="utf-8"
        )
        report = crosslink_docs(str(tmp_path), TREE)
        assert report.links_repaired == 1
        assert "[index](Engine/Engine_index.md)" in read(tmp_path, "Engine")
        again = crosslink_docs(str(tmp_path), TREE)
        assert again.pages_changed == []


def test_generator_respects_flag_and_never_raises(tmp_path, monkeypatch):
    from codewiki.src.be import documentation_generator as dg

    gen = dg.DocumentationGenerator.__new__(dg.DocumentationGenerator)
    gen.config = SimpleNamespace(crosslinks_enabled=False)
    assert gen.crosslink_documentation(str(tmp_path)) is None

    gen.config = SimpleNamespace(crosslinks_enabled=True)

    def boom(*a, **k):
        raise RuntimeError("bad markdown")

    monkeypatch.setattr(dg, "crosslink_docs", boom)
    assert gen.crosslink_documentation(str(tmp_path)) is None
