"""Actions failure evidence: annotations and the failing step's workflow context."""
from __future__ import annotations

import base64
import re
from typing import Any

import httpx
import yaml

_SETUP_ACTIONS = ('actions/checkout', 'actions/setup-', 'actions/cache', 'ruby/setup-ruby',
                  'docker/login-action', 'docker/setup-buildx-action', 'docker/setup-qemu-action')


def failed_step_context(job: dict[str, Any], workflow: dict[str, Any]) -> tuple[list[str], bool]:
    """Identify failed setup steps; a repository run is never inferred to be setup."""
    failed = [step for step in job.get('steps', []) if isinstance(step, dict) and step.get('conclusion') == 'failure']
    names = [str(step.get('name') or '') for step in failed]
    platform = any(name.lower() == 'set up job' or any(action in name.lower() for action in _SETUP_ACTIONS) for name in names)
    jobs = workflow.get('jobs') or {}
    if not isinstance(jobs, dict):
        return names, platform
    rendered_name = str(job.get('name') or '')
    matching = []
    for key, definition in jobs.items():
        if not isinstance(definition, dict):
            continue
        template = str(definition.get('name') or key)
        # Only matrix expressions are variable here; escape all literal text.
        parts = re.split(r'(\$\{\{\s*matrix\.[^{}]+\}\})', template)
        pattern = ''.join('.+?' if part.startswith('${{') else re.escape(part) for part in parts)
        strategy = definition.get('strategy') or {}
        if 'name' not in definition and isinstance(strategy, dict) and strategy.get('matrix'):
            pattern += r' \(.+\)'
        if rendered_name in (key, template) or re.fullmatch(pattern, rendered_name):
            matching.append(definition)
    if not matching and len(jobs) == 1:
        matching = [definition for definition in jobs.values() if isinstance(definition, dict)]
    # Duplicate/overlapping rendered names cannot identify a workflow job safely.
    if len(matching) != 1:
        return names, platform
    for definition in matching:
        saw_run = False
        for step in definition.get('steps', []):
            if not isinstance(step, dict):
                continue
            name = str(step.get('name') or f"Run {step.get('uses', '')}")
            if name in names:
                action = str(step.get('uses') or '').lower()
                platform |= bool(action and (not saw_run or any(action.startswith(prefix) for prefix in _SETUP_ACTIONS)))
            saw_run |= 'run' in step
    return names, platform


async def fetch_failure_evidence(
    api_base: str, token: str, owner: str, repo: str, check_id: int, details_url: str | None,
) -> dict[str, Any]:
    """Read bounded, repository-scoped REST endpoints, retaining partial evidence."""
    root = f'{api_base}/repos/{owner}/{repo}'
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
    annotations: list[str] = []
    remaining_chars = 32_000
    names: list[str] = []
    platform = False
    access_denied = False
    async with httpx.AsyncClient(headers=headers, timeout=10) as client:
        async def get(url: str, **kwargs: Any) -> Any:
            nonlocal access_denied
            try:
                response = await client.get(url, **kwargs)
                if response.status_code in (401, 403):
                    access_denied = True
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError):
                return None

        if check_id:
            for page in range(1, 11):
                rows = await get(f'{root}/check-runs/{int(check_id)}/annotations', params={'per_page': 100, 'page': page})
                if not isinstance(rows, list):
                    break
                for row in rows:
                    if not isinstance(row, dict) or row.get('annotation_level') != 'failure':
                        continue
                    message = str(row.get('message') or '')[:min(4000, remaining_chars)]
                    annotations.append(message)
                    remaining_chars -= len(message)
                    if not remaining_chars or len(annotations) >= 50:
                        break
                if len(rows) < 100 or not remaining_chars or len(annotations) >= 50:
                    break
        match = re.search(r'/actions/runs/(\d+)/job/(\d+)', details_url or '')
        if match:
            run_id, job_id = match.groups()
            job = await get(f'{root}/actions/jobs/{job_id}')
            if isinstance(job, dict):
                names, platform = failed_step_context(job, {})
                run = await get(f'{root}/actions/runs/{run_id}')
                if isinstance(run, dict):
                    path = str(run.get('path') or '').split('@', 1)[0]
                    if path.startswith('.github/workflows/') and '..' not in path.split('/') and run.get('head_sha'):
                        raw = await get(f'{root}/contents/{path}', params={'ref': run['head_sha']})
                        try:
                            document = yaml.safe_load(base64.b64decode(raw['content'])) if isinstance(raw, dict) else None
                            if isinstance(document, dict):
                                names, platform = failed_step_context(job, document)
                        except (KeyError, TypeError, ValueError, yaml.YAMLError):
                            pass
    return {'annotations': annotations, 'failed_steps': names, 'setup_failure': platform, 'access_denied': access_denied}
