"""Project identity and serialized adoption regressions."""
import tempfile
import unittest
from pathlib import Path

from opencoding import service
from opencoding.sessions import session_write_lock
from tests.test_product_service import _complete


class AdoptionIdentityTests(unittest.TestCase):
    def test_adoption_pins_root_and_extras_cannot_replace_verified_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            sid = _complete(root)["session"]["id"]
            adopted = service.adopt_evaluation_plan(root, sid)["adoption"]
            self.assertEqual(adopted["root"], str(root))
            path = root / ".opencoding/adoptions" / (sid + ".json")
            before = path.read_bytes()
            for fields in ({"root": "elsewhere"}, {"session_id": "another"},
                           {"validated": True}, {"plan_digest": "invented"}):
                with self.subTest(fields=fields), self.assertRaises(service.ServiceError) as caught:
                    service.adopt_evaluation_plan(root, sid, extras=fields)
                self.assertEqual(caught.exception.code, "adoption_reserved_fields")
                self.assertEqual(path.read_bytes(), before)

    def test_adoption_cannot_write_while_session_is_being_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            sid = _complete(root)["session"]["id"]
            with session_write_lock(root), self.assertRaises(service.ServiceError) as caught:
                service.adopt_evaluation_plan(root, sid)
            self.assertEqual(caught.exception.code, "session_lock_busy")
            self.assertFalse((root / ".opencoding/adoptions" / (sid + ".json")).exists())
