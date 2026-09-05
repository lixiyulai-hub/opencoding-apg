import json, tempfile, unittest
from pathlib import Path
from scripts.apg_adaptive_git_ledger_persistence_preview import build_plan, persist, TARGET_PATH, canonical_bytes

SNAPSHOT={"schema_version":"1.0","ledger_id":"apg-adaptive-git-ledger-v1","target_path":TARGET_PATH,"records":[{"event_id":"ledger-abc","event_type":"adaptive-git-preview","payload":{"status":"CHECKPOINT_RECOMMENDED","success_node":"validation-passed","checkpoint_id":"cp-1","revert_point":"plan-ready","resume_condition":"resume.after-checkpoint-preview-is-recorded"}}]}
class PersistenceTests(unittest.TestCase):
 def test_plan_is_deterministic_and_preview_only(self):
  with tempfile.TemporaryDirectory() as d:
   a=build_plan(Path(d),SNAPSHOT); b=build_plan(Path(d),SNAPSHOT)
   self.assertEqual(a,b); self.assertEqual(a['status'],'WRITE_CANDIDATE'); self.assertFalse((Path(d)/TARGET_PATH).exists())
 def test_apply_and_idempotent_replay(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); plan=build_plan(root,SNAPSHOT); out=persist(root,plan,apply=True); self.assertEqual(out['status'],'PERSISTED'); self.assertEqual(json.loads((root/TARGET_PATH).read_text(encoding='utf8')),SNAPSHOT)
   self.assertEqual(persist(root,plan,apply=True)['status'],'ALREADY_PERSISTED')
 def test_drift_freezes_without_write(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); (root/TARGET_PATH).parent.mkdir(parents=True); (root/TARGET_PATH).write_text('drift\n',encoding='utf8')
   out=build_plan(root,SNAPSHOT,preimage_sha256='0'*64); self.assertEqual(out['status'],'FREEZE'); self.assertEqual(out['freeze_reason'],'preimage_hash_mismatch'); self.assertEqual((root/TARGET_PATH).read_text(), 'drift\n')
 def test_invalid_snapshot_and_atomic_failure(self):
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(ValueError): build_plan(Path(d),{"schema_version":"0.1"})
   root=Path(d); plan=build_plan(root,SNAPSHOT); self.assertEqual(persist(root,plan,apply=True,fail_atomic=True)['status'],'FREEZE'); self.assertFalse((root/TARGET_PATH).exists())
if __name__=='__main__': unittest.main()
