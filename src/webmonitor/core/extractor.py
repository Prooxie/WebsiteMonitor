"""Element extraction and content normalization.

Parsing runs on selectolax's Lexbor backend - a C HTML5 parser that is roughly
an order of magnitude faster than BeautifulSoup with ``html.parser`` and, unlike
regex-based approaches, actually implements the HTML5 tree construction rules.

Normalization is where false positives are won or lost. A naive monitor that
hashes raw markup will fire on every rotating ad id, CSRF token and "1,234
views" counter. The rules in :class:`~webmonitor.models.Normalization` strip
those out before the hash is taken.
"""

from __future__ import annotations

import logging
import re
from typing import Final

from selectolax.lexbor import LexborHTMLParser, LexborNode

from webmonitor.models import Normalization

__all__ = ["ExtractionError", "Extractor"]

logger = logging.getLogger(__name__)

#: Tags whose contents are never meaningful page content.
NOISE_TAGS: Final[tuple[str, ...]] = ("script", "style", "noscript", "template", "svg")

_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"\d+(?:[.,]\d+)*")
_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->", re.DOTALL)
# Collapses `<div class="x" id="y">` to `<div>` while leaving `</div>` and
# `<br/>` intact. Operating on already-parsed, serialised markup rather than
# arbitrary input keeps this well-behaved.
_ATTRS_RE: Final[re.Pattern[str]] = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)\s+[^>]*?(/?)>")


class ExtractionError(Exception):
    """The document could not be parsed, or the selector was invalid."""


class Extractor:
    """Turns raw HTML plus a CSS selector into a stable, comparable string."""

    def extract(
        self,
        html: str,
        selector: str = "",
        normalization: Normalization | None = None,
    ) -> str | None:
        """Extract the selected content and normalize it for comparison.

        Args:
            html: The full document source.
            selector: CSS selector identifying the watched element(s). An empty
                selector means the whole ``<body>``.
            normalization: Cleanup rules. Defaults to
                :class:`~webmonitor.models.Normalization` defaults.

        Returns:
            The normalized content, or ``None`` when the selector matched
            nothing. ``None`` is meaningful: it usually means either the page
            layout changed or the content is rendered client-side, and the
            engine uses it to decide whether to retry through a real browser.

        Raises:
            ExtractionError: If the document cannot be parsed or the selector is
                rejected by the CSS engine.
        """
        rules = normalization or Normalization()

        try:
            tree = LexborHTMLParser(html)
        except Exception as exc:  # pragma: no cover - lexbor is very tolerant
            raise ExtractionError(f"Could not parse HTML: {exc}") from exc

        if rules.strip_scripts:
            # Done on the tree, before selection, so noise inside the selected
            # subtree is removed too.
            try:
                tree.strip_tags(list(NOISE_TAGS))
            except Exception:
                logger.debug("strip_tags failed; continuing with full tree", exc_info=True)

        nodes = self._select(tree, selector)
        if not nodes:
            logger.debug("Selector %r matched no elements", selector)
            return None

        parts = [self._node_content(node, rules) for node in nodes]
        return self._normalize("\n".join(part for part in parts if part), rules)

    @staticmethod
    def _select(tree: LexborHTMLParser, selector: str) -> list[LexborNode]:
        """Run a CSS selector against a parsed document.

        Args:
            tree: The parsed document.
            selector: CSS selector, or empty for the document body.

        Returns:
            Matching nodes, possibly empty.

        Raises:
            ExtractionError: If the CSS engine rejects the selector.
        """
        if not selector.strip():
            body = tree.body or tree.root
            return [body] if body is not None else []

        try:
            return list(tree.css(selector))
        except Exception as exc:
            raise ExtractionError(f"Invalid CSS selector {selector!r}: {exc}") from exc

    @staticmethod
    def _node_content(node: LexborNode, rules: Normalization) -> str:
        """Serialise one node according to the normalization rules.

        Args:
            node: The node to serialise.
            rules: Cleanup rules.

        Returns:
            The node's text or markup, before textual normalization.
        """
        if rules.text_only:
            return node.text(deep=True, separator=" ", strip=True) or ""
        return node.html or ""

    @staticmethod
    def _normalize(content: str, rules: Normalization) -> str:
        """Apply the textual normalization rules.

        Args:
            content: Extracted content.
            rules: Cleanup rules.

        Returns:
            The normalized string that will be hashed and diffed.
        """
        if not content:
            return ""

        if rules.strip_scripts:
            content = _COMMENT_RE.sub("", content)

        if rules.ignore_attributes and not rules.text_only:
            content = _ATTRS_RE.sub(r"<\1\2>", content)

        for pattern in rules.ignore_patterns:
            try:
                content = re.sub(pattern, "", content)
            except re.error:
                logger.warning("Skipping invalid ignore pattern %r", pattern)

        if rules.ignore_numbers:
            content = _NUMBER_RE.sub("#", content)

        if rules.collapse_whitespace:
            content = _WHITESPACE_RE.sub(" ", content).strip()

        return content

    def preview(self, html: str, selector: str, *, max_chars: int = 600) -> str:
        """Produce a short human-readable preview of what a selector captures.

        Used by the inspector so the user can confirm they picked the right
        element before saving the target.

        Args:
            html: The full document source.
            selector: CSS selector to evaluate.
            max_chars: Truncation limit for the returned text.

        Returns:
            The matched text, truncated with an ellipsis, or an explanatory
            message when nothing matched or the selector was invalid.
        """
        try:
            content = self.extract(html, selector, Normalization())
        except ExtractionError as exc:
            return f"[invalid selector] {exc}"

        if content is None:
            return "[no match] This selector does not match any element on the page."
        if not content.strip():
            return "[empty] The element matched but contains no text."
        if len(content) > max_chars:
            return f"{content[:max_chars]}..."
        return content

    def count_matches(self, html: str, selector: str) -> int:
        """Count how many elements a selector matches.

        Args:
            html: The full document source.
            selector: CSS selector to evaluate.

        Returns:
            The number of matches, or ``0`` if the selector is invalid.
        """
        try:
            tree = LexborHTMLParser(html)
            return len(self._select(tree, selector))
        except ExtractionError:
            return 0
