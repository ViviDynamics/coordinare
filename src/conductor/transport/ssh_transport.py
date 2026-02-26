from __future__ import annotations


class SshTransport:
    def __init__(self) -> None:
        raise NotImplementedError(
            "SSH transport is defined but not yet implemented. "
            "Set agent_transport: subprocess in your configuration."
        )
