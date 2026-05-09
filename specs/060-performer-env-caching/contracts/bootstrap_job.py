"""Contract: BootstrapJobPayload — sent to env_bootstrap performers.

This is the canonical shape sent to an env_bootstrap performer as the job
dispatch payload. It extends the existing JobDispatch protocol with a
``job_type = "env_bootstrap"`` discriminator.

The performer image MUST accept this payload on ``POST /jobs`` and MUST
mount-point at ``cache_mount_path`` to install tooling into the env cache.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class BootstrapJobPayload(BaseModel):
    """Dispatch payload for an env_bootstrap performer job."""

    job_type: Literal["env_bootstrap"] = "env_bootstrap"

    symphony_name: str = Field(
        description="Human-readable symphony name (for logging/labelling inside the container)."
    )
    symphony_org: str = Field(description="GitHub organisation owning the symphony's repo.")
    symphony_repo: str = Field(description="GitHub repository name for the symphony.")

    env_spec_files: list[str] = Field(
        default_factory=lambda: ["README.md"],
        description="Relative paths of the files whose content describes the dev environment.",
    )
    env_spec_contents: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of file path → full text content at the time of the SHA change.",
    )

    cache_mount_path: str = Field(
        default="/devenv/symphony",
        description=(
            "Full in-container path to the symphony's env cache subdirectory, e.g. "
            "'/devenv/my-project-a1b2c3'. Coordinare sets this to "
            "'{container_devenv_root}/{sanitised_symphony_name}'. The performer MUST "
            "install all tooling under this path. The default shown here is illustrative; "
            "the actual value is always computed by coordinare from config."
        ),
    )
