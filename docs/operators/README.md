# Operator Documentation

How-to guides for running coordinare + performer in production.

## LLM provider routing

- [LiteLLM Proxy for the `claude_code` Backend](litellm-proxy.md) — route the `claude_code` performer backend through an operator-supplied LiteLLM proxy (spec 073).

## Deployment topology

- [Containerized Performers](containerized-performers.md) — isolate performer execution from the coordinare host.

## Service inference (env bootstrap)

- [Rails Symphony Quickstart](service-inference-rails-quickstart.md) — end-to-end walkthrough for a Rails symphony using service inference.
- [Manual Override via `.coordinare/score.json`](service-inference-manual-override.md) — bypass the LLM-driven pass when you need deterministic service config.
