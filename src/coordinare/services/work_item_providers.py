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

from typing import TYPE_CHECKING, Any

from coordinare.services.board_provider import BoardProvider, GitHubProjectsBoardProvider

if TYPE_CHECKING:
    from collections.abc import Callable

    from coordinare.config import WorkItemsConfig

#: The provider a symphony with no ``work_items`` block uses.
DEFAULT_WORK_ITEM_PROVIDER = "github_projects"


def _github_projects(_config: WorkItemsConfig | None, github: Any) -> BoardProvider:
    # The project is the symphony's own ``github_project_number``, which the
    # GitHub service already carries, so the block needs no fields of its own.
    return GitHubProjectsBoardProvider(github)


#: Provider name -> builder. The builder receives the block (``None`` when the
#: symphony has none) and the symphony's GitHub service, the code host.
WORK_ITEM_PROVIDERS: dict[str, Callable[[WorkItemsConfig | None, Any], BoardProvider]] = {
    DEFAULT_WORK_ITEM_PROVIDER: _github_projects,
}

#: The names config accepts for ``work_items.provider``.
WORK_ITEM_PROVIDER_NAMES: tuple[str, ...] = tuple(WORK_ITEM_PROVIDERS)


def build_board_provider(work_items: WorkItemsConfig | None, *, github: Any) -> BoardProvider:
    """Build the board for a symphony's ``work_items`` block (``None``: GitHub Projects).

    *github* is the symphony's GitHub service. It is the code host for every
    provider, and the board itself for GitHub Projects.
    """
    name = work_items.provider if work_items is not None else DEFAULT_WORK_ITEM_PROVIDER
    builder = WORK_ITEM_PROVIDERS.get(name)
    if builder is None:
        # Config refuses an unknown name before this can run; this is the guard
        # for a caller that built a block by hand.
        msg = f"unknown work item provider {name!r}; known: {', '.join(WORK_ITEM_PROVIDER_NAMES)}"
        raise ValueError(msg)
    return builder(work_items, github)
