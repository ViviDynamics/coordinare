"""nare-native-034: two reviewers of a divided manifest filed F0010 and F0011 for the same install-aws launcher (same
category and affected ids, different words), and the author answered both. Within one review they are one finding."""
from __future__ import annotations

from coordinare_service_inference.environment.findings import TEXT_BYTES, FindingProposal

from coordinare.services.environment_findings import ALSO_REPORTED, merge_duplicate_findings


def finding(issue: str, ids: list[str], sources: list[str], category: str = 'execution_definition') -> FindingProposal:
    return FindingProposal.model_validate({'category': category, 'issue': issue, 'sources': sources, 'affected_ids': ids})


def test_findings_with_the_same_category_and_ids_merge_keeping_every_text_and_source() -> None:
    first = finding('launcher shebang is absolute', ['install-aws', 'c00-aws-cli'], ['README.md'])
    second = finding('venv is not relocatable', ['c00-aws-cli', 'install-aws'], ['README.md', 'docs/x.md'])
    other = finding('replay needs --yes', ['c00-replay'], ['README.md'])
    merged = merge_duplicate_findings([first, other, second])
    assert len(merged) == 2 and merged[1] == other
    assert merged[0].issue == 'launcher shebang is absolute' + ALSO_REPORTED + 'venv is not relocatable'
    assert merged[0].sources == ['README.md', 'docs/x.md']


def test_another_category_another_id_set_or_no_ids_never_merge() -> None:
    items = [finding('a', ['x'], ['README.md']), finding('b', ['x'], ['README.md'], category='coverage'),
             finding('c', ['x', 'y'], ['README.md']), finding('d', [], ['README.md']), finding('e', [], ['README.md'])]
    assert merge_duplicate_findings(items) == items


def test_a_merged_text_stays_within_the_finding_bound() -> None:
    long = 'x' * (TEXT_BYTES - 10)
    merged = merge_duplicate_findings([finding(long, ['x'], ['README.md']), finding('more detail', ['x'], ['README.md'])])
    assert len(merged) == 1 and len(merged[0].issue.encode()) <= TEXT_BYTES
    FindingProposal.model_validate(merged[0].model_dump(mode='json'))
