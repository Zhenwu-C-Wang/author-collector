"""Parser package for HTML + metadata extraction."""

from parser.html import HtmlParseStage
from parser.jsonld import extract_structured_metadata

__all__ = ["HtmlParseStage", "extract_structured_metadata"]

