"""Tests for the viewer's section search index."""

import json

from codewiki.src.be.search_index import build_search_index, parse_sections

TREE = {
    "UI": {
        "components": [],
        "children": {"card": {"components": [], "children": {}}},
    },
}


def test_sections_split_at_every_heading_and_skip_code_and_tables():
    text = (
        "# Card\n\nIntro.\n\n## Inputs\n\n| Input | Type |\n|---|---|\n| `title` | string |\n\n"
        "```ts\n# not a heading\n```\n\n### Nested\n\nDeep text.\n"
    )
    sections = parse_sections(text)
    assert [(s.level, s.title) for s in sections] == [(1, "Card"), (2, "Inputs"), (3, "Nested")]
    assert sections[1].content == "Input Type title string"
    assert sections[2].content == "Deep text."


def test_anchors_follow_rendered_heading_text():
    sections = parse_sections(
        "## Styling the `card` with [themes](../UI.md#theming)\n\n## Setup\n\n## Setup\n"
    )
    assert [s.anchor for s in sections] == ["styling-the-card-with-themes", "setup", "setup-1"]


def test_nested_pages_are_indexed_with_their_docs_relative_path(tmp_path):
    (tmp_path / "module_tree.json").write_text(json.dumps(TREE), encoding="utf-8")
    (tmp_path / "overview.md").write_text("# Repo\n\nHello.\n", encoding="utf-8")
    (tmp_path / "UI.md").write_text("# UI\n\nWidgets.\n", encoding="utf-8")
    (tmp_path / "UI").mkdir()
    (tmp_path / "UI" / "card.md").write_text(
        "# Card\n\n## Change detection strategy\n\nUses `OnPush`.\n", encoding="utf-8"
    )
    # Markdown that is not a page (e.g. a report folder) stays out of the index
    (tmp_path / "validation_results").mkdir()
    (tmp_path / "validation_results" / "report.md").write_text("# Report\n", encoding="utf-8")

    index = build_search_index(str(tmp_path))
    ids = {record["id"] for record in index}
    assert "UI/card.md#change-detection-strategy" in ids
    assert {record["file"] for record in index} == {"overview.md", "UI.md", "UI/card.md"}
