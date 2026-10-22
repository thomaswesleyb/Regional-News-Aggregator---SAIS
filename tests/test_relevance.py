import pytest
from hypothesis import given
from hypothesis import strategies as st

from news_aggregator.config import Keywords, Scoring
from news_aggregator.relevance import Scorer, compile_term


@pytest.mark.parametrize(
    ("term", "text", "matches"),
    [
        ("NATO", "nato summit", True),
        ("NATO", "SENATOR", False),
        ("Serb*", "Serbian voters", True),
        ("Serb*", "Serbs", True),
        ("Serb*", "Observe", False),
        ("Serbia", "Serbian", False),
        ("Western Balkans", "the western  Balkans summit", True),
        ("St. Petersburg", "St. Petersburg talks", True),
        ("St. Petersburg", "Stx Petersburg", False),
        ("Vučić", "President Vučić said", True),
        ("AfD", "AfD's leader", True),
    ],
)
def test_compile_term(term: str, text: str, matches: bool) -> None:
    assert bool(compile_term(term).search(text)) is matches


def test_requires_region_and_topic(keywords: Keywords) -> None:
    scorer = Scorer(keywords)
    assert scorer.match("German election", "") is None  # region only
    assert scorer.match("NATO inflation", "") is None  # topic only
    match = scorer.match("German military", "")
    assert match is not None
    assert match.regions == ("germany",)
    assert match.topics == ("security",)


def test_title_hits_count_more_than_body(keywords: Keywords) -> None:
    scorer = Scorer(keywords)
    in_title = scorer.match("German NATO", "")
    in_body = scorer.match("Headline", "German NATO")
    assert in_title is not None
    assert in_body is not None
    assert in_title.score == 12
    assert in_body.score == 6


def test_term_counts_once(keywords: Keywords) -> None:
    match = Scorer(keywords).match("x", "German German German NATO NATO")
    assert match is not None
    assert match.score == 6


def test_threshold_is_inclusive(keywords: Keywords) -> None:
    scorer = Scorer(keywords)
    # Berlin (2) + military (2) in body = 4 = threshold
    assert scorer.match("x", "Berlin military") is not None
    # Berlin (2) in body + nothing else that is a topic fails the topic rule
    assert scorer.match("x", "Berlin") is None


def test_below_threshold(keywords: Keywords) -> None:
    lowered = Keywords(
        scoring=keywords.scoring,
        regions={"germany": {"Berlin": 1}},
        topics={"security": {"military": 1}},
        exclude=(),
    )
    assert Scorer(lowered).match("x", "Berlin military") is None


def test_source_regions_satisfy_region_rule_without_scoring(keywords: Keywords) -> None:
    scorer = Scorer(keywords)
    match = scorer.match("NATO drills", "", source_regions=("balkans",))
    assert match is not None
    assert match.regions == ("balkans",)
    assert match.score == 6


def test_regions_ranked_by_strength(keywords: Keywords) -> None:
    match = Scorer(keywords).match("Serbian NATO", "Berlin", source_regions=("germany",))
    assert match is not None
    assert match.regions == ("balkans", "germany")


def test_exclude_title(keywords: Keywords) -> None:
    scorer = Scorer(keywords)
    assert scorer.match("German football military", "") is None
    assert scorer.match("German military", "football") is not None


WORDS = ["German", "Berlin", "Serbian", "NATO", "military", "inflation", "weather", "cat"]


@given(
    title=st.lists(st.sampled_from(WORDS), max_size=6),
    body=st.lists(st.sampled_from(WORDS), max_size=6),
    extra=st.sampled_from(WORDS),
)
def test_adding_words_never_lowers_score(title: list[str], body: list[str], extra: str) -> None:
    keywords = Keywords(
        scoring=Scoring(0.001, 2, 26, 10, 5),
        regions={"germany": {"German*": 3, "Berlin": 2}, "balkans": {"Serb*": 3}},
        topics={"security": {"NATO": 3, "military": 2}, "economy": {"inflation": 3}},
        exclude=(),
    )
    scorer = Scorer(keywords)
    before = scorer.match(" ".join(title), " ".join(body))
    after = scorer.match(" ".join(title), " ".join([*body, extra]))
    if before is not None:
        assert after is not None
        assert after.score >= before.score
