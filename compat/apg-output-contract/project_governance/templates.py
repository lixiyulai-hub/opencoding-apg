from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

_TEMPLATE_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*}}")
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_TEMPLATE_ROOT = _PACKAGE_ROOT / "templates" / "adaptive-project-governance"
_PACKAGED_TEMPLATE_ROOT = _PACKAGE_ROOT / "templates"
_TEMPLATE_ROOT = (
    _SOURCE_TEMPLATE_ROOT
    if _SOURCE_TEMPLATE_ROOT.is_dir()
    else _PACKAGED_TEMPLATE_ROOT
)


def render_template(template: str, values: Mapping[str, object]) -> str:
    if not isinstance(template, str):
        raise TypeError("template must be a string")
    if not isinstance(values, Mapping):
        raise TypeError("values must be a mapping")
    names = tuple(sorted(set(_TEMPLATE_RE.findall(template))))
    keys = tuple(sorted(values))
    unknown = sorted(set(keys) - set(names))
    missing = sorted(set(names) - set(keys))
    if unknown:
        raise ValueError(f"unknown template values: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"missing template values: {', '.join(missing)}")
    result = _TEMPLATE_RE.sub(lambda match: str(values[match.group(1)]), template)
    return result.replace("\r\n", "\n").replace("\r", "\n")


def load_template(name: str, template_root: Path | None = None) -> str:
    if (
        not isinstance(name, str)
        or not name.endswith(".tmpl")
        or Path(name).name != name
        or "/" in name
        or "\\" in name
    ):
        raise ValueError("template name must end with .tmpl")
    roots = (Path(template_root),) if template_root is not None else (
        _TEMPLATE_ROOT / "default",
        _TEMPLATE_ROOT / "conditional",
    )
    matches: list[Path] = []
    for root in roots:
        resolved_root = root.resolve(strict=False)
        path = (resolved_root / name).resolve(strict=False)
        if not path.is_relative_to(resolved_root):
            raise ValueError("template path escapes template root")
        if path.is_file():
            matches.append(path)
    if not matches:
        raise FileNotFoundError(f"template not found: {name}")
    if len(matches) != 1:
        raise ValueError(f"ambiguous template name: {name}")
    return matches[0].read_text(encoding="utf-8")


__all__ = ["load_template", "render_template"]
