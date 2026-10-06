"""Create a runner-local summary context from a pinned binding and actual push HEAD."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from public_output import (Rejected, exact_keys, hex_value, load_json, require,
                           unavailable_json, validate_context)

BASE = '537c1506a05beb8daabdaca4e667afb9fbbe8196'
REF = 'refs/heads/validation/windows-neutral-20261006'
COUNTS = {'adapter': 19, 'cli_preview': 15, 'evidence_projection': 21,
          'installed_taskplan': 5, 'strict_validator': 20, 'transaction_regression': 29,
          'full': 314, 'packaging': 6, 'rust': 4}
LABEL_KEYS = [('adapter', 'adapter'), ('cli-preview', 'cli_preview'),
              ('evidence-projection', 'evidence_projection'), ('installed-taskplan', 'installed_taskplan'),
              ('strict-validator', 'strict_validator'), ('transaction-regression', 'transaction_regression'),
              ('full', 'full'), ('packaging', 'packaging')]
TOOL_NAMES = ('public_output.py', 'capture_unittest.py', 'summarize_pinned.py', 'prepare_context.py')
BINDING_KEYS = {'schema_version', 'candidate_commit', 'candidate_tree', 'restored_product_base',
                'tracked_file_count', 'source_files_sha256', 'verifier_sha256', 'windows_runner_sha256',
                'summary_tools_sha256', 'windows_historical_symlink_files',
                'windows_required_python_versions', 'test_counts'}


def make_context(binding, binding_sha256, version, head, parents):
    exact_keys(binding, BINDING_KEYS)
    require(type(binding['schema_version']) is int and binding['schema_version'] == 1)
    require(binding['restored_product_base'] == BASE)
    require(hex_value(binding['candidate_commit'], 40) and hex_value(binding['candidate_tree'], 40))
    require(hex_value(head, 40) and parents == [binding['candidate_commit']])
    require(hex_value(binding_sha256, 64))
    require(binding['windows_required_python_versions'] == ['3.11', '3.12'])
    require(type(binding['test_counts']) is dict and binding['test_counts'] == COUNTS)
    require(all(type(n) is int for n in binding['test_counts'].values()))
    exact_keys(binding['summary_tools_sha256'], set(TOOL_NAMES))
    require(all(hex_value(n, 64) for n in binding['summary_tools_sha256'].values()))
    links = binding['windows_historical_symlink_files']
    require(type(links) is list and len(links) == 6 and len(set(links)) == 6)
    require(all(type(n) is str and n.startswith('artifacts/') and '\\' not in n
                and all(p not in ('', '.', '..') for p in n.split('/')) for n in links))
    result = {key: binding[key] for key in ('candidate_commit', 'candidate_tree', 'tracked_file_count',
                                           'source_files_sha256', 'verifier_sha256', 'windows_runner_sha256')}
    result.update(schema_version=1, validation_commit=head, binding_sha256=binding_sha256, python_version=version,
                  required_labels=['source-before', 'prerequisites', 'cargo-version', 'rustc-version'] +
                  [label for label, _ in LABEL_KEYS] + ['rust-domain', 'source-after'],
                  expected_test_counts={label: COUNTS[key] for label, key in LABEL_KEYS})
    return validate_context(result)


def check_tools(binding, folder):
    hashes = {**binding['summary_tools_sha256'], 'verify_source.py': binding['verifier_sha256'],
              'run_windows_validation.ps1': binding['windows_runner_sha256'],
              'source-files.json': binding['source_files_sha256']}
    for name, expected in hashes.items():
        require(hex_value(expected, 64) and hashlib.sha256((folder / name).read_bytes()).hexdigest() == expected)


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise Rejected('arguments_rejected')


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('--checkout', required=True)
        parser.add_argument('--binding', required=True)
        parser.add_argument('--binding-sha256', required=True)
        parser.add_argument('--python-version', required=True, choices=['3.11', '3.12'])
        parser.add_argument('--output', required=True)
        args = parser.parse_args(argv)
        require(sys.platform == 'win32')
        require(os.environ.get('GITHUB_EVENT_NAME') == 'push' and os.environ.get('GITHUB_REF') == REF)
        require(tuple(map(int, args.python_version.split('.'))) == sys.version_info[:2])
        checkout = Path(args.checkout).resolve()
        binding_path = Path(args.binding).resolve()
        require(binding_path.parent == Path(__file__).resolve().parent)
        binding = load_json(binding_path, args.binding_sha256)
        def git(*arguments):
            return subprocess.check_output(['git', '-C', str(checkout), *arguments], stderr=subprocess.PIPE)
        head = git('rev-parse', 'HEAD').decode('ascii').strip()
        require(head == os.environ.get('GITHUB_SHA'))
        header = git('cat-file', 'commit', head).split(b'\n\n', 1)[0]
        parents = [line[7:].decode('ascii') for line in header.splitlines() if line.startswith(b'parent ')]
        context = make_context(binding, args.binding_sha256, args.python_version, head, parents)
        check_tools(binding, binding_path.parent)
        destination = Path(args.output).resolve()
        runner_temp = os.environ.get('RUNNER_TEMP')
        require(type(runner_temp) is str and bool(runner_temp))
        require(destination.is_relative_to(Path(runner_temp).resolve()) and not destination.is_relative_to(checkout))
        require(destination.parent.is_dir())
        raw = (json.dumps(context, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
        print(hashlib.sha256(raw).hexdigest())
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Output is a digest only on success; callers publish fixed failure JSON.
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
