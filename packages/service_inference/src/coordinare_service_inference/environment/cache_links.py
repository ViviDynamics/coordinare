"""Which links in an environment cache leave it.

The cache is the only part of an installer's work a clean consumer keeps, so a link in it must resolve inside it. The
one exception is the pinned base image's own system directories: the clean consumer runs the same image, and an
installer's writes outside the cache are discarded, so a link into them names the same immutable file there (a Python
virtual environment's interpreter link is the common case; nare-native-032 was refused twice for one).

Provenance (coordinare.services.env_provenance) refuses a cache holding any other escaping link; the installer runner
reports one as soon as the install command that created it finishes, while a repair can still correct it.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import NamedTuple

MAX_LINK_HOPS = 40
# Directories of the pinned base image a cache link may point into (absolute, normalized, no '..').
BASE_IMAGE_LINK_PREFIXES = ('/usr/', '/bin/', '/sbin/', '/lib/', '/lib64/')
OUTSIDE = 'points outside the cache'
LINK_GUIDANCE = ('Nothing in {cache} may link outside it, except into the base image\'s own system directories '
                 '(/usr, /bin, /sbin, /lib, /lib64), which the clean consumer shares: an environment or tool that links '
                 'to an interpreter, library or file anywhere else (the source checkout, scratch, a temporary or home '
                 'directory) must copy it into {cache} or be installed there. A link to something inside {cache} is '
                 'relative (ln -sr, or ../ from where the link lives): the cache is mounted at another path for the '
                 'clean consumer, so an absolute path into it, as some installers write, leaves it too.')


class EscapingLink(NamedTuple):
    rel: str
    target: str
    reason: str


def base_image_target(target: str) -> bool:
    """An absolute link target inside the pinned base image's system directories."""
    if not target.startswith('/') or '/../' in f'{target}/' or '/./' in f'{target}/':
        return False
    return any(target.startswith(prefix) or f'{target}/' == prefix for prefix in BASE_IMAGE_LINK_PREFIXES)


def link_refusal(rel: str, target: str, links: dict[str, str]) -> str | None:
    """Why a link leaves the cache or cannot be resolved, or None when it stays inside or names the base image.

    Each component of the target is resolved in turn. A component that names another link in the cache is followed
    through that link's own target, so a chain of links cannot reach outside by way of a ``..`` that only the kernel
    would apply. The hop count bounds the chain.
    """
    if not target:
        return OUTSIDE
    if target.startswith('/'):
        return None if base_image_target(target) else OUTSIDE
    current = list(PurePosixPath(rel).parent.parts)
    stack = list(reversed(target.split('/')))
    hops = 0
    while stack:
        part = stack.pop()
        if part in ('', '.'):
            continue
        if part == '..':
            if not current:
                return OUTSIDE
            current.pop()
            continue
        key = '/'.join([*current, part])
        if key in links:
            hops += 1
            if hops > MAX_LINK_HOPS:
                return f'is a link loop (more than {MAX_LINK_HOPS} hops)'
            through = links[key]
            if not through:
                return OUTSIDE
            if through.startswith('/'):
                return None if base_image_target(through) and not stack else OUTSIDE
            stack.extend(reversed(through.split('/')))
            continue
        current.append(part)
    return None


def escaping_links(links: dict[str, str]) -> list[EscapingLink]:
    """Every link of ``links`` (cache-relative path to target) that leaves the cache, in path order."""
    return [EscapingLink(rel, links[rel], reason) for rel in sorted(links)
            if (reason := link_refusal(rel, links[rel], links)) is not None]


def cache_links(cache: Path) -> dict[str, str]:
    """Every symbolic link under ``cache``, by cache-relative path, without following any."""
    found: dict[str, str] = {}
    if not cache.is_dir():
        return found
    for directory, dirnames, filenames in os.walk(cache, followlinks=False):
        for name in [*dirnames, *filenames]:
            path = Path(directory) / name
            if path.is_symlink():
                found[path.relative_to(cache).as_posix()] = os.readlink(path)
    return found


def describe(escaping: list[EscapingLink], limit: int = 16) -> str:
    """The escaping links as one bounded line: path, reason and target each."""
    shown = '; '.join(f'{item.rel} {item.reason} (target {item.target!r})' for item in escaping[:limit])
    more = f'; and {len(escaping) - limit} more' if len(escaping) > limit else ''
    return shown + more
