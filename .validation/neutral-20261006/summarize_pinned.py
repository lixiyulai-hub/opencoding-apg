"""Print only a validated public projection of explicitly named private result files."""
from __future__ import annotations
import argparse
from public_output import Rejected, build_pinned, load_json, public_json, unavailable_json, validate_context


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise Rejected("arguments_rejected")


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--context", required=True)
        parser.add_argument("--context-sha256", required=True)
        parser.add_argument("--result", required=True)
        parser.add_argument("--source-before")
        parser.add_argument("--source-after")
        args = parser.parse_args(argv)
        context = validate_context(load_json(args.context, args.context_sha256))
        private_result = load_json(args.result)
        # Source reports are the complete raw verifier logs, tied to recorded hashes.
        def source(path, label):
            if not path:
                return None
            rows = private_result.get("records", [])
            matches = [row for row in rows if type(row) is dict and row.get("label") == label]
            if len(matches) != 1 or matches[0].get("log_sha256") is None:
                raise Rejected("summary_input_rejected")
            return load_json(path, matches[0]["log_sha256"])
        result = build_pinned(context, private_result, source(args.source_before, "source-before"), source(args.source_after, "source-after"))
        print(public_json(result, context))
        return 0 if result["status"] == "passed" else 1
    except KeyboardInterrupt:
        print(unavailable_json())
        return 130
    except (Exception,):
        print(unavailable_json())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
