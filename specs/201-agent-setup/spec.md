# Agent setup

Provide one ordered setup procedure at docs/setup-for-agents.md using the release
installer and noninteractive init answers. Ask for repository, board, harness,
reviewers and credentials without displaying secrets. Keep the dashboard local.

Extend doctor and config validate with schema version 1 JSON reports and provide
validate as a command alias. Reports contain stable check categories, instance
numbers, statuses, messages and fixes. Never serialize raw config, credentials,
URLs, exception text or subprocess output. Preserve existing prose output.

Publish an answers JSON Schema and a complete credential-free YAML example.
Validate the example and exercise init with external boundaries stubbed in the
normal unit suite. Link the guide from README, AGENTS and root llms.txt.

Release validation requires a recorded clean machine run using only the guide.
Landing site publication requires its separate source or deployment owner.
