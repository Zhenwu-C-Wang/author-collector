"""
Default compliance configuration for author-collector.

These settings are IMMUTABLE in v0 and intended to prevent accidental
misuse (over-crawling, SSRF, legal issues, etc.).

Design: Everything defaults to "safe + slow" mode. Speed is a non-goal for v0.

In future versions, some settings may become configurable, but only with
explicit acknowledgment of the risks.
"""

from typing import Set, Tuple


class ComplianceConfig:
    """
    Immutable compliance settings for v0.

    Rationale for each setting can be found in docs/compliance.md.
    """

    # ========================================================================
    # Fetch-Layer Constraints (Non-negotiable)
    # ========================================================================

    # Global concurrency: max # of simultaneous fetches across all domains
    # v0: 1 (completely serial, safest)
    MAX_GLOBAL_CONCURRENCY: int = 1
    """Max concurrent fetches. v0 = serial (no parallelism)."""

    # Per-domain rate limiting: minimum gap between requests to same domain
    # v0: 5 seconds (very conservative)
    PER_DOMAIN_DELAY_SECONDS: float = 5.0
    """Minimum seconds between requests to same domain."""

    # Robots.txt enforcement: REQUIRED, cannot be disabled
    ROBOTS_CHECK_REQUIRED: bool = True
    """Robots.txt must be checked. Cannot be disabled."""

    # Protocol whitelist: only http(s), no file://, gopher, etc.
    ALLOWED_PROTOCOLS: Set[str] = {"http", "https"}
    """Only HTTP(S) allowed."""

    # IP blocklist: private/internal IPs cannot be fetched (SSRF prevention)
    BLOCKED_IP_RANGES: list[str] = [
        "127.0.0.1/8",          # Loopback
        "10.0.0.0/8",           # Private
        "172.16.0.0/12",        # Private
        "192.168.0.0/16",       # Private
        "169.254.0.0/16",       # Link-local
        "224.0.0.0/4",          # Multicast
        "255.255.255.255/32",   # Broadcast
        "0.0.0.0/8",            # This network
        "::1/128",              # IPv6 loopback
        "fe80::/10",            # IPv6 link-local
        "fc00::/7",             # IPv6 private
    ]
    """IP ranges that cannot be fetched (SSRF prevention)."""

    # Maximum number of redirects per fetch
    MAX_REDIRECTS: int = 5
    """Maximum redirect hops per fetch."""

    # Fetch timeout
    FETCH_TIMEOUT_SECONDS: int = 30
    """Maximum time to wait for a single fetch (seconds)."""

    # Maximum body size (memory safety)
    MAX_BODY_BYTES: int = 10_000_000  # 10 MB
    """Maximum bytes to download per fetch."""

    # User-Agent (must be descriptive + link to docs)
    USER_AGENT: str = "author-collector/0.1 (+https://github.com/anthropics/author-collector)"
    """User-Agent header (must be descriptive)."""

    # ========================================================================
    # Content Constraints (Non-negotiable)
    # ========================================================================

    # Snippet maximum length (no full text storage)
    SNIPPET_MAX_CHARS: int = 5000
    """Maximum snippet length (no full article body)."""

    # Don't store full body text (compliance boundary)
    STORE_FULL_BODY: bool = False
    """Never store full article text. This is a hard boundary."""

    # ========================================================================
    # Disabled Features (for v0)
    # ========================================================================

    # No automatic author merging in v0 (manual review only)
    AUTO_MERGE_ENABLED: bool = False
    """Auto-merge disabled in v0. All merges must be manual review."""

    # No ML/LLM for evidence scoring in v0
    LLM_ENABLED: bool = False
    """LLM/ML disabled in v0 (depends on LLM, risky for compliance)."""

    # ========================================================================
    # Optional Features (safe defaults)
    # ========================================================================

    # PII scrubbing in snippets (default: off, but safe to enable)
    PII_SCRUBBING_ENABLED: bool = False
    """PII scrubbing in snippets. Optional, disabled by default in v0."""

    # Cache robots.txt in-memory (recommended on)
    ROBOTS_CACHE_ENABLED: bool = True
    """Cache robots.txt results."""

    # ========================================================================
    # Connector Constraints
    # ========================================================================

    # Disabled connectors (can be overridden per-run by --enable)
    DISABLED_CONNECTORS: list[str] = [
        # "playwright",     # No browser automation (too risky)
        # "selenium",       # No browser automation (too risky)
    ]
    """List of connector types that are never allowed."""

    # List of domains to never fetch from
    BLOCKED_DOMAINS: list[str] = []
    """Hardcoded domain blocklist (e.g., internal staging servers)."""

    # Maximum URLs per discovery run (per connector per run)
    # This prevents "accidentally crawling entire site"
    MAX_URLS_PER_RUN: int = 10000
    """Max URLs discovered per run (safety valve)."""

    @classmethod
    def validate(cls) -> None:
        """
        Validate configuration at startup.

        Raises:
            ValueError: If any constraint is violated.
        """
        assert (
            cls.MAX_GLOBAL_CONCURRENCY >= 1
        ), "MAX_GLOBAL_CONCURRENCY must be ≥1"

        assert (
            cls.PER_DOMAIN_DELAY_SECONDS >= 0
        ), "PER_DOMAIN_DELAY_SECONDS must be ≥0"

        assert (
            cls.ROBOTS_CHECK_REQUIRED is True
        ), "ROBOTS_CHECK_REQUIRED must be True in v0"

        assert (
            cls.STORE_FULL_BODY is False
        ), "STORE_FULL_BODY must be False (compliance)"

        assert (
            cls.AUTO_MERGE_ENABLED is False
        ), "AUTO_MERGE_ENABLED must be False in v0"

        assert (
            cls.SNIPPET_MAX_CHARS > 0
        ), "SNIPPET_MAX_CHARS must be > 0"

        assert (
            cls.MAX_BODY_BYTES > 0
        ), "MAX_BODY_BYTES must be > 0"


# Validate at module import time
ComplianceConfig.validate()


# ============================================================================
# Example usage:
# ============================================================================
#
# from core.config import ComplianceConfig
#
# # Check if feature is allowed
# if not ComplianceConfig.AUTO_MERGE_ENABLED:
#     print("Auto-merge is disabled in v0")
#
# # Use a setting
# timeout = ComplianceConfig.FETCH_TIMEOUT_SECONDS
#
# # All settings are read-only (no setters) — if you need different values,
# # that's a signal you need to reconsider the design or get explicit approval.
