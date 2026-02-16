from __future__ import annotations

import inspect
from typing import Any

from gql import Client, gql
from gql.transport.aiohttp import AIOHTTPTransport

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


class GitHubService:
    """Async GitHub GraphQL service with field and option caching."""

    def __init__(
        self,
        token: str,
        org: str,
        project_number: int,
        endpoint: str = "https://api.github.com/graphql",
    ) -> None:
        self._token = token
        self._org = org
        self._project_number = project_number
        self._endpoint = endpoint

        self._client: Client | None = None
        self.project_id: str | None = None
        self.project_title: str | None = None
        self.field_cache: dict[str, Any] = {}

    def _build_client(self) -> Client:
        transport = AIOHTTPTransport(
            url=self._endpoint,
            headers={"Authorization": f"bearer {self._token}"},
        )
        return Client(transport=transport, fetch_schema_from_transport=False)

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            self._client = self._build_client()
        result_or_awaitable = self._client.execute(gql(query), variable_values=variables)
        if inspect.isawaitable(result_or_awaitable):
            result = await result_or_awaitable
        else:
            result = result_or_awaitable
        if not isinstance(result, dict):
            msg = "GitHub GraphQL response must be a JSON object"
            raise ValueError(msg)
        return result

    async def initialize(self) -> None:
        project_result = await self._execute(
            FIND_PROJECT_QUERY,
            {"org": self._org, "number": self._project_number},
        )
        project = project_result.get("organization", {}).get("projectV2")
        if not isinstance(project, dict) or "id" not in project:
            msg = "Project not found for provided organization and number"
            raise ValueError(msg)

        self.project_id = str(project["id"])
        self.project_title = str(project.get("title", ""))

        fields_result = await self._execute(
            GET_PROJECT_FIELDS_QUERY,
            {"projectId": self.project_id},
        )
        nodes = (
            fields_result.get("node", {})
            .get("fields", {})
            .get("nodes", [])
        )
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

    async def poll_board(self) -> dict[str, Any]:
        raise NotImplementedError

    async def get_issue_details(self, issue_id: str) -> dict[str, Any]:
        _ = issue_id
        raise NotImplementedError

    async def move_card(self, item_id: str, status: str) -> None:
        _ = (item_id, status)
        raise NotImplementedError
