"""Locate the checked-in or packaged OpenCoding skill resource.

The source checkout keeps the project-standard ``.agents`` resource as the
canonical development copy.  Wheels and sdists carry the same bytes under the
Python package so a clean install can validate the resource without a checkout.
This resolver never treats a partial higher-priority resource as complete.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


_RESOURCE_FILES = ("SKILL.md", "skill.json")


class SkillResourceError(ValueError):
    """A skill resource is missing, partial, linked, or otherwise unusable."""


@dataclass(frozen=True)
class SkillResourceLocation:
    directory: Path
    identifier: str


def _candidate(root: Path, relative: str, identifier: str) -> SkillResourceLocation:
    directory = root / relative
    if not directory.exists():
        return SkillResourceLocation(directory=directory, identifier=identifier)
    if directory.is_symlink() or not directory.is_dir():
        raise SkillResourceError(f"skill resource directory is not regular: {directory}")
    for filename in _RESOURCE_FILES:
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise SkillResourceError(f"incomplete skill resource: {directory}")
    return SkillResourceLocation(directory=directory, identifier=identifier)


def locate_skill_resource(root: str | Path) -> SkillResourceLocation:
    """Return the first complete resource for an absolute source/package root.

    ``root`` is the project root in a checkout and ``site-packages`` in an
    installed package.  An existing but partial standard resource is rejected
    instead of being silently replaced by a lower-priority copy.
    """
    root = Path(root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise SkillResourceError("skill source root must be an existing absolute directory")
    candidates = (
        (".agents/skills/opencoding", ".agents/skills/opencoding"),
        ("skills/opencoding", "skills/opencoding"),
        ("opencoding/resources/skill", "opencoding/resources/skill"),
    )
    for relative, identifier in candidates:
        location = _candidate(root, relative, identifier)
        if location.directory.exists():
            return location
    raise SkillResourceError("no complete opencoding skill resource")


__all__ = ["SkillResourceError", "SkillResourceLocation", "locate_skill_resource"]
