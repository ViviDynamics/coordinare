"""417: question dedup must use a symmetric overlap (a short new question is not
swallowed by a long answered one) and tokenise Unicode (CJK has no spaces)."""
from __future__ import annotations

from performer.workflows._text import matches_answered, normalize_tokens, token_overlap


def test_a_short_new_question_is_not_swallowed_by_a_long_answered_one():
    long_answered = "what did you decide about the release build for the friday train"
    assert token_overlap("build", long_answered) is False
    assert token_overlap("build", long_answered) == matches_answered("build", long_answered)


def test_near_identical_questions_still_match():
    assert token_overlap("audience", "which audience") is True


def test_symmetry_of_the_overlap_measure():
    """token_overlap(a, b) == token_overlap(b, a): the measure is symmetric."""
    a = "what is the deployment target"
    b = "what is the deployment target and when"
    assert token_overlap(a, b) == token_overlap(b, a)


def test_cjk_text_tokenises_per_character():
    assert normalize_tokens("数据流") == frozenset({"数", "据", "流"})


def test_cjk_questions_match_through_shared_characters():
    assert matches_answered("这个模块在哪？", "哪个模块？") is True  # noqa: RUF001


def test_unrelated_cjk_questions_do_not_match():
    assert matches_answered("这个模块在哪？", "测试环境何时部署？") is False  # noqa: RUF001


def test_cjk_punctuation_is_stripped():
    assert normalize_tokens("你好，世界。") == frozenset({"你", "好", "世", "界"})  # noqa: RUF001
