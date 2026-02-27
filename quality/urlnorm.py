"""URL canonicalization and dedup-key normalization."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_REMOVABLE_QUERY_PARAMS = {
    "session",
    "sessionid",
    "sid",
    "phpsessid",
    "jsessionid",
}


def canonicalize_url(url: str) -> str:
    """
    Canonicalize URL for stable deduplication.

    Rules (v0):
    - Lowercase host and path
    - Remove fragment
    - Sort query params
    - Drop `utm_*` and common session-id params
    - Prefer https over http
    """
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        return url

    scheme = "https"
    hostname = (parsed.hostname or "").lower()

    # Preserve explicit non-default port.
    if parsed.port:
        default_port = 80 if parsed.scheme.lower() == "http" else 443
        netloc = hostname if parsed.port == default_port else f"{hostname}:{parsed.port}"
    else:
        netloc = hostname

    path = (parsed.path or "/").lower()
    if not path.startswith("/"):
        path = "/" + path

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filtered_pairs = []
    for key, value in query_pairs:
        key_lower = key.lower()
        if key_lower.startswith("utm_"):
            continue
        if key_lower in _REMOVABLE_QUERY_PARAMS:
            continue
        filtered_pairs.append((key, value))
    filtered_pairs.sort(key=lambda item: (item[0], item[1]))
    query = urlencode(filtered_pairs, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))

