# Contract: GitHub Projects GraphQL API

**Feature**: 001-board-orchestrator
**Date**: 2026-02-16
**Service**: GitHub GraphQL API v4 (Projects v2)
**Client**: `src/coordinare/services/github.py`

## Authentication

All requests require a Bearer token in the `Authorization` header.

```
POST https://api.github.com/graphql
Authorization: bearer <token>
Content-Type: application/json
```

**Token Types** (FR-015):
- Fine-grained PAT: permissions `projects:rw`, `issues:rw`, `pull_requests:rw`, `contents:read`
- GitHub App installation token (recommended for production): same permissions at org level

---

## Queries

### Q1: Find Project by Number

Used at startup to resolve the project's GraphQL node ID.

```graphql
query FindProject($org: String!, $number: Int!) {
  organization(login: $org) {
    projectV2(number: $number) {
      id
      title
    }
  }
}
```

**Response Shape**:
```json
{
  "organization": {
    "projectV2": {
      "id": "PVT_...",
      "title": "Project Name"
    }
  }
}
```

---

### Q2: Get Status Field Definition

Used at startup to cache the Status field ID and option IDs (column mappings).

```graphql
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
```

**Expected Status Options** (FR-016):
- "ToDo / Backlog" → maps to `CardStatus.TODO`
- "Blocked" → maps to `CardStatus.BLOCKED`
- "In Progress" → maps to `CardStatus.IN_PROGRESS`
- "In Review" → maps to `CardStatus.IN_REVIEW`
- "Done" → maps to `CardStatus.DONE`

**Coordinare must**: Match option names case-insensitively. If a required column is missing, log an error and fail startup.

---

### Q3: Poll Board Items

Lightweight query for the daemon polling loop (~3-5 GraphQL points).

```graphql
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
                optionId
                field {
                  ... on ProjectV2SingleSelectField { id }
                }
              }
            }
          }
          content {
            ... on Issue { id number state }
            ... on PullRequest { id number state }
          }
        }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
```

**Polling Interval**: 30-60 seconds (configurable via `poll_interval_seconds`)

---

### Q4: Get Issue Details

Full issue details for card dispatch (FR-004).

```graphql
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
      comments(first: 50, orderBy: {field: CREATED_AT, direction: DESC}) {
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
```

---

### Q5: Get PR Reviews

Used to monitor reviews and classify human vs bot (FR-005, FR-006).

```graphql
query GetPRReviews($prId: ID!) {
  node(id: $prId) {
    ... on PullRequest {
      reviews(first: 50) {
        nodes {
          id
          author {
            __typename
            login
          }
          state
          body
          submittedAt
        }
      }
      reviewDecision
      mergeable
      mergeStateStatus
    }
  }
}
```

**Classification**: `author.__typename == "Bot"` OR `author.login NOT IN human_reviewers` → ignore (FR-006)

---

### Q6: Check PR Mergeability

Pre-merge validation (FR-008).

```graphql
query CheckMergeability($prId: ID!) {
  node(id: $prId) {
    ... on PullRequest {
      mergeable
      mergeStateStatus
      reviewDecision
    }
  }
}
```

**Merge preconditions**:
- `mergeable == MERGEABLE` (not `CONFLICTING` or `UNKNOWN`)
- `reviewDecision == APPROVED`
- If `CONFLICTING` → move card to BLOCKED, notify team

---

## Mutations

### M1: Update Card Status (Move Column)

```graphql
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
```

---

### M2: Add Comment to Issue/PR

```graphql
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
```

**Usage**: Post clarifying questions when card moves to BLOCKED (FR-013).

---

### M3: Squash-Merge PR

```graphql
mutation MergePR($prId: ID!, $commitHeadline: String, $commitBody: String) {
  mergePullRequest(
    input: {
      pullRequestId: $prId
      mergeMethod: SQUASH
      commitHeadline: $commitHeadline
      commitBody: $commitBody
    }
  ) {
    pullRequest {
      merged
      mergedAt
      mergeCommit { oid message }
      url
    }
  }
}
```

---

## Rate Limiting

- **Budget**: 5,000 points/hour
- **Cost estimation**: Include `rateLimit { cost remaining resetAt }` in every query
- **Backoff**: If `remaining < 100`, increase poll interval to 120s until reset
- **Error handling**: On 403 rate-limit response, wait until `resetAt` timestamp
