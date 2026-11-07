"""Resolve document identities before planning without relaxing transaction guards."""
from hashlib import sha256
from pathlib import Path
import re

from .safety import _existing_casefold_alias, _root_path, safe_target

_BEGIN = "<!-- opencoding:generated "
_END = "<!-- /opencoding:generated -->"
_BLOCK = re.compile(r"<!-- opencoding:generated sha256=([0-9a-f]{64}) -->\n(.*?)<!-- /opencoding:generated -->", re.S)


def resolve_document_paths(root: Path, names: list[str]) -> dict[str, str]:
    project = _root_path(root)
    result = {}
    for name in names:
        alias = _existing_casefold_alias(project, name)
        target_name = alias.name if alias is not None else name
        safe_target(project, target_name)
        result[name] = target_name
    return result


def merge_document(existing: str, generated: str) -> str:
    """Preserve user/APG bytes; replace only an intact OpenCoding-owned block."""
    body = generated + "\n"
    block = _BEGIN + "sha256=" + sha256(body.encode("utf-8")).hexdigest() + " -->\n" + body + _END
    if _BEGIN in existing or _END in existing:
        matches = list(_BLOCK.finditer(existing))
        if len(matches) != 1 or existing.count(_BEGIN) != 1 or existing.count(_END) != 1:
            raise ValueError("ambiguous or malformed OpenCoding document block")
        match = matches[0]
        if sha256(match[2].encode("utf-8")).hexdigest() != match[1]:
            raise ValueError("OpenCoding document block was edited; manual merge required")
        return existing[:match.start()] + block + existing[match.end():]
    if not existing or existing == generated:
        return generated
    return existing + "\n\n" + block + "\n"


def merge_documents(root: Path, documents: dict[str, str], preimages: dict | None = None) -> dict[str, str]:
    merged = {}
    for name, content in documents.items():
        target = safe_target(root, name)
        data = target.read_bytes() if target.exists() else None
        if preimages is not None:
            preimages[name] = sha256(data).hexdigest() if data is not None else None
        existing = data.decode("utf-8") if data is not None else ""
        merged[name] = merge_document(existing, content)
    return merged
