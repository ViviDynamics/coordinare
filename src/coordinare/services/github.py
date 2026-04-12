from __future__ import annotations

import asyncio
import inspect
from typing import Any, ClassVar

import aiohttp
import stamina
from gql import Client, gql
from gql.transport.aiohttp import AIOHTTPTransport

# ---------------------------------------------------------------------------
# Exception taxonomy (T008)
# ---------------------------------------------------------------------------


class GitHubError(RuntimeError): ...


class TransientGitHubError(GitHubError): ...


class PermanentGitHubError(GitHubError): ...


class RateLimitedGitHubError(TransientGitHubError):
    def __init__(self, retry_after: float, message: str = "") -> None:
        super().__init__(message or f"rate-limited; Retry-After={retry_after}s")
        self.retry_after = retry_after

FIND_PROJECT_QUERY = """
query FindProject($org: String!, $number: Int!) {
  organization(login: $org) {
    projectV2(number: $number) {
      id
      title
    }
  }
}
"""

GET_PROJECT_FIELDS_QUERY = """
query GetProjectFields($projectId: ID!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      fields(first: 20) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options {
              id
              name
            }
          }
        }
      }
    }
  }
}
"""

POLL_BOARD_QUERY = """
query PollBoard($projectId: ID!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      items(first: 50) {
        nodes {
          id
          fieldValues(first: 8) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
              }
            }
          }
          content {
            ... on Issue {
              id
              number
              title
              body
              url
              labels(first: 100) {
                nodes {
                  name
                }
              }
            }
            ... on PullRequest {
              id
              number
              title
              body
            }
            ... on DraftIssue {
              title
              body
            }
          }
        }
      }
    }
  }
}
"""

GET_ISSUE_DETAILS_QUERY = """
query GetIssueDetails($issueId: ID!) {
  node(id: $issueId) {
    ... on Issue {
      id
      number
      title
      body
      url
      state
      labels(first: 10) { nodes { name } }
      comments(first: 50) {
        nodes {
          id
          body
          author { login }
          createdAt
        }
      }
      timelineItems(first: 50, itemTypes: [CONNECTED_EVENT, CROSS_REFERENCED_EVENT]) {
        nodes {
          ... on ConnectedEvent {
            subject {
              ... on PullRequest { id number title url state merged }
            }
          }
          ... on CrossReferencedEvent {
            source {
              ... on PullRequest { id number title url state merged }
            }
          }
        }
      }
    }
  }
}
"""

MOVE_CARD_MUTATION = """
mutation MoveCard($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(
    input: {
      projectId: $projectId
      itemId: $itemId
      fieldId: $fieldId
      value: { singleSelectOptionId: $optionId }
    }
  ) {
    projectV2Item { id }
  }
}
"""

GET_PR_REVIEWS_QUERY = """
query GetPRReviews($prId: ID!) {
  node(id: $prId) {
    ... on PullRequest {
      reviews(first: 50) {
        nodes {
          id
          author { __typename login }
          state
          body
          submittedAt
          comments(first: 50) {
            nodes {
              body
              path
              line: originalLine
            }
          }
        }
      }
      reviewDecision
      mergeable
      mergeStateStatus
    }
  }
}
"""

CHECK_MERGEABILITY_QUERY = """
query CheckMergeability($prId: ID!) {
  node(id: $prId) {
    ... on PullRequest {
      mergeable
      mergeStateStatus
      reviewDecision
    }
  }
}
"""

SQUASH_MERGE_MUTATION = """
mutation SquashMerge($pullRequestId: ID!) {
  mergePullRequest(
    input: { pullRequestId: $pullRequestId, mergeMethod: SQUASH }
  ) {
    pullRequest {
      id
      merged
      mergeCommit { oid messageHeadline }
    }
  }
}
"""

ADD_COMMENT_MUTATION = """
mutation AddComment($subjectId: ID!, $body: String!) {
  addComment(input: { subjectId: $subjectId, body: $body }) {
    commentEdge {
      node {
        id
        body
        createdAt
      }
    }
  }
}
"""

# ---------------------------------------------------------------------------
# Advocate GraphQL operations (spec 007)
# ---------------------------------------------------------------------------

