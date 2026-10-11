"""Probe reservations and explicit extensions; isolated profile, no network."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from opencoding import aiadapter, aiconfig, request_budget, service
from opencoding.workbench import Workbench
from tests import test_product_aiadapter as probe_fixtures
from tests.test_product_service import _complete


class ProbeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        isolated = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "profile")})
        isolated.start()
        self.addCleanup(isolated.stop)
        self.config = {"provider": "openai_compatible", "base_url": "https://probe.invalid/v1",
                       "api_key": "synthetic-secret-must-not-be-recorded", "model": "model"}

    def success(self, *args, **kwargs):
        return probe_fixtures.ProbeConnectionTests.Response(200, b'{"data":[{"id":"model"}]}')

    def test_budget_view_is_read_only_before_first_probe(self):
        self.assertEqual(aiadapter.connection_budget_state()["remaining"], 20)
        self.assertFalse(aiconfig.config_path().parent.exists())

    def test_failure_counts_and_changing_config_does_not_reset_rate(self):
        with mock.patch("opencoding.aiadapter.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("synthetic offline")) as transport:
            outcomes = [aiadapter.probe_connection(self.config) for _ in range(3)]
            denied = aiadapter.probe_connection({**self.config, "model": "another-model"})
        self.assertTrue(all(r["state"] == "unreachable" for r in outcomes))
        self.assertEqual(denied["code"], "request_budget_rate_limited")
        self.assertEqual(transport.call_count, 3)
        state = request_budget.read(aiadapter.connection_budget_root(), aiadapter.CONNECTION_BUDGET_SCOPE)
        self.assertEqual(state["used"], 3)
        self.assertEqual(len({r["request_id"] for r in state["requests"]}), 3)
        self.assertTrue(all(r["status"] == "unknown" for r in state["requests"]))
        self.assertNotIn(self.config["api_key"], json.dumps(state))

    def test_cumulative_limit_survives_cooldown_and_adapter_recreation(self):
        with mock.patch("opencoding.aiadapter.urllib.request.urlopen", side_effect=self.success) as transport:
            for number in range(20):
                with mock.patch("opencoding.request_budget.time.time", return_value=1000 + 61 * number):
                    self.assertEqual(aiadapter.probe_connection(dict(self.config))["state"], "verified")
            with mock.patch("opencoding.request_budget.time.time", return_value=99999):
                denied = aiadapter.probe_connection(dict(self.config))
        self.assertEqual(denied["code"], "request_budget_exhausted")
        self.assertEqual(transport.call_count, 20)

    def test_extension_requires_literal_confirmation_and_preserves_rate(self):
        root = aiadapter.connection_budget_root(create=True)
        with mock.patch("opencoding.request_budget.time.time", return_value=1000):
            request_budget.reserve(root, aiadapter.CONNECTION_BUDGET_SCOPE,
                                   limit=1, window_limit=1, window_seconds=60)
            bench = Workbench(self.root / "workspace")
            for confirmation in ("true", "false", 1, None, False):
                with self.subTest(confirmation=confirmation), self.assertRaises(request_budget.RequestBudgetError):
                    bench.api("POST", "/api/ai/extend-budget", {},
                              {"amount": 2, "reason": "explicit fixture", "confirm": confirmation})
            state = bench.api("POST", "/api/ai/extend-budget", {},
                              {"amount": 2, "reason": "explicit fixture", "confirm": True})
            self.assertEqual(state["remaining"], 2)
            with mock.patch("opencoding.aiadapter.urllib.request.urlopen", side_effect=self.success) as transport:
                self.assertEqual(aiadapter.probe_connection(self.config)["code"], "request_budget_rate_limited")
                transport.assert_not_called()
        with mock.patch("opencoding.request_budget.time.time", return_value=1061), \
             mock.patch("opencoding.aiadapter.urllib.request.urlopen", side_effect=self.success):
            self.assertEqual(aiadapter.probe_connection(self.config)["state"], "verified")

    def test_corrupted_budget_blocks_network_and_preserves_bytes(self):
        with mock.patch("opencoding.aiadapter.urllib.request.urlopen", side_effect=self.success):
            aiadapter.probe_connection(self.config)
        path = next((aiadapter.connection_budget_root() / ".opencoding/request-budgets").glob("*.json"))
        path.write_bytes(b"broken-budget")
        with mock.patch("opencoding.aiadapter.urllib.request.urlopen") as transport:
            result = aiadapter.probe_connection(self.config)
        self.assertEqual(result["code"], "request_budget_invalid")
        transport.assert_not_called()
        self.assertEqual(path.read_bytes(), b"broken-budget")

    def test_http_failure_is_recorded_without_modifying_provider_configuration(self):
        aiconfig.save_config(self.config)
        before = aiconfig.config_path().read_bytes()
        error = urllib.error.HTTPError("https://probe.invalid", 401, "blocked", {}, io.BytesIO())
        with mock.patch("opencoding.aiadapter.urllib.request.urlopen", side_effect=error):
            result = aiadapter.probe_connection(self.config)
        self.assertEqual(result["http_status"], 401)
        self.assertEqual(aiconfig.config_path().read_bytes(), before)
        state = request_budget.read(aiadapter.connection_budget_root(), aiadapter.CONNECTION_BUDGET_SCOPE)
        self.assertEqual(state["requests"][0]["status"], "failed")

    def test_evaluation_extension_is_session_scoped_and_strict(self):
        workspace = self.root / "workspace"
        project = workspace / "project"
        project.mkdir(parents=True)
        sid = _complete(project)["session"]["id"]
        bench = Workbench(workspace)
        request_budget.reserve(project, "evaluation:" + sid, limit=1)
        endpoint = "/api/project/project/session/" + sid
        for amount in (True, 1.5, "2"):
            with self.subTest(amount=amount), self.assertRaises(request_budget.RequestBudgetError):
                bench.api("POST", endpoint + "/extend-evaluation-budget", {},
                          {"amount": amount, "reason": "fixture", "confirm": True})
        result = bench.api("POST", endpoint + "/extend-evaluation-budget", {},
                           {"amount": 2, "reason": "fixture", "confirm": True})
        self.assertEqual(result["remaining"], 2)
        other = service.create_session(project, "another")['session']['id']
        self.assertFalse(bench.api("GET", "/api/project/project/session/" + other + "/evaluation-budget",
                                  {}, {})["initialized"])
