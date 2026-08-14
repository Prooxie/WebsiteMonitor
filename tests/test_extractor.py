"""Tests for CSS extraction and content normalization."""

from __future__ import annotations

import pytest

from webmonitor.core.extractor import ExtractionError, Extractor
from webmonitor.models import Normalization


def test_extracts_only_the_selected_element(sample_html: str) -> None:
    """Content outside the selector must not appear in the result."""
    result = Extractor().extract(sample_html, "div.content")

    assert result is not None
    assert "Product list" in result
    assert "Ignore me" not in result
    assert "Also ignore" not in result


def test_strips_scripts_styles_and_comments(sample_html: str) -> None:
    """Noise tags inside the selected subtree are removed before comparison."""
    result = Extractor().extract(sample_html, "div.content")

    assert result is not None
    assert "track(" not in result
    assert "a comment" not in result


def test_missing_selector_returns_none(sample_html: str) -> None:
    """A selector matching nothing yields ``None``, not an empty string.

    The distinction matters: the engine treats ``None`` as "maybe this page is
    client-rendered" and may retry through the browser.
    """
    assert Extractor().extract(sample_html, "div.nonexistent") is None


@pytest.mark.parametrize("selector", ["div..", "div::bogus-pseudo", ">>>", "div:nth-of-type("])
def test_invalid_selector_raises(sample_html: str, selector: str) -> None:
    """A malformed selector is reported rather than silently ignored.

    Args:
        sample_html: The document fixture.
        selector: A selector the CSS engine cannot parse.
    """
    with pytest.raises(ExtractionError):
        Extractor().extract(sample_html, selector)


def test_tolerated_bad_selector_degrades_to_no_match(sample_html: str) -> None:
    """Lexbor accepts some malformed selectors and simply matches nothing.

    ``div[unclosed`` is one such case. It must behave like any other selector
    miss rather than blowing up a check.
    """
    assert Extractor().extract(sample_html, "div[unclosed") is None


def test_empty_selector_falls_back_to_body(sample_html: str) -> None:
    """An empty selector watches the whole document body."""
    result = Extractor().extract(sample_html, "")

    assert result is not None
    assert "Ignore me" in result
    assert "Product list" in result


def test_ignore_numbers_neutralises_counters(sample_html: str) -> None:
    """Digit runs collapse to ``#`` so prices and counters stop causing alerts."""
    rules = Normalization(ignore_numbers=True)
    result = Extractor().extract(sample_html, "div.content", rules)

    assert result is not None
    assert "19.99" not in result
    assert "#" in result


def test_collapse_whitespace_makes_reformatting_invisible() -> None:
    """Reindented markup with identical text produces identical output."""
    extractor = Extractor()
    compact = "<div class='c'>Hello world</div>"
    spread = "<div class='c'>\n\n   Hello     world\n</div>"

    assert extractor.extract(compact, "div.c") == extractor.extract(spread, "div.c")


def test_text_only_ignores_markup_changes() -> None:
    """A class rename with unchanged text must not register as a change."""
    extractor = Extractor()
    before = '<div class="c"><span class="old-style">Same text</span></div>'
    after = '<div class="c"><span class="new-style">Same text</span></div>'

    assert extractor.extract(before, "div.c") == extractor.extract(after, "div.c")


def test_markup_mode_notices_attribute_changes() -> None:
    """With ``text_only`` off, attribute changes are visible again."""
    rules = Normalization(text_only=False)
    extractor = Extractor()
    before = '<div class="c"><a href="/one">Link</a></div>'
    after = '<div class="c"><a href="/two">Link</a></div>'

    assert extractor.extract(before, "div.c", rules) != extractor.extract(after, "div.c", rules)


def test_ignore_attributes_hides_them_again() -> None:
    """``ignore_attributes`` strips attributes from a markup comparison."""
    rules = Normalization(text_only=False, ignore_attributes=True)
    extractor = Extractor()
    before = '<div class="c"><a href="/one" data-x="1">Link</a></div>'
    after = '<div class="c"><a href="/two" data-x="2">Link</a></div>'

    assert extractor.extract(before, "div.c", rules) == extractor.extract(after, "div.c", rules)


def test_ignore_patterns_remove_site_specific_noise() -> None:
    """A custom regex strips content that would otherwise flap."""
    rules = Normalization(ignore_patterns=(r"session-[a-f0-9]+",))
    extractor = Extractor()
    before = "<div class='c'>Stable session-abc123 text</div>"
    after = "<div class='c'>Stable session-def456 text</div>"

    assert extractor.extract(before, "div.c", rules) == extractor.extract(after, "div.c", rules)


def test_invalid_ignore_pattern_is_skipped_not_fatal() -> None:
    """A malformed user regex is logged and skipped rather than crashing."""
    rules = Normalization(ignore_patterns=("[unclosed",))

    result = Extractor().extract("<div class='c'>text</div>", "div.c", rules)

    assert result == "text"


def test_multiple_matches_are_joined(sample_html: str) -> None:
    """A selector matching several elements concatenates them in order."""
    result = Extractor().extract(sample_html, "li")

    assert result is not None
    assert "Widget" in result
    assert "Gadget" in result


def test_count_matches_reports_selector_reach(sample_html: str) -> None:
    """``count_matches`` powers the inspector's match badge."""
    extractor = Extractor()

    assert extractor.count_matches(sample_html, "li") == 2
    assert extractor.count_matches(sample_html, "div.content") == 1
    assert extractor.count_matches(sample_html, "div.missing") == 0
    assert extractor.count_matches(sample_html, "div[bad") == 0


def test_preview_explains_a_missing_selector(sample_html: str) -> None:
    """The preview helper returns a readable message instead of ``None``."""
    assert "no match" in Extractor().preview(sample_html, "div.missing")


def test_preview_truncates_long_content() -> None:
    """Long previews are cut so the dialog stays readable."""
    html = f"<div class='c'>{'x' * 5000}</div>"

    preview = Extractor().preview(html, "div.c", max_chars=100)

    assert preview.endswith("...")
    assert len(preview) <= 110
