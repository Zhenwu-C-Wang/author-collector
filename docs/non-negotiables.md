# Non-Negotiables (v0)

These boundaries are mandatory in v0 unless explicitly changed through a documented version upgrade.

1. No full article body storage in exported data.
2. Robots checking is required.
3. Conservative crawl posture: low concurrency and per-domain delay.
4. No login bypass, no paywall bypass, no restricted-content scraping.
5. HTTP(S)-only fetching with SSRF protections.
6. Manual-first identity resolution (no automatic merge application).
7. Evidence-first extraction with traceable claim paths.

## Scope Clarification

- In-scope: URL discovery, metadata extraction, snippets, evidence links, export.
- Out-of-scope for v0: full-content redistribution, high-scale parallel crawling, autonomous identity merges.
