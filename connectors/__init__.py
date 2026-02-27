"""Connector implementations."""

from connectors.arxiv import ArxivDiscoverStage
from connectors.html_author_page import HtmlAuthorPageDiscoverStage
from connectors.rss import RssDiscoverStage

__all__ = ["RssDiscoverStage", "HtmlAuthorPageDiscoverStage", "ArxivDiscoverStage"]
