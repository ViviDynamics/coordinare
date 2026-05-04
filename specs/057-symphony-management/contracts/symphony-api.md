# API Contracts: Symphony Management Endpoints

**Status**: Implemented  
**Version**: 1.1  
**Last Updated**: 2026-05-01

## Endpoint: GET /api/symphonies

**Purpose**: List all configured symphonies with current runtime state.

### Request

```http
GET /api/symphonies HTTP/1.1
Host: coordinare:8000
Accept: application/json
```

### Response: 200 OK

```json
{
  "symphonies": [
    {
      "name": "backend",
      "priority": 0,
      "github_project_number": 43,
      "cycle_count": 456,
      "error_count": 2,
      "last_error": "GitHub API rate limit (429)",
      "last_poll_at": "2026-04-30T15:30:45Z"
    },
    {
      "name": "frontend",
      "priority": 1,
      "github_project_number": 42,
      "cycle_count": 201,
      "error_count": 0,
      "last_error": null,
      "last_poll_at": "2026-04-30T15:30:44Z"
    }
  ],
  "config_version": 3
}
```

### Response: 503 Service Unavailable

```json
{
  "error": "daemon_not_ready",
  "details": "Coordinare daemon is starting up"
}
```

---

## Endpoint: GET /api/symphonies/{name}

**Purpose**: Get detailed state for a single symphony.

### Request

```http
GET /api/symphonies/backend HTTP/1.1
Host: coordinare:8000
Accept: application/json
```

### Response: 200 OK

```json
{
  "name": "backend",
  "github_project_number": 43,
  "state": {
    "cycle_count": 456,
    "error_count": 2,
    "last_error": "GitHub API rate limit (429)",
    "last_poll_at": "2026-04-30T15:30:45Z",
    "active_card": {
      "id": "GH-42",
      "title": "Fix auth bug",
      "column": "IN_PROGRESS",
      "url": "https://github.com/orgs/acme-corp/projects/43?pane=issue&itemId=123"
    },
    "board_snapshot": {
      "TODO": 12,
      "IN_PROGRESS": 2,
      "IN_REVIEW": 3,
      "DONE": 45
    }
  }
}
```

`state` is `null` if the symphony has never been polled.

### Response: 404 Not Found

```json
{
  "error": "Symphony 'backend' not found"
}
```

---

## Endpoint: PUT /api/symphonies/{name}

**Purpose**: Update a single symphony's overrides and/or personas.

### Request

```http
PUT /api/symphonies/backend HTTP/1.1
Host: coordinare:8000
Content-Type: application/json

{
  "overrides": {
    "assignee_filter": "coordinare-backend-v2",
    "performer_timeout_seconds": 400
  },
  "personas": {
    "main-persona": {
      "backend": "claude_code",
      "instructions": "New persona instructions"
    }
  }
}
```

### Response: 200 OK

```json
{
  "name": "backend",
  "overrides": {
    "assignee_filter": "coordinare-backend-v2",
    "performer_timeout_seconds": 400
  },
  "personas": {
    "main-persona": {
      "backend": "claude_code",
      "instructions": "New persona instructions"
    }
  }
}
```

### Response: 400 Bad Request

```json
{
  "error": "Invalid symphony configuration"
}
```

### Response: 404 Not Found

```json
{
  "error": "Symphony 'backend' not found"
}
```

### Response: 409 Conflict (Cycle In Progress)

```json
{
  "status": "cycle_in_progress"
}
```

---

## Endpoint: DELETE /api/symphonies/{name}

**Purpose**: Remove a symphony from configuration.

### Request

```http
DELETE /api/symphonies/infra HTTP/1.1
Host: coordinare:8000
```

### Response: 200 OK

```json
{
  "deleted": "infra"
}
```

(Symphony removed, `config_version` incremented, next cycle skips this symphony.)

### Response: 404 Not Found

```json
{
  "error": "Symphony 'infra' not found"
}
```

### Response: 409 Conflict (Last Symphony)

