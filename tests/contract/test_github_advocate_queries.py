"""Contract tests for GitHub advocate GraphQL queries/mutations (007, T029)."""
from __future__ import annotations

import coordinare.services.github as github_service


def test_list_open_issues_query_exists() -> None:
    assert "ListOpenIssues" in github_service.LIST_OPEN_ISSUES_QUERY


def test_get_file_content_query_exists() -> None:
    assert "GetFileContent" in github_service.GET_FILE_CONTENT_QUERY


def test_get_repository_id_query_exists() -> None:
    assert "GetRepositoryId" in github_service.GET_REPOSITORY_ID_QUERY


def test_get_label_ids_query_exists() -> None:
    assert "GetLabelIds" in github_service.GET_LABEL_IDS_QUERY


def test_create_label_mutation_exists() -> None:
    assert "CreateLabel" in github_service.CREATE_LABEL_MUTATION


def test_add_labels_mutation_exists() -> None:
    assert "AddLabels" in github_service.ADD_LABELS_MUTATION


def test_list_open_issues_query_has_required_fields() -> None:
    query = github_service.LIST_OPEN_ISSUES_QUERY
    assert "states: [OPEN]" in query
    assert "title" in query
    assert "body" in query
    assert "labels" in query


def test_add_labels_mutation_uses_labelable_id() -> None:
    mutation = github_service.ADD_LABELS_MUTATION
    assert "labelableId" in mutation
    assert "labelIds" in mutation


def test_poll_board_query_includes_labels() -> None:
    """Ensure POLL_BOARD_QUERY fetches labels for advocate dedup (FR-001a)."""
    assert "labels" in github_service.POLL_BOARD_QUERY
    assert "nodes" in github_service.POLL_BOARD_QUERY
