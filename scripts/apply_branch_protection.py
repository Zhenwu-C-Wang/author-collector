#!/usr/bin/env python3
"""Apply branch protection rules for the main branch via GitHub REST API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Apply branch protection rules.")
    parser.add_argument("--owner", required=True, help="GitHub repository owner")
    parser.add_argument("--repo", required=True, help="GitHub repository name")
    parser.add_argument("--branch", default="main", help="Branch name (default: main)")
    parser.add_argument(
        "--checks",
        nargs="+",
        default=["lint", "test"],
        help="Required CI status check contexts",
    )
    parser.add_argument(
        "--approvals",
        type=int,
        default=1,
        help="Required approving review count",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print payload without calling GitHub API",
    )
    return parser.parse_args(argv)


def build_payload(checks: list[str], approvals: int) -> dict[str, object]:
    """Build GitHub branch protection payload."""
    return {
        "required_status_checks": {
            "strict": True,
            "contexts": checks,
        },
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
            "required_approving_review_count": approvals,
        },
        "restrictions": None,
        "required_conversation_resolution": True,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "block_creations": False,
        "required_linear_history": False,
        "lock_branch": False,
        "allow_fork_syncing": True,
    }


def main(argv: list[str] | None = None) -> int:
    """Apply branch protection using `GITHUB_TOKEN`."""
    args = parse_args(argv)
    payload = build_payload(args.checks, args.approvals)
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    if args.dry_run:
        print("dry-run: no API call made")
        return 0

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("error: GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    url = (
        f"https://api.github.com/repos/{args.owner}/{args.repo}/branches/{args.branch}/protection"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="PUT",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request) as response:
            body = response.read().decode("utf-8", errors="replace")
            print(body)
            return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(body, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
