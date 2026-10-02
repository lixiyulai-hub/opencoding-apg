# Project skill resource

This directory is the project-level OpenCoding skill entry. It is intentionally
kept beside the source copy at `skills/opencoding/` so a host Agent that scans
`.agents/skills` can discover it without installing a wheel.

The current Codex execution environment does not expose a local project-skill
loader; run `python scripts/verify_codex_skill.py --root /absolute/project --exercise`
to validate the resource and its entrypoint. A successful local check is not a
claim that the host automatically loaded the skill.
