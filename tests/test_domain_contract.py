import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class DomainContractTests(unittest.TestCase):
    def test_rust_domain_contract_exists(self):
        source = (ROOT / "services/domain/src/lib.rs").read_text(encoding="utf-8")
        for token in ("TaskState", "submit_proof", "review", "answer_why", "repair", "append_ledger"):
            self.assertIn(token, source)

    def test_cargo_tests_pass(self):
        result = subprocess.run(["cargo", "test", "--manifest-path", str(ROOT / "services/domain/Cargo.toml")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

if __name__ == "__main__":
    unittest.main()
