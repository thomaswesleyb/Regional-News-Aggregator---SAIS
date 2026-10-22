"""Keyword-weight relevance scoring shared by articles and datasets."""

import re
from dataclasses import dataclass

from news_aggregator.config import Keywords


@dataclass(frozen=True)
class Match:
    """Why an item was judged relevant.

    Attributes:
        score: Sum of matched term weights (title hits multiplied).
        regions: Matched region names, strongest first.
        topics: Matched topic names, strongest first.
    """

    score: float
    regions: tuple[str, ...]
    topics: tuple[str, ...]


@dataclass(frozen=True)
class _Term:
    group: str
    weight: float
    pattern: re.Pattern[str]


def compile_term(term: str) -> re.Pattern[str]:
    """Compile a keyword into a whole-word, case-insensitive pattern.

    A trailing "*" matches any word ending, so "Serb*" matches "Serbian".
    """
    wildcard = term.endswith("*")
    body = re.escape(term.rstrip("*")).replace(r"\ ", r"\s+")
    suffix = r"\w*" if wildcard else ""
    return re.compile(rf"(?<!\w){body}{suffix}(?!\w)", re.IGNORECASE)


def _compile_groups(groups: dict[str, dict[str, float]]) -> list[_Term]:
    return [
        _Term(group, weight, compile_term(term))
        for group, terms in groups.items()
        for term, weight in terms.items()
    ]


def _ranked(scores: dict[str, float]) -> tuple[str, ...]:
    return tuple(sorted(scores, key=lambda name: (-scores[name], name)))


class Scorer:
    """Scores text against the configured keyword groups."""

    def __init__(self, keywords: Keywords) -> None:
        self._regions = _compile_groups(keywords.regions)
        self._topics = _compile_groups(keywords.topics)
        self._exclude = [compile_term(term) for term in keywords.exclude]
        self._threshold = keywords.scoring.threshold
        self._title_multiplier = keywords.scoring.title_multiplier

    def _score_terms(self, terms: list[_Term], title: str, body: str) -> dict[str, float]:
        scores: dict[str, float] = {}
        for term in terms:
            if term.pattern.search(title):
                points = term.weight * self._title_multiplier
            elif term.pattern.search(body):
                points = term.weight
            else:
                continue
            scores[term.group] = scores.get(term.group, 0.0) + points
        return scores

    def match(self, title: str, body: str, source_regions: tuple[str, ...] = ()) -> Match | None:
        """Return why the item is relevant, or None if it should be left out.

        Args:
            title: Headline or dataset title.
            body: Summary, description or article text.
            source_regions: Regions implied by the source; they satisfy the region
                requirement without adding to the score.
        """
        if any(pattern.search(title) for pattern in self._exclude):
            return None
        region_scores = self._score_terms(self._regions, title, body)
        topic_scores = self._score_terms(self._topics, title, body)
        score = sum(region_scores.values()) + sum(topic_scores.values())
        for region in source_regions:
            region_scores.setdefault(region, 0.0)
        if not region_scores or not topic_scores or score < self._threshold:
            return None
        return Match(score=score, regions=_ranked(region_scores), topics=_ranked(topic_scores))
