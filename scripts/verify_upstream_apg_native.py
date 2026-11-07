"""Retained offline APG init/native document fixture; not a live Host acceptance."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apg-root", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source))
    from opencoding import service
    from tests.test_product_service import _complete

    evidence = Path(tempfile.mkdtemp(prefix="opencoding-apg-native-" )).resolve()
    project = evidence / "fixture"
    project.mkdir()
    summary = {"mode": "offline-synthetic-fixture", "source_root": str(source),
               "project": str(project), "live_host": False, "independent_review": False,
               "status": "running", "steps": []}
    print(str(evidence), flush=True)

    def save():
        (evidence / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        controller = args.apg_root.resolve() / "scripts/project_governance.py"
        approval = {"id": "offline-test-fixture-owner", "actor": "synthetic-fixture-owner",
                    "role": "owner", "timestamp_utc": datetime.now(timezone.utc).isoformat(), "scope": ["."]}
        approval_path = evidence / "fixture-approval.json"
        approval_path.write_text(json.dumps(approval), encoding="utf-8")
        for phase, extra in (("init-preview", []), ("init-apply", ["--apply", "--approval-file", str(approval_path)])):
            run = subprocess.run([sys.executable, "-B", "-X", "utf8", str(controller), "init", str(project),
                                  "--json", *extra], capture_output=True, timeout=60)
            (evidence / (phase + ".stdout.json")).write_bytes(run.stdout)
            (evidence / (phase + ".stderr.txt")).write_bytes(run.stderr)
            summary["steps"].append({"step": phase, "exit_code": run.returncode})
            if run.returncode != 0:
                raise RuntimeError(phase + " failed; inspect retained output")
        originals = {p.name: p.read_bytes() for p in project.glob("*.md")}
        if "ARCHITECTURE.md" not in originals or "AGENTS.md" not in originals:
            raise RuntimeError("APG did not produce expected fixture documents")
        session = _complete(project)["session"]["id"]
        preview = service.preview_session(project, session)
        if "ARCHITECTURE.md" not in preview["documents"] or "architecture.md" in preview["documents"]:
            raise RuntimeError("native preview did not resolve APG document identity")
        applied = service.apply_approved(project, service.approve_preview(preview))
        if applied["status"] != "applied":
            raise RuntimeError("native apply failed: " + str(applied["status"]))
        for name, original in originals.items():
            if not (project / name).read_bytes().startswith(original):
                raise RuntimeError("APG content changed: " + name)
        if service.preview_session(project, session)["documents"] != preview["documents"]:
            raise RuntimeError("repeat preview changed generated document content")
        transaction_id = applied["transaction"]["transaction_id"]
        rolled_back = service.rollback(project, transaction_id)
        if rolled_back["status"] != "rolled_back":
            raise RuntimeError("native rollback failed")
        for name, original in originals.items():
            if (project / name).read_bytes() != original:
                raise RuntimeError("rollback did not restore APG bytes: " + name)
        summary.update(status="passed", session_id=session, transaction_id=transaction_id,
                       restored_sha256={name: sha256(data).hexdigest() for name, data in originals.items()})
        summary["steps"].append({"step": "native-preview-apply-repeat-rollback", "status": "passed"})
    except Exception as exc:
        summary.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
        save()
        raise
    save()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
