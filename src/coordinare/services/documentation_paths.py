from __future__ import annotations

# Mirrored from performer.workflows.documenter.models, guarded by a parity test.
DOC_PATH_PREFIXES = ("docs/", "doc/")


def is_documentation_path(path: object) -> bool:
    return (
        isinstance(path, str)
        and path.startswith(DOC_PATH_PREFIXES)
        and not any(part in {"", ".", ".."} for part in path.split("/"))
        and "\\" not in path
    )
