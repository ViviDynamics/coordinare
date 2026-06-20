from __future__ import annotations

import asyncio
import fnmatch
import inspect
import re
from typing import Any, ClassVar
from urllib.parse import quote

import aiohttp
import httpx
import stamina
import structlog
from gql import Client, gql
from gql.transport.aiohttp import AIOHTTPTransport
from gql.transport.exceptions import TransportQueryError, TransportServerError

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Exception taxonomy (T008)
# ---------------------------------------------------------------------------


class GitHubError(RuntimeError): ...


class TransientGitHubError(GitHubError): ...


class PermanentGitHubError(GitHubError): ...


class AuthGitHubError(PermanentGitHubError):
    """A recognized GitHub authorization failure (e.g. HTTP 401 / UNAUTHORIZED).

    Subclasses ``PermanentGitHubError`` so that, if it escapes the internal
    refresh-and-retry path, it is treated as permanent: not retried by stamina
    (``on=TransientGitHubError``) and ignored by the circuit breaker
    (``ignore=PermanentGitHubError``). Messages must stay secret-free — never
    embed a token, ``Authorization`` header value, or response body.
    """


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
query PollBoard($projectId: ID!, $after: String) {
  node(id: $projectId) {
    ... on ProjectV2 {
      items(first: 100, after: $after) {
        pageInfo {
          hasNextPage
          endCursor
        }
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
              assignees(first: 10) {
                nodes {
                  login
                }
              }
              # TODO: paginate if a card accumulates >20 cross-references —
              # ``last: 20`` will drop the oldest links, which may include the
              # canonical merged PR.  Acceptable today; revisit when first hit.
              timelineItems(last: 20, itemTypes: [CROSS_REFERENCED_EVENT]) {
                nodes {
                  ... on CrossReferencedEvent {
                    source {
                      ... on PullRequest {
                        url
                        state
                        merged
                      }
                    }
                  }
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
      reviews(last: 50) {
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
      headRefOid
      headRefName
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

GET_FILE_BLOB_SHA_QUERY = """
query GetFileBlobSha($owner: String!, $repo: String!, $expression: String!) {
  repository(owner: $owner, name: $repo) {
    object(expression: $expression) {
      ... on Blob {
        oid
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

GET_BRANCH_PROTECTION_QUERY = """
query GetBranchProtection($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    branchProtectionRules(first: 50) {
      nodes {
        pattern
        requiredStatusChecks {
          context
        }
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
        self._project_name: str = ""  # set externally by __main__ for REST branch operations

        self._client: Client | None = None
        self._last_token: str | None = None
        self._gql_lock: asyncio.Lock = asyncio.Lock()
        self.project_id: str | None = None
        self.project_title: str | None = None
        self.field_cache: dict[str, Any] = {}

    async def _current_token(self) -> str:
        return await self._auth.get_token()

    async def current_token(self) -> str:
        """Public accessor for the current auth token."""
        return await self._current_token()

    @property
    def org(self) -> str:
        return self._org

    @property
    def project_name(self) -> str:
        return self._project_name

    def _build_client(self, token: str) -> Client:
        transport = AIOHTTPTransport(
            url=self._endpoint,
            headers={"Authorization": f"bearer {token}"},
        )
        return Client(transport=transport, fetch_schema_from_transport=False)

    async def aclose(self) -> None:
        """Close the underlying aiohttp session if one has been opened."""
        import contextlib
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close_async()
            self._client = None

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        # 061: Hold the lock for the full request, not just client construction.
        # The underlying AIOHTTPTransport cannot service concurrent
        # execute_async calls — overlapping callers produce
        # "Transport is already connected" and poison the session for
        # subsequent requests ("Connector is closed.").
        async with self._gql_lock:
            token = await self._current_token()
            if self._client is None or token != self._last_token:
                self._client = self._build_client(token)
                self._last_token = token
            try:
                return await self._execute_request(self._client, query, variables)
            except AuthGitHubError:
                # 085: a recognized auth failure (401 / UNAUTHORIZED). The
                # cached credential may be stale — discard it, mint a fresh
                # one, and retry exactly once. Bounded to ≤1 invalidate,
                # ≤1 re-mint, ≤1 retry; never logs token material.
                old_token = self._last_token
                await self._auth.invalidate()
                fresh_token = await self._current_token()
                if fresh_token == old_token:
                    # Credential is static/unchanged (e.g. PAT) — a refresh
                    # cannot help, so don't retry. Surface as permanent.
                    logger.info("github.auth.refresh_retry", outcome="failed")
                    raise PermanentGitHubError(
                        "GitHub auth failed and the credential could not be refreshed"
                    ) from None
                self._client = self._build_client(fresh_token)
                self._last_token = fresh_token
                try:
                    result = await self._execute_request(self._client, query, variables)
                except AuthGitHubError as exc:
                    logger.info("github.auth.refresh_retry", outcome="failed")
                    raise PermanentGitHubError(
                        "GitHub auth failed again after credential refresh"
                    ) from exc
                logger.info("github.auth.refresh_retry", outcome="recovered")
                return result

    async def _execute_request(self, client: Any, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        document = gql(query)
        try:
            if hasattr(client, "execute_async"):
                result = await client.execute_async(document, variable_values=variables)
            else:
                result_or_awaitable = client.execute(document, variable_values=variables)
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
            if exc.status == 401:
                raise AuthGitHubError(str(exc)) from exc
            if exc.status >= 500:
                raise TransientGitHubError(str(exc)) from exc
            raise PermanentGitHubError(str(exc)) from exc
        except TransportQueryError as exc:
            # 042: GraphQL response-level errors. Classify by the GitHub
            # error type so application-layer issues (auth, ruleset rejection,
            # missing nodes) don't get retried forever and don't trip the
            # service-health circuit breaker.  Only true transport problems
            # (5xx, timeouts) should count as service failures.
            errors = exc.errors or []
            err_types: set[str] = set()
            for e in errors:
                if isinstance(e, dict):
                    t = e.get("type")
                    if t:
                        err_types.add(str(t))
            # 085: UNAUTHORIZED is a recognized auth failure — route it to
            # AuthGitHubError so _execute can refresh-and-retry once. It is
            # still permanent (AuthGitHubError subclasses PermanentGitHubError)
            # so the breaker/stamina behavior is unchanged.
            if "UNAUTHORIZED" in err_types:
                raise AuthGitHubError(str(exc)) from exc
            permanent_types = {
                "UNPROCESSABLE",   # branch protection / ruleset rejection
                "FORBIDDEN",       # missing scope / installation perms
                "NOT_FOUND",       # bad node id (e.g. stale pr_node_id)
            }
            if err_types & permanent_types:
                raise PermanentGitHubError(str(exc)) from exc
            # Anything else (server-side transient, rate limit) is retryable
            raise TransientGitHubError(str(exc)) from exc
        except TransportServerError as exc:
            # 5xx from the GraphQL transport (e.g. 502 Bad Gateway during
            # GitHub upstream incidents).  Surface as a transient error so
            # callers log a clean warning instead of an unhandled traceback.
            code = getattr(exc, "code", None)
            if code == 401:
                # 085: a 401 is an auth failure, not a transient server
                # error — route it to AuthGitHubError so _execute can
                # refresh-and-retry once with a freshly minted credential.
                raise AuthGitHubError(f"GraphQL HTTP {code}: {exc}") from exc
            raise TransientGitHubError(f"GraphQL HTTP {code}: {exc}") from exc
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
        from coordinare.observability import get_current_symphony

        try:
            if self._circuit_breaker is not None:
                # 042: PermanentGitHubError is an application-layer error
                # (auth, ruleset rejection, missing node) — not a service
                # health issue.  Don't count it against the breaker; otherwise
                # a single misconfigured ruleset trips the breaker every cycle
                # and starves all other GitHub calls.
                async with self._circuit_breaker.guard(ignore=PermanentGitHubError):
                    result = await self._retried_execute(query, variables)
            else:
                result = await self._retried_execute(query, variables)
            METRICS.service_calls_total.labels(
                symphony=get_current_symphony(),
                service="github",
                action="execute",
                outcome="success",
            ).inc()
            return result
        except Exception:
            METRICS.service_calls_total.labels(
                symphony=get_current_symphony(),
                service="github",
                action="execute",
                outcome="failure",
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
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        # Cap pagination to bound a single poll's work — a runaway board (or a
        # broken hasNextPage upstream) would otherwise loop indefinitely and
        # block the coordinare's poll cycle.
        max_pages = 20
        more_pages_available = False
        for page_index in range(max_pages):
            result = await self._guarded_execute(
                POLL_BOARD_QUERY,
                {"projectId": self.project_id, "after": cursor},
            )
            items_node = result.get("node", {}).get("items", {})
            page_items = items_node.get("nodes", [])
            if isinstance(page_items, list):
                items.extend(p for p in page_items if isinstance(p, dict))
            page_info = items_node.get("pageInfo", {}) or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
            # If this is the last allowed iteration and the server still says
            # hasNextPage=True, we are about to truncate the board view. Flag
            # it so the warning below can report items the operator is not
            # seeing rather than just "we hit the cap".
            if page_index == max_pages - 1:
                more_pages_available = True
        if more_pages_available:
            logger.warning(
                "poll_board.pagination_cap_hit",
                max_pages=max_pages,
                items_collected=len(items),
                more_pages_available=True,
            )

        snapshot: dict[str, list[str]] = {
            "BACKLOG": [],
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
        item_assignees: dict[str, list[str]] = {}
        # Maps project item ID (PVTI_…) → underlying issue/PR node ID (I_… / PR_…)
        # Required for API calls that target issues/PRs (addComment, addLabels, etc.)
        content_node_ids: dict[str, str] = {}
        # Item ID → linked PR URL.  Picked from issue timeline cross-references
        # with priority OPEN → MERGED → CLOSED so a card with an in-flight PR
        # surfaces the live PR rather than a stale closed one.
        pr_urls: dict[str, str] = {}

        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", ""))
            if not item_id:
                continue

            status_name = ""  # unknown until we read the field
            field_values = item.get("fieldValues", {}).get("nodes", [])
            if isinstance(field_values, list):
                for field_value in field_values:
                    if not isinstance(field_value, dict):
                        continue
                    raw_name = str(field_value.get("name", "")).strip().lower()
                    if raw_name in {"todo / backlog", "todo"}:
                        status_name = "TODO"
                    elif raw_name == "backlog":
                        status_name = "BACKLOG"
                    elif raw_name == "blocked":
                        status_name = "BLOCKED"
                    elif raw_name in {"in progress", "in_progress"}:
                        status_name = "IN_PROGRESS"
                    elif raw_name in {"in review", "in_review"}:
                        status_name = "IN_REVIEW"
                    elif raw_name == "done":
                        status_name = "DONE"

            item_assignees[item_id] = []  # default empty; overwritten below if content has assignees
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
                assignee_nodes = content.get("assignees", {})
                item_assignees[item_id] = [
                    str(n.get("login", "")).lower()
                    for n in (assignee_nodes.get("nodes", []) if isinstance(assignee_nodes, dict) else [])
                    if isinstance(n, dict) and n.get("login")
                ]
                timeline = content.get("timelineItems", {})
                if isinstance(timeline, dict):
                    nodes = timeline.get("nodes", [])
                    open_url = ""
                    merged_url = ""
                    closed_url = ""
                    for ev in nodes if isinstance(nodes, list) else []:
                        if not isinstance(ev, dict):
                            continue
                        src = ev.get("source")
                        if not isinstance(src, dict):
                            continue
                        url_val = str(src.get("url", "")).strip()
                        if not url_val:
                            continue
                        state_val = str(src.get("state", "")).upper()
                        if state_val == "OPEN":
                            open_url = url_val
                        elif src.get("merged"):
                            merged_url = url_val
                        elif state_val == "CLOSED":
                            closed_url = url_val
                    chosen = open_url or merged_url or closed_url
                    if chosen:
                        pr_urls[item_id] = chosen

            if status_name:  # skip items with unknown/unmapped status
                snapshot.setdefault(status_name, []).append(item_id)

        return {
            "snapshot": snapshot,
            "titles": titles,
            "descriptions": descriptions,
            "issue_numbers": issue_numbers,
            "issue_urls": issue_urls,
            "item_labels": item_labels,
            "item_assignees": item_assignees,
            "content_node_ids": content_node_ids,
            "pr_urls": pr_urls,
        }

    async def get_issue_details(self, issue_id: str) -> dict[str, Any]:
        result = await self._guarded_execute(GET_ISSUE_DETAILS_QUERY, {"issueId": issue_id})
        issue = result.get("node")
        if isinstance(issue, dict):
            return issue
        return {}

    async def check_issue_state(self, repo: str, issue_number: int) -> str:
        """Return the state of a GitHub issue by number.

        ``repo`` is the ``owner/name`` slug (e.g. ``"ViviDynamics/website"``).
        Returns one of:
          - ``"open"`` / ``"closed"`` — definitive (issue exists)
          - ``"not_found"`` — definitive (404)
          - ``"auth_error"`` / ``"api_error"`` — transient; the caller MUST
            treat these as "unknown, try again next cycle", not as a permanent
            failure (see resolve_off_board_dependencies).

        Uses a lightweight REST call (not GraphQL) so we don't need the node
        ID.  Used by the dependency service (046) to resolve off-board
        blockers.
        """
        url = f"{self._rest_api_base()}/repos/{repo}/issues/{issue_number}"
        try:
            token = await self._current_token()
        except Exception as exc:
            logger.warning(
                "check_issue_state.auth_failed",
                issue_number=issue_number,
                error=str(exc),
            )
            return "auth_error"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return "not_found"
            if resp.is_success:
                data = resp.json()
                return str(data.get("state", "open"))
            logger.warning(
                "check_issue_state.api_error",
                issue_number=issue_number,
                status_code=resp.status_code,
                body_preview=resp.text[:200],
            )
            return "api_error"
        except Exception as exc:
            logger.warning(
                "check_issue_state.request_failed",
                issue_number=issue_number,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return "api_error"

    async def get_issue_comments(
        self,
        issue_number: int,
        since_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch comments on a GitHub issue, optionally after a known comment ID.

        Returns a list of dicts with keys: id, author, body, created_at.
        Returns [] on any error (logs warning).
        """
        if not self._project_name:
            logger.warning("get_issue_comments.no_project_name", issue_number=issue_number)
            return []
        url = f"{self._rest_api_base()}/repos/{self._org}/{self._project_name}/issues/{issue_number}/comments"
        params: dict[str, str | int] = {"per_page": 100}
        try:
            token = await self._current_token()
        except Exception as exc:
            logger.warning("get_issue_comments.auth_failed", issue_number=issue_number, error=str(exc))
            return []
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            comments: list[dict[str, Any]] = []
            next_url: str | None = url
            async with httpx.AsyncClient(timeout=10) as client:
                while next_url:
                    resp = await client.get(next_url, headers=headers, params=params if next_url == url else {})
                    if not resp.is_success:
                        logger.warning(
                            "get_issue_comments.api_error",
                            issue_number=issue_number,
                            status_code=resp.status_code,
                        )
                        return []
                    page: list[dict[str, Any]] = resp.json()
                    for c in page:
                        if not isinstance(c, dict):
                            continue
                        cid = int(c.get("id", 0))
                        if since_id is not None and cid <= since_id:
                            continue
                        author = c.get("user", {})
                        comments.append({
                            "id": cid,
                            "author": str(author.get("login", "")) if isinstance(author, dict) else "",
                            "body": str(c.get("body", "")),
                            "created_at": str(c.get("created_at", "")),
                        })
                    link = resp.headers.get("link", "")
                    next_url = None
                    if 'rel="next"' in link:
                        for part in link.split(","):
                            if 'rel="next"' in part:
                                next_url = part.split(";")[0].strip().strip("<>")
                                break
                    params = {}
            return comments
        except Exception as exc:
            logger.warning("get_issue_comments.request_failed", issue_number=issue_number, error=str(exc))
            return []

    def _rest_api_base(self) -> str:
        """Derive the REST API base URL from the configured GraphQL endpoint.

        Standard: https://api.github.com/graphql → https://api.github.com
        GHE:      https://github.corp.com/api/graphql → https://github.corp.com/api/v3
        """
        from urllib.parse import urlparse

        # Case-insensitive suffix strip — GraphQL endpoints are conventionally
        # lowercase, but defend against ``/GraphQL`` / mixed-case configs.
        raw = str(self._endpoint).rstrip("/")
        if raw.lower().endswith("/graphql"):
            raw = raw[: -len("/graphql")]
        parsed = urlparse(raw)
        path = parsed.path.rstrip("/")
        if path.lower().endswith("/api"):
            return f"{parsed.scheme}://{parsed.netloc}{path}/v3"
        if path:
            return f"{parsed.scheme}://{parsed.netloc}{path}"
        return f"{parsed.scheme}://{parsed.netloc}"

    async def fetch_failed_job_log(
        self,
        owner: str,
        repo: str,
        job_id: int,
        max_chars: int = 6000,
    ) -> str:
        """Return the tail of a GitHub Actions job log, or "" if unavailable.

        Hits ``/repos/{owner}/{repo}/actions/jobs/{job_id}/logs`` which
        responds with a 302 to a pre-signed S3 URL. The redirect is
        followed without re-sending the Authorization header (S3 rejects
        it). Returns at most ``max_chars`` characters from the tail. Never
        raises — log retrieval is best-effort context for the implementer.
        """
        if max_chars <= 0 or job_id <= 0:
            return ""
        try:
            token = await self._current_token()
        except Exception as exc:
            logger.warning("fetch_failed_job_log.token_failed", job_id=job_id, error=str(exc))
            return ""
        if not token.strip():
            return ""
        url = f"{self._rest_api_base()}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        return ""
                    resp = await client.get(location)
                if not resp.is_success:
                    return ""
                text = resp.text
        except Exception as exc:
            logger.warning("fetch_failed_job_log.request_failed", job_id=job_id, error=str(exc))
            return ""
        if len(text) > max_chars:
            return "... (log truncated) ...\n" + text[-max_chars:]
        return text

    async def get_pr_files(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> dict[str, Any]:
        """074 — Fetch the file list + head SHA for a PR.

        Returns ``{"files": [{"path","added","removed","status"}, ...],
        "head_sha": "...", "truncated": bool, "error": str | None}``.

        ``error`` is non-None on real outages (token unavailable, PR fetch
        failed, request raised) so callers can distinguish "PR genuinely has
        zero files" from "we couldn't reach GitHub" — the classifier maps
        ``error`` to its ``gh_outage`` fallback path.

        ``truncated`` is True when pagination hit the 300-file cap (3 pages of
        100); the classifier treats truncation as a full-depth signal so a
        massive PR can't slip through with skim depth.

        Two REST calls: ``GET /pulls/{n}`` for head.sha + ``GET /pulls/{n}/files``
        for the per-file diff stats.
        """
        result: dict[str, Any] = {
            "files": [],
            "head_sha": "",
            "truncated": False,
            "error": None,
        }
        if pr_number <= 0:
            result["error"] = "invalid_pr_number"
            return result
        try:
            token = await self._current_token()
        except Exception as exc:
            logger.warning("get_pr_files.token_failed", pr=pr_number, error=str(exc))
            result["error"] = f"token_failed:{type(exc).__name__}"
            return result
        if not token.strip():
            result["error"] = "no_token"
            return result
        base = self._rest_api_base()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        files: list[dict[str, Any]] = []
        truncated = False
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                pr_resp = await client.get(
                    f"{base}/repos/{owner}/{repo}/pulls/{pr_number}",
                    headers=headers,
                )
                if not pr_resp.is_success:
                    logger.warning(
                        "get_pr_files.pr_fetch_failed",
                        pr=pr_number,
                        status=pr_resp.status_code,
                    )
                    result["error"] = f"pr_fetch_status:{pr_resp.status_code}"
                    return result
                pr_data = pr_resp.json()
                head_sha = ""
                if isinstance(pr_data, dict):
                    head = pr_data.get("head") or {}
                    if isinstance(head, dict):
                        head_sha = str(head.get("sha") or "")
                result["head_sha"] = head_sha

                for page in range(1, 4):
                    files_resp = await client.get(
                        f"{base}/repos/{owner}/{repo}/pulls/{pr_number}/files",
                        headers=headers,
                        params={"per_page": "100", "page": str(page)},
                    )
                    if not files_resp.is_success:
                        logger.warning(
                            "get_pr_files.files_fetch_failed",
                            pr=pr_number,
                            page=page,
                            status=files_resp.status_code,
                        )
                        result["error"] = f"files_fetch_status:{files_resp.status_code}"
                        result["files"] = files
                        return result
                    batch = files_resp.json()
                    if not isinstance(batch, list) or not batch:
                        break
                    for entry in batch:
                        if not isinstance(entry, dict):
                            continue
                        files.append(
                            {
                                "path": str(entry.get("filename") or ""),
                                "added": int(entry.get("additions") or 0),
                                "removed": int(entry.get("deletions") or 0),
                                "status": str(entry.get("status") or "modified"),
                            }
                        )
                    if len(batch) < 100:
                        break
                    if page == 3:
                        # We hit the page cap with a full last batch — more files exist upstream.
                        truncated = True
        except Exception as exc:
            logger.warning("get_pr_files.request_failed", pr=pr_number, error=str(exc))
            result["error"] = f"request_failed:{type(exc).__name__}"
            result["files"] = files
            return result
        result["files"] = files
        result["truncated"] = truncated
        return result

    async def get_pr_diff(self, pr_url: str) -> tuple[str, list[str]]:
        """083 — Fetch a PR's unified diff + changed-file list (Contract 2).

        Returns ``(raw_unified_diff, changed_files)`` where ``changed_files`` is
        the list of post-image paths parsed from the diff's ``diff --git`` headers.

        Uses the coordinare's existing GH auth (REST API with the
        ``application/vnd.github.diff`` media type — no ``gh`` CLI dependency).

        Raises on any fetch failure (malformed URL, auth, network, non-2xx) so the
        dispatch layer applies fail-closed handling. Never logs the token or the
        raw diff at INFO (FR-011).
        """
        m = re.match(r"https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
        if not m:
            raise ValueError("get_pr_diff: unrecognized PR URL")
        owner, repo, number = m.group(1), m.group(2), int(m.group(3))

        token = await self._current_token()
        if not token.strip():
            raise RuntimeError("get_pr_diff: no GH token available")

        base = self._rest_api_base()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.diff",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{base}/repos/{owner}/{repo}/pulls/{number}",
                headers=headers,
            )
        if not resp.is_success:
            logger.warning("get_pr_diff.fetch_failed", pr=number, status=resp.status_code)
            raise RuntimeError(f"get_pr_diff: fetch failed (status {resp.status_code})")

        raw = resp.text or ""
        changed_files = self._parse_diff_paths(raw)
        logger.info(
            "get_pr_diff.complete",
            pr=number,
            file_count=len(changed_files),
            diff_bytes=len(raw),
        )
        return raw, changed_files

    @staticmethod
    def _parse_diff_paths(raw_diff: str) -> list[str]:
        """Extract post-image (``b/``) paths from a unified diff's git headers."""
        paths: list[str] = []
        for line in raw_diff.splitlines():
            if not line.startswith("diff --git "):
                continue
            # "diff --git a/<path> b/<path>" — take the b-side path.
            dm = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if dm:
                paths.append(dm.group(2))
        return paths

    async def branch_exists(self, branch_name: str) -> bool:
        """Return True if a remote branch exists, False if 404.

        Uses the REST branches endpoint so no GraphQL node ID is needed.
        GHE-compatible: derives the REST base from self._endpoint.
        Requires self._project_name to be set (done by __main__ after init).
        """
        if not self._project_name:
            logger.warning("branch_exists.no_project_name", branch=branch_name)
            return False
        url = f"{self._rest_api_base()}/repos/{self._org}/{self._project_name}/branches/{quote(branch_name, safe='')}"
        try:
            token = await self._current_token()
        except Exception as exc:
            logger.warning("branch_exists.token_failed", branch=branch_name, error=str(exc))
            return False
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return True
            if resp.status_code == 404:
                return False
            logger.warning(
                "branch_exists.unexpected_status",
                branch=branch_name,
                status_code=resp.status_code,
                error=resp.text[:200],
            )
            return False
        except Exception as exc:
            logger.warning("branch_exists.request_failed", branch=branch_name, error=str(exc))
            return False

    async def branch_has_open_pr(self, branch_name: str) -> bool | None:
        """Return open-PR state for ``branch_name``.

        Returns:
        - ``True``: an open PR currently uses ``branch_name`` as head.
        - ``False``: confirmed no open PR for ``branch_name``.
        - ``None``: unknown (transient/auth/config failure while checking).
        """
        if not self._project_name:
            logger.warning("branch_has_open_pr.no_project_name", branch=branch_name)
            return None
        url = f"{self._rest_api_base()}/repos/{self._org}/{self._project_name}/pulls"
        try:
            token = await self._current_token()
        except Exception as exc:
            logger.warning("branch_has_open_pr.token_failed", branch=branch_name, error=str(exc))
            return None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        params = {
            "head": f"{self._org}:{branch_name}",
            "state": "open",
            "per_page": "1",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 404:
                logger.warning(
                    "branch_has_open_pr.repo_not_found_or_inaccessible",
                    branch=branch_name,
                )
                return None
            if not resp.is_success:
                logger.warning(
                    "branch_has_open_pr.unexpected_status",
                    branch=branch_name,
                    status_code=resp.status_code,
                    error=resp.text[:200],
                )
                return None
            data = resp.json()
            return isinstance(data, list) and len(data) > 0
        except Exception as exc:
            logger.warning("branch_has_open_pr.request_failed", branch=branch_name, error=str(exc))
            return None

    async def delete_branch(self, branch_name: str) -> None:
        """Delete a remote branch via REST.

        Logs a warning on failure but never raises — deletion is best-effort.
        Requires self._project_name to be set (done by __main__ after init).
        """
        if not self._project_name:
            logger.warning("delete_branch.no_project_name", branch=branch_name)
            return
        url = f"{self._rest_api_base()}/repos/{self._org}/{self._project_name}/git/refs/heads/{quote(branch_name, safe='')}"
        try:
            token = await self._current_token()
        except Exception as exc:
            logger.warning("workspace.stale_branch_delete_failed", branch=branch_name, error=str(exc))
            return
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.delete(url, headers=headers)
            if not resp.is_success:
                logger.warning(
                    "workspace.stale_branch_delete_failed",
                    branch=branch_name,
                    error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                )
        except Exception as exc:
            logger.warning("workspace.stale_branch_delete_failed", branch=branch_name, error=str(exc))

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

    async def find_pr_for_issue(self, issue_node_id: str) -> dict[str, str] | None:
        """Find the open PR linked to an issue via "Closes #N" or GitHub linkage.

        Used as a recovery mechanism when state-store loses pr_node_id (e.g.,
        after a snapshot bug or daemon crash mid-lifecycle).  Returns
        ``{"pr_node_id": ..., "pr_url": ...}`` for the most recent OPEN PR
        that closes this issue, or None if no such PR exists.
        """
        if not issue_node_id:
            return None
        query = """
        query($issueId: ID!) {
          node(id: $issueId) {
            ... on Issue {
              closedByPullRequestsReferences(first: 5, includeClosedPrs: false) {
                nodes { id url state }
              }
            }
          }
        }
        """
        try:
            result = await self._guarded_execute(query, {"issueId": issue_node_id})
        except Exception as exc:
            logger.warning(
                "find_pr_for_issue.query_failed",
                issue_node_id=issue_node_id,
                error=str(exc),
            )
            return None
        node = result.get("node") or {}
        prs = (node.get("closedByPullRequestsReferences") or {}).get("nodes") or []
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            if pr.get("state") != "OPEN":
                continue
            pr_id = str(pr.get("id") or "")
            pr_url = str(pr.get("url") or "")
            if pr_id and pr_url:
                return {"pr_node_id": pr_id, "pr_url": pr_url}
        return None

    async def count_closed_prs_for_issue(self, issue_node_id: str) -> int:
        """Count CLOSED (unmerged) PRs linked to an issue.

        Returns 0 on missing input or query failure.
        """
        if not issue_node_id:
            return 0
        query = """
        query($issueId: ID!) {
          node(id: $issueId) {
            ... on Issue {
              closedByPullRequestsReferences(first: 50, includeClosedPrs: true) {
                nodes { state }
              }
            }
          }
        }
        """
        try:
            result = await self._guarded_execute(query, {"issueId": issue_node_id})
        except Exception as exc:
            logger.warning(
                "count_closed_prs_for_issue.query_failed",
                issue_node_id=issue_node_id,
                error=str(exc),
            )
            return 0
        node = result.get("node") or {}
        prs = (node.get("closedByPullRequestsReferences") or {}).get("nodes") or []
        closed_count = 0
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            if str(pr.get("state") or "").upper() == "CLOSED":
                closed_count += 1
        return closed_count

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
            # 096: branch head OID, used by the auto-rebase anti-thrash marker to
            # tell "performer pushed new work" from "nothing changed".
            "head_ref_oid": str(node.get("headRefOid", "")),
            # 097: branch name, so the pre-dispatch rebase guard can rebase the
            # branch directly without depending on a (possibly unset) workspace_branch.
            "head_ref_name": str(node.get("headRefName", "")),
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

    async def link_to_project(self, content_id: str, status: str = "IN_REVIEW") -> str | None:
        """Add an item (PR or issue) to the project board and set its status.

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
            item_id = str(item.get("id", "")) or None
            if not item_id:
                return None
            # Set the status field on the new project item
            await self.move_card(item_id, status)
            return item_id
        except Exception as exc:
            logger.warning("link_to_project_failed", content_id=content_id, error=str(exc))
            return None

    async def request_reviewers(self, owner: str, repo: str, pr_number: int, reviewers: list[str]) -> None:
        """Request reviews from the given GitHub usernames on a PR."""
        if not reviewers:
            return
        token = await self._current_token()
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json={"reviewers": reviewers}, headers=headers)
            if resp.is_success:
                logger.info("reviewers_requested", pr_number=pr_number, reviewers=reviewers)
            else:
                logger.warning("request_reviewers_failed", pr_number=pr_number, status=resp.status_code, body=resp.text[:200])
        except Exception as exc:
            logger.warning("request_reviewers_error", pr_number=pr_number, error=str(exc))

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

    async def get_required_status_checks(
        self, owner: str, repo: str, default_branch: str
    ) -> set[str] | None:
        """075: Return the set of required-status-check contexts for ``default_branch``.

        Used by the implementer CI-gate resolver as fallback layer 2 (between
        persona_check_map and all_head_checks).  Matches branch-protection rules
        by glob pattern (``*`` wildcards) against ``default_branch``.

        Returns:
            - A non-empty set when one or more matching rules declare required
              status checks.
            - An empty set when matching rules exist but declare none.
            - ``None`` when the response shape indicates the token cannot read
              branch protection (e.g. lacks admin:read scope) — callers should
              treat this as "unreadable" and fall through to the next resolver
              layer.
        """
        try:
            result = await self._guarded_execute(
                GET_BRANCH_PROTECTION_QUERY, {"owner": owner, "repo": repo}
            )
        except Exception:
            return None

        repo_block = (result or {}).get("repository") or {}
        bpr_block = repo_block.get("branchProtectionRules")
        if bpr_block is None:
            return None

        required: set[str] = set()
        for rule in (bpr_block.get("nodes") or []):
            if not isinstance(rule, dict):
                continue
            pattern = rule.get("pattern") or ""
            if not pattern:
                continue
            if pattern != default_branch and (
                "*" not in pattern
                or not fnmatch.fnmatchcase(default_branch, pattern)
            ):
                continue
            for rsc in (rule.get("requiredStatusChecks") or []):
                ctx = (rsc or {}).get("context")
                if ctx:
                    required.add(str(ctx))
        return required

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

    async def get_file_blob_sha(
        self, owner: str, repo: str, path: str, ref: str = "HEAD"
    ) -> str | None:
        expression = f"{ref}:{path}"
        result = await self._guarded_execute(
            GET_FILE_BLOB_SHA_QUERY,
            {"owner": owner, "repo": repo, "expression": expression},
        )
        obj = result.get("repository", {}).get("object")
        if isinstance(obj, dict):
            return obj.get("oid")
        return None

    async def list_prs_by_branch_prefix(
        self,
        owner: str,
        repo: str,
        prefix: str,
        *,
        state: str = "OPEN",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """076 (T110, FR-024): list open PRs whose head ref starts with
        ``prefix``.  Used by multi-PR divergence detection — if more than
        one open PR matches ``coordinare/<card_node_id>/``, the card is
        divergent and dispatch must refuse until the operator resolves.

        Returns a list of ``{"number", "url", "head_ref"}`` dicts.  Empty
        list on any error or zero matches (callers treat as "no
        divergence detected").

        Uses GraphQL because REST does not support arbitrary head-ref
        prefix filtering.  Client-side filters the result by branch
        prefix since GraphQL does not have a native prefix filter
        either; the ``limit=20`` cap is the practical maximum.
        """
        query = (
            "query($owner:String!,$repo:String!,$states:[PullRequestState!]) {"
            " repository(owner:$owner, name:$repo) {"
            "  pullRequests(first:50, states:$states, orderBy:{field:CREATED_AT,direction:DESC}) {"
            "   nodes { number url headRefName }"
            " }}}"
        )
        variables: dict[str, Any] = {
            "owner": owner,
            "repo": repo,
            "states": [state],
        }
        try:
            result = await self._guarded_execute(query, variables)
        except Exception as exc:
            logger.warning(
                "github.list_prs_by_branch_prefix_failed",
                owner=owner,
                repo=repo,
                prefix=prefix,
                error=str(exc),
            )
            return []

        nodes = (
            result.get("repository", {}).get("pullRequests", {}).get("nodes", [])
            if isinstance(result, dict)
            else []
        )
        if not isinstance(nodes, list):
            return []
        matches: list[dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            head_ref = node.get("headRefName")
            if not isinstance(head_ref, str) or not head_ref.startswith(prefix):
                continue
            matches.append({
                "number": node.get("number"),
                "url": node.get("url"),
                "head_ref": head_ref,
            })
            if len(matches) >= limit:
                break
        return matches
