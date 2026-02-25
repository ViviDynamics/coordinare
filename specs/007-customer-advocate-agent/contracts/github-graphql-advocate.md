# Contract: GitHub GraphQL — Customer Advocate Queries & Mutations

**Feature**: 007-customer-advocate-agent
**Service**: `src/coordinare/services/github.py` (extensions)
**Date**: 2026-02-24

---

## LIST_OPEN_ISSUES_QUERY

Fetches the most recently created open issues, including their labels. Used to identify issues not yet handled by the advocate.

```graphql
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
        labels(first: 10) {
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
```

**Variables**:
- `owner`: GitHub organization login (e.g., `"ViviDynamics"`)
- `repo`: Repository name from `advocate.github_repo` config
- `first`: Issues per page (default: 20, max: 20 per advocate cycle per SC-006)
- `cursor`: Pagination cursor; null for first page

**Used by**: `GitHubService.list_open_issues(owner, repo, first=20)`

**Client-side filtering**: After fetching, filter out issues where labels include `advocate.handled_label` or `advocate.escalation_label`. Also skip issue IDs present in `advocate_history` (secondary dedup guard).

**Rate cost**: ~1–2 GraphQL points per call.

---

## GET_FILE_CONTENT_QUERY

Fetches a repository file's text content at a given git ref. Used to load documentation sources for grounding advocate responses.

```graphql
query GetFileContent($owner: String!, $repo: String!, $expression: String!) {
  repository(owner: $owner, name: $repo) {
    object(expression: $expression) {
      ... on Blob {
        text
      }
    }
  }
}
```

**Variables**:
- `owner`: GitHub organization login
- `repo`: Repository name
- `expression`: `"{ref}:{path}"` (e.g., `"main:README.md"`)

**Returns**: `text` field containing file contents as a UTF-8 string; `null` for binary files or missing paths.

**Used by**: `GitHubService.get_file_content(owner, repo, path, ref="main")`

**On null result**: Mark `DocumentationSource.reachable = False`. Log structured warning. Treat all issues in that cycle as `no_documentation_configured` or `no_documentation_match` depending on whether any sources are reachable.

**Rate cost**: ~1 GraphQL point per file.

---

## GET_REPOSITORY_ID_QUERY

Fetches the repository node ID required for `createLabel` mutation. Called once at startup.

```graphql
query GetRepositoryId($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    id
  }
}
```

**Variables**:
- `owner`: GitHub organization login
- `repo`: Repository name

**Used by**: `GitHubService.get_repository_id(owner, repo)` — result cached as `self.repository_id`

**Rate cost**: ~1 GraphQL point.

---

## GET_LABEL_IDS_QUERY

Fetches all label node IDs for the repository. Used to build the label cache at startup.

```graphql
query GetLabelIds($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    labels(first: 50) {
      nodes {
        id
        name
      }
    }
  }
}
```

**Variables**:
- `owner`: GitHub organization login
- `repo`: Repository name

**Used by**: `GitHubService.get_label_ids(owner, repo)` — returns `dict[str, str]` (name → node ID)

**Rate cost**: ~1 GraphQL point.

---

## CREATE_LABEL_MUTATION

Creates a label in the repository if it does not already exist. Called at startup by `ensure_labels_exist()` for `advocate-handled` and `needs-human`.

```graphql
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
```

**Variables**:
- `repositoryId`: Repository node ID (from `GET_REPOSITORY_ID_QUERY`)
- `name`: Label name (e.g., `"advocate-handled"`)
- `color`: Hex color without `#` (defaults: `"0075ca"` for `advocate-handled`; `"e4e669"` for `needs-human`)
- `description`: Human-readable description of the label's purpose

**Used by**: `GitHubService.ensure_labels_exist(owner, repo, handled_label, escalation_label)`

**Idempotent behaviour**: Call `get_label_ids()` first. Only call `createLabel` for names not already present.

**Rate cost**: ~1 GraphQL point per label created.

---

## ADD_LABELS_MUTATION

Applies one or more labels to an issue. This is the core of the label-first approach (FR-010).

```graphql
mutation AddLabels($labelableId: ID!, $labelIds: [ID!]!) {
  addLabelsToLabelable(
    input: { labelableId: $labelableId, labelIds: $labelIds }
  ) {
    labelable {
      ... on Issue {
        id
        labels(first: 10) {
          nodes {
            id
            name
          }
        }
      }
    }
  }
}
```

**Variables**:
- `labelableId`: Issue GraphQL node ID (from `LIST_OPEN_ISSUES_QUERY`)
- `labelIds`: List containing the appropriate label node ID — either `handled_label_id` or `escalation_label_id`

**Used by**: `GitHubService.add_labels(issue_id, label_ids)`

**Ordering**: MUST be called before posting any comment on the issue (label-first approach per FR-010). The existing `add_comment` method handles the subsequent comment post.

**Rate cost**: ~1 GraphQL point per mutation.

---

## Rate Limit Estimates

| Operation | Points | Per-cycle calls (20 issues, 2 doc files) | Total points |
|-----------|--------|------------------------------------------|--------------|
| `ListOpenIssues` | ~2 | 1 | 2 |
| `GetFileContent` | ~1 | 2 | 2 |
| `AddLabels` (per issue) | ~1 | up to 20 | 20 |
| `AddComment` (per actionable issue) | ~1 | up to 20 | 20 |

**Worst-case per cycle**: ~44 points. Well within the 5,000 points/hour budget.

**Startup-only operations** (not counted per cycle):
- `GetRepositoryId`: ~1 point
- `GetLabelIds`: ~1 point
- `CreateLabel` (if missing): ~1 point each
