"""Regression coverage for the product-baseline review gaps."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from opencoding.documents import validate_recommendation
from opencoding.service import ServiceError, apply_approved, approve_preview, preview_session

from .test_product_documents import recommendation
from .test_product_service import _approve, _complete


class ProductBaselineReviewTests(unittest.TestCase):
    def test_human_confirmation_is_required_and_bound_to_exact_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = _complete(root)
            preview = preview_session(root, view["session"]["id"])
            with self.assertRaises(ServiceError) as missing:
                approve_preview(preview)
            self.assertEqual(missing.exception.code, "human_confirmation_required")
            approval = _approve(preview)
            with self.assertRaises(ServiceError) as apply_missing:
                apply_approved(root, approval)
            self.assertEqual(apply_missing.exception.code, "human_confirmation_required")
            altered = deepcopy(approval["authorization_context"])
            altered["targets"] = list(reversed(altered["targets"]))
            with self.assertRaises(ServiceError) as mismatch:
                apply_approved(root, approval, authorization_context=altered)
            self.assertEqual(mismatch.exception.code, "human_confirmation_scope_mismatch")

    def test_scenario_source_must_be_user_answer_reference(self):
        invalid = recommendation()
        invalid["project"]["scenarios"][0]["source"] = "answers.outcome"
        with self.assertRaises(ValueError):
            validate_recommendation(invalid)


if __name__ == "__main__":
    unittest.main()
