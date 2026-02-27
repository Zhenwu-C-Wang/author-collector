# Compliance Rationale

`author-collector` is designed as an indexing system, not a bulk content downloader.

## Why We Store Metadata + Snippets Only

- Reduces legal and copyright risk compared to full-text mirroring.
- Keeps the project aligned with research/analysis and personal knowledge use cases.
- Makes downstream exports easier to audit and govern.

## Why Robots Is Mandatory

- `robots.txt` is treated as a hard compliance boundary in v0.
- Fetch behavior is intentionally conservative: low concurrency, per-domain delay, bounded redirects/timeouts.
- The default policy is to avoid aggressive crawling behavior.

## Why Evidence-First Matters

- Every claim should be tied to source evidence (`source_url`, `claim_path`, extracted snippet).
- Evidence enables reproducibility, audits, and safer human review.
- Identity resolution is manual-first in v0 to avoid irreversible merge mistakes.

## Security Posture in v0

- HTTP(S) protocol allowlist.
- Private/link-local IP range blocking (IPv4 + IPv6) to reduce SSRF risk.
- Request timeout, body size limits, and redirect limits.

## Operational Principle

If extraction fails, the system should degrade gracefully to index-level output (URL + minimal metadata) rather than fail unsafely.
