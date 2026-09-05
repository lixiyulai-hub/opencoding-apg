import tempfile, unittest
from pathlib import Path
from scripts.apg_independent_review import run_review
from scripts.apg_adaptive_git_ledger_persistence_preview import build_plan, persist, TARGET_PATH
class IndependentReviewTests(unittest.TestCase):
 def test_review_passes_current_project(self):
  result=run_review(Path('.')); self.assertEqual(result['status'],'PASS', result.get('blocker_codes'))
 def test_persistence_fixture_records_external_write(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); snap={"schema_version":"1.0","ledger_id":"apg-adaptive-git-ledger-v1","target_path":TARGET_PATH,"records":[]}; plan=build_plan(root,snap); out=persist(root,plan,apply=True); self.assertEqual(out['status'],'PERSISTED'); self.assertTrue((root/TARGET_PATH).is_file()); self.assertTrue(out['external_actions']['filesystem_write'])
if __name__=='__main__': unittest.main()
