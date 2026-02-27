"""Fetcher subsystem with robots, politeness, and HTTP safety checks."""

from fetcher.http import fetch_url
from fetcher.http import HttpFetchStage
from fetcher.logging import emit_event, emit_fetch_log
from fetcher.politeness import PolitenessController
from fetcher.robots import RobotsTxtChecker

__all__ = [
    "fetch_url",
    "HttpFetchStage",
    "emit_event",
    "emit_fetch_log",
    "PolitenessController",
    "RobotsTxtChecker",
]