```json
{
  "error": "Cannot delete the last symphony"
}
```

### Response: 409 Conflict (Cycle In Progress)

```json
{
  "status": "cycle_in_progress"
}
```

---

## Endpoint: POST /api/config/reload

**Purpose**: Trigger hot-reload of configuration from disk.

### Request

```http
POST /api/config/reload HTTP/1.1
Host: coordinare:8000
```

No request body required.

### Response: 202 Accepted

```json
{
  "status": "reload_triggered",
  "message": "Configuration reload in progress"
}
```

### Response: 501 Not Implemented

```json
{
  "error": "Config reload not supported"
}
```

(Returned if the daemon was not started with reload support enabled.)

---

## Endpoint: GET /api/config/effective

**Purpose**: Get the effective global configuration and orchestration mode.

### Request

```http
GET /api/config/effective HTTP/1.1
Host: coordinare:8000
```

**Query parameters** (all optional):

| Parameter  | Type   | Description                                                     |
|------------|--------|-----------------------------------------------------------------|
| `symphony` | string | If provided, return the effective config scoped to this symphony. |

### Response: 200 OK (global)

When `symphony` is omitted:

```json
{
  "github_org": "acme-corp",
  "github_project_number": 0,
  "project_name": "Demo",
  "mode": "multi_symphony"
}
```

`mode` is `"multi_symphony"` when the coordinare config includes a `symphonies` section, otherwise `"legacy"`.

### Response: 200 OK (symphony-scoped)

When `?symphony=<name>` is provided and the symphony exists:

```json
{
  "github_org": "acme-corp",
  "github_project_number": 43,
  "project_name": "Demo",
  "symphony": "backend",
  "mode": "symphony"
}
```

`mode` is always `"symphony"` in this variant.

### Response: 404 Not Found

```json
{
  "error": "Symphony 'backend' not found"
}
```

(Returned when `?symphony=<name>` is specified but no matching symphony is configured.)

### Response: 500 Internal Server Error

```json
{
  "error": "Config not available"
}
```

(Returned if the daemon has not yet loaded its configuration.)

```json
{
  "error": "Failed to compute effective config for this symphony"
}
```

(Returned when the `?symphony=` variant encounters an unexpected error resolving overrides.)

---

## Endpoint: POST /api/symphonies/{name}/validate

**Purpose**: Validate a symphony's effective configuration without saving.

### Request

```http
POST /api/symphonies/backend/validate HTTP/1.1
Host: coordinare:8000
Content-Type: application/json

{
  "overrides": {
    "performer_timeout_seconds": 400
  }
}
```

Body is optional. If omitted, the symphony's current configuration is validated.

### Response: 200 OK (Valid)

```json
{
  "valid": true,
  "symphony": "backend",
  "effective_config": {
    "github_org": "acme-corp",
    "github_project_number": 43,
    "project_name": "Backend Project"
  }
}
```

### Response: 400 Bad Request (Invalid)

```json
{
  "valid": false,
  "errors": [
    {
      "type": "value_error",
      "loc": ["performer_timeout_seconds"],
      "msg": "Input should be greater than or equal to 60",
      "input": 10
    }
  ]
}
```

### Response: 400 Bad Request (Malformed JSON)

```json
{
  "error": "Invalid JSON body"
}
```

### Response: 404 Not Found

```json
{
  "error": "Symphony 'backend' not found"
}
```

---

## Error Response Schema (All Endpoints)

Most error responses use:

```json
{
  "error": "Human-readable error message"
}
```

Some 409 responses use `status` instead of `error`:

```json
{
  "status": "cycle_in_progress"
}
```

### HTTP Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success, body contains result |
| 202 | Request accepted, processing async |
| 400 | Bad request (validation error, malformed JSON) |
| 404 | Resource not found |
| 409 | Conflict (cycle in progress, last symphony removal) |
| 500 | Internal error (config not loaded) |
| 501 | Not implemented (reload not supported) |
| 503 | Service unavailable (daemon starting) |
