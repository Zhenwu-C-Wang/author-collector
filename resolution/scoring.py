"""Rule-based candidate scoring for manual author-merge review (v0)."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5


def _normalize_name(value: str) -> str:
    """Normalize author names for robust comparison."""
    return " ".join(value.strip().lower().split())


def _levenshtein_distance(left: str, right: str) -> int:
    """Compute classic Levenshtein edit distance in O(m*n)."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, left_ch in enumerate(left, start=1):
        current = [i]
        for j, right_ch in enumerate(right, start=1):
            insertion = current[j - 1] + 1
            deletion = previous[j] + 1
            substitution = previous[j - 1] + (0 if left_ch == right_ch else 1)
            current.append(min(insertion, deletion, substitution))
        previous = current
    return previous[-1]


def normalized_levenshtein_distance(left: str, right: str) -> float:
    """Compute normalized Levenshtein distance using max length denominator."""
    normalized_left = _normalize_name(left)
    normalized_right = _normalize_name(right)
    if not normalized_left and not normalized_right:
        return 0.0
    denominator = max(len(normalized_left), len(normalized_right), 1)
    return _levenshtein_distance(normalized_left, normalized_right) / denominator


@dataclass(frozen=True)
class ReviewAuthor:
    """Author profile used in candidate generation."""

    id: str
    canonical_name: str
    source_id: str
    domains: tuple[str, ...]
    accounts: tuple[str, ...]
    profile_urls: tuple[str, ...]

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "ReviewAuthor":
        """Create a ReviewAuthor from a plain mapping."""
        return cls(
            id=str(payload["id"]),
            canonical_name=str(payload["canonical_name"]),
            source_id=str(payload.get("source_id", "")),
            domains=tuple(sorted({str(item).strip().lower() for item in payload.get("domains", []) if item})),
            accounts=tuple(sorted({str(item).strip().lower() for item in payload.get("accounts", []) if item})),
            profile_urls=tuple(sorted({str(item).strip() for item in payload.get("profile_urls", []) if item})),
        )

    @property
    def normalized_name(self) -> str:
        """Lowercased, whitespace-normalized name."""
        return _normalize_name(self.canonical_name)

    @property
    def profile_domains(self) -> set[str]:
        """Domains that appear in profile URLs."""
        domains: set[str] = set()
        for url in self.profile_urls:
            lower = url.lower()
            if "://" not in lower:
                continue
            host = lower.split("://", 1)[1].split("/", 1)[0].strip()
            if host:
                domains.add(host)
        return domains

    def to_dict(self) -> dict[str, Any]:
        """Serialize into queue JSON shape."""
        return {
            "id": self.id,
            "canonical_name": self.canonical_name,
            "source_id": self.source_id,
            "domains": list(self.domains),
            "accounts": list(self.accounts),
            "profile_urls": list(self.profile_urls),
        }


@dataclass(frozen=True)
class Candidate:
    """One human-review merge candidate."""

    id: str
    from_author: ReviewAuthor
    to_author: ReviewAuthor
    score: float
    scoring_breakdown: dict[str, float]
    evidence: list[str]

    @property
    def confidence(self) -> str:
        """Bucketized confidence label for UX."""
        if self.score >= 0.75:
            return "HIGH"
        if self.score >= 0.5:
            return "MEDIUM"
        return "LOW"

    def to_dict(self) -> dict[str, Any]:
        """Serialize candidate as review.json object."""
        return {
            "id": self.id,
            "from_author": self.from_author.to_dict(),
            "to_author": self.to_author.to_dict(),
            "score": round(self.score, 4),
            "confidence": self.confidence,
            "scoring_breakdown": self.scoring_breakdown,
            "evidence": self.evidence,
            "decision": None,
        }


def score_candidate_pair(left: ReviewAuthor, right: ReviewAuthor) -> Candidate | None:
    """
    Score one author pair using M5 v0 rules.

    Rules are cumulative and capped at 1.0.
    """
    if left.id == right.id:
        return None

    breakdown: dict[str, float] = {}
    evidence: list[str] = []
    score = 0.0

    shared_accounts = sorted(set(left.accounts).intersection(right.accounts))
    shared_domains = sorted(set(left.domains).intersection(right.domains))

    # Rule 1: Exact account match (strongest signal).
    if shared_accounts:
        breakdown["rule_1_exact_account_match"] = 1.0
        evidence.append(f"exact account match: {', '.join(shared_accounts)}")
        score += 1.0

    # Rule 2: Shared domain and both have explicit profile links on that domain.
    left_profile_domains = left.profile_domains
    right_profile_domains = right.profile_domains
    profile_domains = sorted(set(shared_domains).intersection(left_profile_domains, right_profile_domains))
    if profile_domains:
        breakdown["rule_2_same_domain_profile_link"] = 0.9
        evidence.append(f"profile links on shared domain: {', '.join(profile_domains)}")
        score += 0.9

    normalized_left = left.normalized_name
    normalized_right = right.normalized_name

    # Rule 3: Exact normalized name + shared domain.
    if shared_domains and normalized_left and normalized_left == normalized_right:
        breakdown["rule_3_exact_name_same_domain"] = 0.8
        evidence.append(f"exact name match on shared domain: {', '.join(shared_domains)}")
        score += 0.8

    # Rule 4: Similar name (distance <= 0.15) + shared domain.
    if shared_domains and normalized_left and normalized_right and normalized_left != normalized_right:
        distance = normalized_levenshtein_distance(normalized_left, normalized_right)
        if distance <= 0.15:
            breakdown["rule_4_similar_name_same_domain"] = 0.6
            evidence.append(
                f"similar names on shared domain ({distance:.3f}): {', '.join(shared_domains)}"
            )
            score += 0.6

    # Rule 5: Shared domain only (weak), only when no stronger same-domain name rule fired.
    if (
        shared_domains
        and "rule_3_exact_name_same_domain" not in breakdown
        and "rule_4_similar_name_same_domain" not in breakdown
    ):
        breakdown["rule_5_same_domain_only"] = 0.3
        evidence.append(f"shared publishing domain: {', '.join(shared_domains)}")
        score += 0.3

    score = min(score, 1.0)
    if score < 0.5:
        return None

    candidate_id = str(uuid5(NAMESPACE_URL, f"candidate|{left.id}|{right.id}"))
    return Candidate(
        id=candidate_id,
        from_author=left,
        to_author=right,
        score=score,
        scoring_breakdown=breakdown,
        evidence=evidence,
    )


def build_candidates(
    author_profiles: Iterable[dict[str, Any] | ReviewAuthor],
    min_score: float = 0.6,
) -> list[Candidate]:
    """Build scored candidates sorted by score DESC then deterministic id."""
    authors: list[ReviewAuthor] = []
    for profile in author_profiles:
        if isinstance(profile, ReviewAuthor):
            authors.append(profile)
        else:
            authors.append(ReviewAuthor.from_mapping(profile))
    authors.sort(key=lambda item: item.id)

    candidates: list[Candidate] = []
    for left, right in combinations(authors, 2):
        candidate = score_candidate_pair(left, right)
        if candidate and candidate.score >= min_score:
            candidates.append(candidate)

    candidates.sort(key=lambda item: (-item.score, item.id))
    return candidates
