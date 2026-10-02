#!/usr/bin/env python3
"""Install and exercise the OpenCoding resource in an explicit Codex home.

The command never touches the user's real ``$CODEX_HOME`` unless it is passed
explicitly.  Use a private temporary Codex home for acceptance tests.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opencoding.codex_host import CodexHostError, CodexSkillHost, install_skill


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--load", action="store_true", help="load the installed skill through the project host adapter")
    args = parser.parse_args(argv)
    if not args.project_root.is_absolute() or not args.codex_home.is_absolute():
        parser.error("--project-root and --codex-home must be absolute")
    try:
        report: dict = {"installation": install_skill(args.project_root, args.codex_home, overwrite=args.overwrite)}
        if args.load:
            report["load"] = CodexSkillHost(args.project_root, codex_home=args.codex_home).load(target_platform="unspecified")
    except (CodexHostError, OSError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    report["status"] = "installed_and_loaded" if args.load else "installed"
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