LIST_OPEN_ISSUES_QUERY = """
query ListOpenIssues($owner: String!, $repo: String!, $first: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    issues(
      first: $first
      states: [OPEN]
      after: $cursor
      orderBy: { field: CREATED_AT, direction: DESC }
    ) {
      nodes {
        id
        number
        title
        body
        url
        labels(first: 100) {
          nodes {
            id
            name
          }
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

GET_FILE_CONTENT_QUERY = """
query GetFileContent($owner: String!, $repo: String!, $expression: String!) {
  repository(owner: $owner, name: $repo) {
    object(expression: $expression) {
      ... on Blob {
        text
      }
    }
  }
}
"""

GET_REPOSITORY_ID_QUERY = """
query GetRepositoryId($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    id
  }
}
"""

GET_LABEL_IDS_QUERY = """
query GetLabelIds($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    labels(first: 100) {
      nodes {
        id
        name
      }
    }
  }
}
"""

CREATE_LABEL_MUTATION = """
mutation CreateLabel($repositoryId: ID!, $name: String!, $color: String!, $description: String!) {
  createLabel(
    input: {
      repositoryId: $repositoryId
      name: $name
      color: $color
      description: $description
    }
  ) {
    label {
      id
      name
    }
  }
}
"""

ADD_LABELS_MUTATION = """
mutation AddLabels($labelableId: ID!, $labelIds: [ID!]!) {
  addLabelsToLabelable(
    input: { labelableId: $labelableId, labelIds: $labelIds }
  ) {
    labelable {
      ... on Issue {
        id
        labels(first: 100) {
          nodes {
            id
            name
          }
        }
      }
    }
  }
}
"""


class GitHubService:
    """Async GitHub GraphQL service with field and option caching."""

    _DEFAULT_RETRY_KWARGS: ClassVar[dict[str, Any]] = {
        "attempts": 1,
        "wait_initial": 0.1,
        "wait_max": 30.0,
        "wait_jitter": 0.0,
        "wait_exp_base": 2.0,
    }

    def __init__(
        self,
        org: str,
        project_number: int,
        endpoint: str = "https://api.github.com/graphql",
        circuit_breaker: Any = None,
        retry_kwargs: dict[str, Any] | None = None,
        # Legacy positional arg — kept for backwards compat with callers
        # that pass token= directly; prefer auth= for new callers.
        token: str | None = None,
        auth: Any = None,  # GitHubAuth — typed Any to avoid circular import at class body level
    ) -> None:
        from coordinare.auth.pat import PatAuth
        from coordinare.auth.protocol import GitHubAuth

        if auth is not None and token is not None:
            msg = "GitHubService accepts only one of auth= or token=, not both"
            raise ValueError(msg)
        if auth is not None:
            if not isinstance(auth, GitHubAuth):
                msg = f"auth= must satisfy the GitHubAuth protocol, got {type(auth)!r}"
                raise ValueError(msg)
            self._auth: GitHubAuth = auth
        elif token is not None:
            self._auth = PatAuth(token)
        else:
            msg = "GitHubService requires either auth= or token="
            raise ValueError(msg)

        self._org = org
        self._project_number = project_number
        self._endpoint = endpoint
        self._circuit_breaker = circuit_breaker
        self._retry_kwargs = retry_kwargs if retry_kwargs is not None else dict(self._DEFAULT_RETRY_KWARGS)

        self._client: Client | None = None
        self._last_token: str | None = None
        self.project_id: str | None = None
        self.project_title: str | None = None
        self.field_cache: dict[str, Any] = {}

    async def _current_token(self) -> str:
        return await self._auth.get_token()

    def _build_client(self, token: str) -> Client:
        transport = AIOHTTPTransport(
            url=self._endpoint,
            headers={"Authorization": f"bearer {token}"},
        )
        return Client(transport=transport, fetch_schema_from_transport=False)

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        token = await self._current_token()
        if self._client is None or token != self._last_token:
            self._client = self._build_client(token)
            self._last_token = token
        document = gql(query)
        try:
            if hasattr(self._client, "execute_async"):
                result = await self._client.execute_async(document, variable_values=variables)
            else:
                result_or_awaitable = self._client.execute(document, variable_values=variables)
                if inspect.isawaitable(result_or_awaitable):
                    result = await result_or_awaitable
                else:
                    result = result_or_awaitable
        except aiohttp.ClientResponseError as exc:
            if exc.status == 429:
                raw = exc.headers.get("Retry-After", "60") if exc.headers else "60"
                try:
                    retry_after = float(raw)
                except (ValueError, TypeError):
                    retry_after = 60.0
                await asyncio.sleep(retry_after)
                raise RateLimitedGitHubError(retry_after=retry_after) from exc
            if exc.status >= 500:
                raise TransientGitHubError(str(exc)) from exc
            raise PermanentGitHubError(str(exc)) from exc
        except (TimeoutError, aiohttp.ClientError, OSError) as exc:
            raise TransientGitHubError(str(exc)) from exc
        except ValueError as exc:
            raise PermanentGitHubError(str(exc)) from exc
        if not isinstance(result, dict):
            msg = "GitHub GraphQL response must be a JSON object"
            raise PermanentGitHubError(msg)
        return result

    async def _retried_execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        @stamina.retry(on=TransientGitHubError, **self._retry_kwargs)
        async def _inner() -> dict[str, Any]:
            return await self._execute(query, variables)

        return await _inner()

    async def _guarded_execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        from coordinare.metrics import METRICS

        try:
            if self._circuit_breaker is not None:
                async with self._circuit_breaker.guard():
                    result = await self._retried_execute(query, variables)
            else:
                result = await self._retried_execute(query, variables)
            METRICS.service_calls_total.labels(
                service="github", action="execute", outcome="success",
            ).inc()
            return result
        except Exception:
            METRICS.service_calls_total.labels(
                service="github", action="execute", outcome="failure",
            ).inc()
            raise

    async def initialize(self) -> None:
        project_result = await self._guarded_execute(
            FIND_PROJECT_QUERY,
            {"org": self._org, "number": self._project_number},
        )
        project = project_result.get("organization", {}).get("projectV2")
        if not isinstance(project, dict) or "id" not in project:
            msg = "Project not found for provided organization and number"
            raise ValueError(msg)

        self.project_id = str(project["id"])
        self.project_title = str(project.get("title", ""))

        fields_result = await self._guarded_execute(
            GET_PROJECT_FIELDS_QUERY,
            {"projectId": self.project_id},
        )
        nodes = fields_result.get("node", {}).get("fields", {}).get("nodes", [])
        if not isinstance(nodes, list):
            msg = "Project fields response is malformed"
            raise ValueError(msg)

        status_field_id: str | None = None
        status_options: dict[str, str] = {}

        for node in nodes:
            if not isinstance(node, dict):
                continue
            if str(node.get("name", "")).strip().lower() != "status":
                continue
            status_field_id = str(node.get("id", ""))
            options = node.get("options", [])
            if isinstance(options, list):
                for option in options:
                    if not isinstance(option, dict):
                        continue
                    option_name = str(option.get("name", "")).strip().lower()
                    option_id = str(option.get("id", "")).strip()
                    if option_name and option_id:
                        status_options[option_name] = option_id

        if not status_field_id:
            msg = "Status field not found in project"
            raise ValueError(msg)

        self.field_cache = {
            "status_field_id": status_field_id,
            "status_option_ids": status_options,
        }

    def _ensure_initialized(self) -> None:
        if not self.project_id or not self.field_cache:
            msg = "GitHubService is not initialized"
            raise RuntimeError(msg)

    async def poll_board(self) -> dict[str, Any]:
        self._ensure_initialized()
        result = await self._guarded_execute(POLL_BOARD_QUERY, {"projectId": self.project_id})
        items = result.get("node", {}).get("items", {}).get("nodes", [])
        if not isinstance(items, list):
            return {"snapshot": {}}

        snapshot: dict[str, list[str]] = {
            "TODO": [],
            "BLOCKED": [],
            "IN_PROGRESS": [],
            "IN_REVIEW": [],
            "DONE": [],
        }
        titles: dict[str, str] = {}
        descriptions: dict[str, str] = {}
        issue_numbers: dict[str, int] = {}
        issue_urls: dict[str, str] = {}
        item_labels: dict[str, list[str]] = {}
        # Maps project item ID (PVTI_…) → underlying issue/PR node ID (I_… / PR_…)
        # Required for API calls that target issues/PRs (addComment, addLabels, etc.)
        content_node_ids: dict[str, str] = {}

        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", ""))
            if not item_id:
                continue

            status_name = "TODO"
            field_values = item.get("fieldValues", {}).get("nodes", [])
            if isinstance(field_values, list):
                for field_value in field_values:
                    if not isinstance(field_value, dict):
                        continue
                    raw_name = str(field_value.get("name", "")).strip().lower()
                    if raw_name in {"todo / backlog", "todo", "backlog"}:
                        status_name = "TODO"
                    elif raw_name == "blocked":
                        status_name = "BLOCKED"
                    elif raw_name in {"in progress", "in_progress"}:
                        status_name = "IN_PROGRESS"
                    elif raw_name in {"in review", "in_review"}:
                        status_name = "IN_REVIEW"
                    elif raw_name == "done":
                        status_name = "DONE"

            content = item.get("content", {})
            if isinstance(content, dict):
                titles[item_id] = str(content.get("title", ""))
                descriptions[item_id] = str(content.get("body", ""))
                number = content.get("number", 0)
                issue_numbers[item_id] = int(number) if isinstance(number, int) else 0
                content_node_id = str(content.get("id", "")).strip()
                if content_node_id:
                    content_node_ids[item_id] = content_node_id
                issue_url = str(content.get("url", "")).strip()
                if issue_url:
                    issue_urls[item_id] = issue_url
                label_nodes = content.get("labels", {})
                if isinstance(label_nodes, dict):
                    item_labels[item_id] = [
                        str(n.get("name", ""))
                        for n in label_nodes.get("nodes", [])
                        if isinstance(n, dict)
                    ]

            snapshot.setdefault(status_name, []).append(item_id)

        return {
            "snapshot": snapshot,
            "titles": titles,
            "descriptions": descriptions,
            "issue_numbers": issue_numbers,
            "issue_urls": issue_urls,
            "item_labels": item_labels,
            "content_node_ids": content_node_ids,
        }

    async def get_issue_details(self, issue_id: str) -> dict[str, Any]:
        result = await self._guarded_execute(GET_ISSUE_DETAILS_QUERY, {"issueId": issue_id})
        issue = result.get("node")
        if isinstance(issue, dict):
            return issue
        return {}

    async def move_card(self, item_id: str, status: str) -> None:
        self._ensure_initialized()
        status_field_id = str(self.field_cache.get("status_field_id", ""))
        status_option_ids = self.field_cache.get("status_option_ids", {})
        if not isinstance(status_option_ids, dict):
            msg = "Status options cache malformed"
            raise RuntimeError(msg)

        key_map = {
            "TODO": ["todo / backlog", "todo", "backlog"],
            "BLOCKED": ["blocked"],
            "IN_PROGRESS": ["in_progress", "in progress"],
            "IN_REVIEW": ["in_review", "in review"],
            "DONE": ["done"],
        }
        candidates = key_map.get(status, [status.lower()])
        option_id = ""
        for name in candidates:
            option = status_option_ids.get(name)
            if isinstance(option, str) and option:
                option_id = option
                break
        if not option_id:
            available = list(status_option_ids.keys())
            msg = (
                f"Unknown status option for {status!r}. "
                f"Tried: {candidates}. "
                f"Available board options: {available}"
            )
            raise ValueError(msg)

        await self._guarded_execute(
            MOVE_CARD_MUTATION,
            {
                "projectId": self.project_id,
                "itemId": item_id,
                "fieldId": status_field_id,
                "optionId": option_id,
            },
        )

    async def get_pr_reviews(self, pr_id: str) -> list[dict[str, Any]]:
        result = await self._guarded_execute(GET_PR_REVIEWS_QUERY, {"prId": pr_id})
        nodes = result.get("node", {}).get("reviews", {}).get("nodes", [])
        if not isinstance(nodes, list):
            return []
        parsed: list[dict[str, Any]] = []
        for review in nodes:
            if not isinstance(review, dict):
                continue
            author = review.get("author", {})
            author_login = str(author.get("login", "")) if isinstance(author, dict) else ""
            # Extract inline review comments (file-specific feedback)
            inline_comments: list[dict[str, Any]] = []
            raw_comments = review.get("comments", {})
            if isinstance(raw_comments, dict):
                for c in raw_comments.get("nodes", []):
                    if isinstance(c, dict) and c.get("body"):
                        inline_comments.append({
                            "body": str(c["body"]),
                            "path": str(c.get("path", "")),
                            "line": c.get("line"),
                        })
            parsed.append(
                {
                    "id": str(review.get("id", "")),
                    "author_login": author_login,
                    "state": str(review.get("state", "")),
                    "body": str(review.get("body", "")),
                    "submitted_at": review.get("submittedAt"),
                    "comments": inline_comments,
                }
            )
        return parsed

    async def check_mergeability(self, pr_id: str) -> dict[str, Any]:
        result = await self._guarded_execute(CHECK_MERGEABILITY_QUERY, {"prId": pr_id})
        node = result.get("node", {})
        if not isinstance(node, dict):
            return {"mergeable": False, "reason": "missing_pr"}

        mergeable_flag = str(node.get("mergeable", "")).upper() == "MERGEABLE"
        review_decision = str(node.get("reviewDecision", "")).upper()
        return {
            "mergeable": mergeable_flag and review_decision == "APPROVED",
            "mergeable_raw": str(node.get("mergeable", "")),
            "merge_state_status": str(node.get("mergeStateStatus", "")),
            "review_decision": review_decision,
        }

    async def squash_merge(self, pr_id: str) -> dict[str, Any]:
        result = await self._guarded_execute(SQUASH_MERGE_MUTATION, {"pullRequestId": pr_id})
        node = result.get("mergePullRequest", {}).get("pullRequest", {})
        if not isinstance(node, dict):
            return {"merged": False}
        return {
            "merged": bool(node.get("merged", False)),
            "id": str(node.get("id", "")),
            "merge_commit": node.get("mergeCommit"),
        }

    async def link_to_project(self, content_id: str) -> str | None:
        """Add an item (PR or issue) to the project board by its node ID.

        Returns the project item ID on success, or None on failure.
        """
        if not self.project_id:
            return None
        mutation = """
        mutation($projectId: ID!, $contentId: ID!) {
          addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
            item { id }
          }
        }
        """
        try:
            result = await self._guarded_execute(mutation, {
                "projectId": self.project_id,
                "contentId": content_id,
            })
            item = result.get("addProjectV2ItemById", {}).get("item", {})
            return str(item.get("id", "")) or None
        except Exception as exc:
            self._logger.warning("link_to_project_failed", content_id=content_id, error=str(exc))
            return None

    async def add_comment(self, subject_id: str, body: str) -> dict[str, Any]:
        result = await self._guarded_execute(ADD_COMMENT_MUTATION, {"subjectId": subject_id, "body": body})
        node = result.get("addComment", {}).get("commentEdge", {}).get("node", {})
        if isinstance(node, dict):
            return node
        return {}

    # -----------------------------------------------------------------------
    # Advocate methods (spec 007)
    # -----------------------------------------------------------------------

    async def get_repository_id(self, owner: str, repo: str) -> str:
        result = await self._guarded_execute(
            GET_REPOSITORY_ID_QUERY, {"owner": owner, "repo": repo}
        )
        repo_id = result.get("repository", {}).get("id")
        if not repo_id:
            msg = f"Repository not found: {owner}/{repo}"
            raise ValueError(msg)
        return str(repo_id)

    async def get_label_ids(self, owner: str, repo: str) -> dict[str, str]:
        result = await self._guarded_execute(
            GET_LABEL_IDS_QUERY, {"owner": owner, "repo": repo}
        )
        nodes = result.get("repository", {}).get("labels", {}).get("nodes", [])
        if not isinstance(nodes, list):
            return {}
        return {
            str(n.get("name", "")): str(n.get("id", ""))
            for n in nodes
            if isinstance(n, dict) and n.get("name") and n.get("id")
        }

    async def ensure_labels_exist(
        self,
        owner: str,
        repo: str,
        handled_label: str,
        escalation_label: str,
    ) -> dict[str, str]:
        """Ensure advocate labels exist; create any that are missing. Returns name→id map."""
        repo_id = await self.get_repository_id(owner, repo)
        existing = await self.get_label_ids(owner, repo)

        label_colors = {handled_label: "0075ca", escalation_label: "e4e669"}
        label_descriptions = {
            handled_label: "Issue handled by the customer advocate agent",
            escalation_label: "Issue escalated to a human reviewer",
        }

        for label_name in (handled_label, escalation_label):
            if label_name not in existing:
                result = await self._guarded_execute(
                    CREATE_LABEL_MUTATION,
                    {
                        "repositoryId": repo_id,
                        "name": label_name,
                        "color": label_colors.get(label_name, "ededed"),
                        "description": label_descriptions.get(label_name, ""),
                    },
                )
                new_label = result.get("createLabel", {}).get("label", {})
                if isinstance(new_label, dict) and new_label.get("id"):
                    existing[label_name] = str(new_label["id"])

        return existing

    async def add_labels(self, issue_id: str, label_ids: list[str]) -> None:
        await self._guarded_execute(
            ADD_LABELS_MUTATION,
            {"labelableId": issue_id, "labelIds": label_ids},
        )

    async def list_open_issues(
        self, owner: str, repo: str, first: int = 20
    ) -> list[dict[str, Any]]:
        result = await self._guarded_execute(
            LIST_OPEN_ISSUES_QUERY,
            {"owner": owner, "repo": repo, "first": first, "cursor": None},
        )
        nodes = result.get("repository", {}).get("issues", {}).get("nodes", [])
        if not isinstance(nodes, list):
            return []
        return [n for n in nodes if isinstance(n, dict)]

    async def get_file_content(
        self, owner: str, repo: str, path: str, ref: str = "HEAD"
    ) -> str | None:
        expression = f"{ref}:{path}"
        result = await self._guarded_execute(
            GET_FILE_CONTENT_QUERY,
            {"owner": owner, "repo": repo, "expression": expression},
        )
        obj = result.get("repository", {}).get("object")
        if isinstance(obj, dict):
            return obj.get("text")
        return None
