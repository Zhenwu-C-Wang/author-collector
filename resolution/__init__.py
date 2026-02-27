"""Identity-resolution utilities."""

from resolution.scoring import (
    Candidate,
    ReviewAuthor,
    build_candidates,
    normalized_levenshtein_distance,
    score_candidate_pair,
)

__all__ = [
    "ReviewAuthor",
    "Candidate",
    "build_candidates",
    "score_candidate_pair",
    "normalized_levenshtein_distance",
]
