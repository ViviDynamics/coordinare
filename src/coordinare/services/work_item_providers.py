"""Which work item provider a symphony reads, and the one place it is built (#394).

A symphony names its provider in an optional ``work_items`` block. No block means
GitHub Projects with the symphony's existing fields, so every config written before
this loads and behaves as it did.

Construction lives here and nowhere else. ``__main__`` and the daemon's
per-symphony swap call ``build_board_provider``; a test asserts that nothing else
in ``src/coordinare`` constructs a provider, because a second construction site is
how a configured board gets silently replaced by GitHub.

Adding a provider means adding its name and builder to ``WORK_ITEM_PROVIDERS``.
Config accepts exactly the names registered here.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from coordinare.services.board_provider import (
    BoardProvider,
    GitHubProjectsBoardProvider,
    done_archive_days,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from coordinare.config import WorkItemsConfig

#: The provider a symphony with no ``work_items`` block uses.
DEFAULT_WORK_ITEM_PROVIDER = "github_projects"


def _github_projects(
    config: WorkItemsConfig | None, github: Any, provider_state: dict[str, Any] | None = None,
) -> BoardProvider:
    # The project is the symphony's own ``github_project_number``, which the
    # GitHub service already carries. 365: the block's one GitHub setting is how
    # long a work item stays in Done before the project archives it.
    return GitHubProjectsBoardProvider(
        github, archive_done_after_days=done_archive_days(config), provider_state=provider_state,
    )


#: Provider name -> builder. The builder receives the block (``None`` when the
#: symphony has none) and the symphony's GitHub service, the code host. A builder
#: may also take ``provider_state`` (see ``build_board_provider``).
WORK_ITEM_PROVIDERS: dict[str, Callable[..., BoardProvider]] = {
    DEFAULT_WORK_ITEM_PROVIDER: _github_projects,
}

#: The names config accepts for ``work_items.provider``.
WORK_ITEM_PROVIDER_NAMES: tuple[str, ...] = tuple(WORK_ITEM_PROVIDERS)


def build_board_provider(
    work_items: WorkItemsConfig | None, *, github: Any, provider_state: dict[str, Any] | None = None,
) -> BoardProvider:
    """Build the board for a symphony's ``work_items`` block (``None``: GitHub Projects).

    *github* is the symphony's GitHub service. It is the code host for every
    provider, and the board itself for GitHub Projects.

    *provider_state* (365) is the symphony's durable state for its provider, a dict the
    daemon persists in its snapshot. Only the board for a symphony's own cycle is given
    one, and only that board does housekeeping on the tracker (GitHub Projects archives
    finished items). It goes only to a builder that takes it.
    """
    name = work_items.provider if work_items is not None else DEFAULT_WORK_ITEM_PROVIDER
    builder = WORK_ITEM_PROVIDERS.get(name)
    if builder is None:
        # Config refuses an unknown name before this can run; this is the guard
        # for a caller that built a block by hand.
        msg = f"unknown work item provider {name!r}; known: {', '.join(WORK_ITEM_PROVIDER_NAMES)}"
        raise ValueError(msg)
    if provider_state is not None and "provider_state" in inspect.signature(builder).parameters:
        return builder(work_items, github, provider_state=provider_state)
    return builder(work_items, github)
