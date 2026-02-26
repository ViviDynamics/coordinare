from __future__ import annotations


class KubernetesTransport:
    def __init__(self) -> None:
        raise NotImplementedError(
            "Kubernetes transport is defined but not yet implemented. "
            "Set agent_transport: subprocess in your configuration."
        )
