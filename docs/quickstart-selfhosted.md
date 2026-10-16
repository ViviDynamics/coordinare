# Quickstart with a self-hosted model

Follow the [installation quickstart](quickstart.md) for the installer, GitHub
board, token and security requirements. Install git and OpenCode for local mode, or use Docker performers. Your repository's code reaches the model
endpoint, so choose an endpoint you trust. Read the
[threat model](security/threat-model.md).

Start an OpenAI-compatible server such as vLLM, LM Studio, Ollama's compatibility
API or LiteLLM. Note its exact model identifier and API base URL. The base URL
must serve `/models` and `/chat/completions`; include `/v1` if your server needs
it. Do not append `/chat/completions` to the base URL.

For example, with a server at `http://127.0.0.1:1234/v1` serving a model named
`your-model-name`, run:

```sh
coordinare init --harness opencode \
  --endpoint http://127.0.0.1:1234/v1 --model your-model-name
```

Replace the URL and model with your server's values. Supply its key at the
hidden API key prompt if it requires authentication. An endpoint without
authentication can omit the key. An exported or saved `OPENAI_API_KEY` is reused,
so remove an unrelated provider key from the environment before setup.

Init writes a `starter` endpoint with `kind: litellm`, a `starter` model endpoint
and a single-model mode. This generic OpenAI-compatible endpoint works with the
`opencode` harness. Models are selected through `endpoints`, `model_endpoints`
and `modes`; inline `performers.<role>.model` is rejected. Use the
[full reference](../config.example.yaml) for advanced orchestration. Init enables
only implementer; the older full example files enable additional roles.

Init checks endpoint reachability and the model list, then offers a login
service or foreground start. To start later:

```sh
coordinare
```

Open <http://127.0.0.1:8090> and try the [first card](first-launch.md#try-one-card).
Before rerunning doctor, stop the daemon to free its dashboard port:

```sh
coordinare doctor
```

## Docker performers

Choose Docker during init, or use:

```sh
coordinare init --run-mode docker --harness opencode \
  --endpoint http://192.168.1.10:1234/v1 --model your-model-name
```

Replace the example LAN address with a server reachable from both the host and
the containers. Container `localhost` refers to the container. A host alias such
as `host.docker.internal` can work inside Docker but may not resolve on the Linux
host, where preflight also runs. See [Docker setup](docker-mode-setup.md) for
runtime requirements, image selection, smoke checks and upgrades. Existing setup
files require confirmation in a terminal or `--force` without one.

## Troubleshooting

For endpoint or model failures, confirm the base URL, credentials and exact model
name. Doctor prints a fix for each failed check. A reachable models API does not
prove the model can complete a card. See [setup troubleshooting](setup-troubleshooting.md).
For the separate Claude Code route through an Anthropic-compatible LiteLLM proxy,
use the [proxy operator guide](operators/litellm-proxy.md).
