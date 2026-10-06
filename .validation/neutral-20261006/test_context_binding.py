"""Synthetic/read-only checks for the pinned temporary-context mapping."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import prepare_context as context
from public_output import Rejected

HERE = Path(__file__).resolve().parent
BINDING = json.loads((HERE / 'candidate-binding.json').read_bytes())
PIN = hashlib.sha256((HERE / 'candidate-binding.json').read_bytes()).hexdigest()


class ContextChecks(unittest.TestCase):
    def make(self, value=None, parents=None):
        return context.make_context(value or BINDING, PIN, '3.11', '1'*40,
                                    [BINDING['candidate_commit']] if parents is None else parents)

    def test_fixed_coverage_and_dynamic_validation_identity(self):
        result = self.make()
        self.assertEqual(result['validation_commit'], '1'*40)
        self.assertEqual(result['candidate_commit'], BINDING['candidate_commit'])
        self.assertEqual(result['expected_test_counts']['full'], 314)
        self.assertEqual(result['expected_test_counts']['strict-validator'], 20)
        self.assertEqual(result['expected_test_counts']['transaction-regression'], 29)
        self.assertEqual(result['required_labels'][0], 'source-before')
        self.assertEqual(result['required_labels'][-1], 'source-after')
        self.assertEqual(len(result['required_labels']), 14)

    def test_wrong_or_multiple_parent_rejected(self):
        for parents in ([], ['0'*40], [BINDING['candidate_commit'], '0'*40]):
            with self.assertRaises(Rejected): self.make(parents=parents)

    def test_reduced_counts_or_coverage_rejected(self):
        for counts in ({**BINDING['test_counts'], 'full': 313}, {'full': 314}, {**BINDING['test_counts'], 'adapter': True}):
            changed=deepcopy(BINDING); changed['test_counts']=counts
            with self.assertRaises(Rejected): self.make(changed)

    def test_extra_metadata_or_private_identifiers_rejected(self):
        changed=deepcopy(BINDING); changed['internal_provenance']='PRIVATE_SYNTHETIC_MARKER'
        with self.assertRaises(Rejected): self.make(changed)
        changed=deepcopy(BINDING); changed['candidate_commit']='PRIVATE_SYNTHETIC_MARKER'
        with self.assertRaises(Rejected): self.make(changed)

    def test_duplicate_link_allowance_rejected(self):
        changed=deepcopy(BINDING); changed['windows_historical_symlink_files'][1]=changed['windows_historical_symlink_files'][0]
        with self.assertRaises(Rejected): self.make(changed)

    def test_exact_bound_tools_pass(self):
        context.check_tools(BINDING,HERE)

    def test_tool_byte_drift_rejected(self):
        names=list(BINDING['summary_tools_sha256'])+['verify_source.py','run_windows_validation.ps1','source-files.json']
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in names: shutil.copyfile(HERE/name,root/name)
            (root/'public_output.py').write_bytes((root/'public_output.py').read_bytes()+b'\n# synthetic drift\n')
            with self.assertRaises(Rejected): context.check_tools(BINDING,root)

    def test_non_windows_cli_cannot_generate_context(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(context.sys,'platform','linux'):
            output=Path(folder)/'context.json'
            self.assertEqual(context.main(['--checkout',folder,'--binding',str(HERE/'candidate-binding.json'),
                                           '--binding-sha256',PIN,'--python-version','3.11','--output',str(output)]),1)
            self.assertFalse(output.exists())


if __name__ == '__main__': unittest.main()
