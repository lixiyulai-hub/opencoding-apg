"""AI 适配层契约测试：结构校验、绑定、防重放随机标识与离线模拟。"""

from __future__ import annotations

import json
import unittest
import unittest.mock
import urllib.error
import io

from opencoding.aiadapter import (
    ADAPTER_SCHEMA_VERSION,
    AIAdapter,
    AIRequestError,
    DISABLE_THINKING,
    MockAdapter,
    WorkBuddyGatewayAdapter,
    validate_structured_response,
    probe_connection,
)

INPUT_DIGEST = "a" * 64


class StructuredResponseValidationTests(unittest.TestCase):
    def _binding(self, **overrides):
        result = {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "request_id": "req-1",
            "run_id": "run-1",
            "task_id": "task-1",
            "attempt": 1,
            "request_kind": "evaluate",
            "nonce": "n-1",
            "input_digest": INPUT_DIGEST,
            "allowed_outputs": [],
        }
        result.update(overrides)
        return result

    def test_valid_evaluate_response_passes(self):
        content = json.dumps({
            **self._binding(),
            "summary": "首选本地保存",
            "choice": "local-sqlite",
            "reasons": ["单机使用"],
        })
        payload = validate_structured_response(
            "evaluate", content, nonce="n-1", run_id="run-1",
            task_id="task-1", attempt=1, request_id="req-1",
            input_digest=INPUT_DIGEST, allowed_outputs=[],
        )
        self.assertEqual(payload["choice"], "local-sqlite")

    def test_missing_fields_are_structured_errors(self):
        content = json.dumps({**self._binding(), "summary": "缺字段"})
        with self.assertRaises(AIRequestError) as caught:
            validate_structured_response(
                "implement", content, nonce="n-1", run_id="run-1",
                task_id="task-1", attempt=1, request_id="req-1",
                input_digest=INPUT_DIGEST, allowed_outputs=[],
            )
        self.assertEqual(caught.exception.code, "response_schema_invalid")

    def test_files_nested_types_are_rejected_before_recovery(self):
        for invalid in (None, 23, True, "not-a-list"):
            with self.subTest(files=invalid):
                content = json.dumps({
                    **self._binding(request_kind="implement"),
                    "request_kind": "implement",
                    "summary": "生成代码",
                    "files": invalid,
                })
                with self.assertRaises(AIRequestError) as caught:
                    validate_structured_response(
                        "implement", content, nonce="n-1", run_id="run-1",
                        task_id="task-1", attempt=1, request_id="req-1",
                        input_digest=INPUT_DIGEST, allowed_outputs=[],
                    )
                self.assertEqual(caught.exception.code, "response_schema_invalid")

    def test_files_entries_require_path_and_string_content(self):
        for entry in ({}, {"path": "x.py"}, {"path": "", "content": "x"}, {"path": "x.py", "content": 1}):
            with self.subTest(entry=entry):
                content = json.dumps({
                    **self._binding(request_kind="implement"),
                    "request_kind": "implement",
                    "summary": "生成代码",
                    "files": [entry],
                })
                with self.assertRaises(AIRequestError) as caught:
                    validate_structured_response(
                        "implement", content, nonce="n-1", run_id="run-1",
                        task_id="task-1", attempt=1, request_id="req-1",
                        input_digest=INPUT_DIGEST, allowed_outputs=[],
                    )
                self.assertEqual(caught.exception.code, "response_schema_invalid")

    def test_nonce_mismatch_rejects_stale_or_wrong_task_responses(self):
        content = json.dumps({
            **self._binding(nonce="n-OTHER"),
            "summary": "s",
            "choice": "c",
            "reasons": [],
        })
        with self.assertRaises(AIRequestError) as caught:
            validate_structured_response(
                "evaluate", content, nonce="n-1", run_id="run-1",
                task_id="task-1", attempt=1, request_id="req-1",
                input_digest=INPUT_DIGEST, allowed_outputs=[],
            )
        self.assertEqual(caught.exception.code, "nonce_mismatch")

    def test_non_json_and_plain_claims_are_rejected(self):
        with self.assertRaises(AIRequestError) as caught:
            validate_structured_response("evaluate", "已经做好了", nonce="n-1", run_id="r", task_id="t", attempt=1)
        self.assertEqual(caught.exception.code, "response_not_json")

    def test_model_declined_is_propagated(self):
        content = json.dumps({"error": "超出能力范围"})
        with self.assertRaises(AIRequestError) as caught:
            validate_structured_response("evaluate", content, nonce="n-1", run_id="r", task_id="t", attempt=1)
        self.assertEqual(caught.exception.code, "model_declined")

    def test_sensitive_response_is_blocked(self):
        content = json.dumps({
            **self._binding(),
            "summary": "s",
            "choice": "c",
            "reasons": [],
            "token": "sk-test-1234567890",
        })
        with self.assertRaises(AIRequestError) as caught:
            validate_structured_response(
                "evaluate", content, nonce="n-1", run_id="run-1",
                task_id="task-1", attempt=1, request_id="req-1",
                input_digest=INPUT_DIGEST, allowed_outputs=[],
            )
        self.assertEqual(caught.exception.code, "response_sensitive")

    def test_each_binding_is_required_and_must_match_the_original_request(self):
        binding = self._binding()
        kwargs = {
            "nonce": "n-1",
            "run_id": "run-1",
            "task_id": "task-1",
            "attempt": 1,
            "request_id": "req-1",
            "input_digest": INPUT_DIGEST,
            "allowed_outputs": [],
        }
        for field in binding:
            with self.subTest(field=field):
                response = dict(binding, summary="s", choice="c", reasons=[])
                response.pop(field)
                with self.assertRaises(AIRequestError):
                    validate_structured_response("evaluate", json.dumps(response), **kwargs)
        for field, value in (
            ("run_id", "foreign-run"),
            ("task_id", "foreign-task"),
            ("attempt", True),
            ("request_id", "foreign-request"),
            ("input_digest", "0" * 64),
            ("allowed_outputs", ["other.py"]),
            ("request_kind", "repair"),
            ("schema_version", "1.0"),
        ):
            with self.subTest(field=field):
                response = dict(binding, summary="s", choice="c", reasons=[])
                response[field] = value
                with self.assertRaises(AIRequestError):
                    validate_structured_response("evaluate", json.dumps(response), **kwargs)


class MockAdapterTests(unittest.TestCase):
    def test_mock_adapter_echoes_binding_and_is_marked_not_real(self):
        adapter = MockAdapter()
        result = adapter.complete(
            [{"role": "user", "content": "评估：本地保存"}],
            request_kind="evaluate", run_id="run-1", task_id="task-1", attempt=1, nonce="n-9",
        )
        self.assertFalse(result["real"])
        self.assertEqual(result["structured"]["nonce"], "n-9")
        self.assertEqual(result["task_id"], "task-1")

    def test_mock_adapter_with_responder_validates_schema(self):
        def responder(messages, *, request_kind, nonce):
            if request_kind == "implement":
                return json.dumps({"nonce": nonce, "summary": "生成代码", "files": [{"path": "a.py", "content": "print(1)"}]})
            return json.dumps({"nonce": nonce, "summary": "s", "choice": "c", "reasons": []})

        adapter = MockAdapter(responder=responder)
        result = adapter.complete(
            [{"role": "user", "content": "实现任务"}],
            request_kind="implement", run_id="run-1", task_id="task-2", attempt=1,
        )
        self.assertEqual(result["structured"]["files"][0]["path"], "a.py")


class RealAdapterConfigTests(unittest.TestCase):
    def test_config_requires_sane_values(self):
        with self.assertRaises(AIRequestError):
            AIAdapter(base_url="not-a-url", api_key="k", model="m")
        with self.assertRaises(AIRequestError):
            AIAdapter(base_url="https://example.com/v1", api_key="", model="m")
        with self.assertRaises(AIRequestError):
            AIAdapter(base_url="https://example.com/v1", api_key="k", model="")

    def test_real_adapter_reports_real(self):
        adapter = AIAdapter(base_url="https://example.com/v1", api_key="k", model="m", provider="test")
        self.assertTrue(adapter.real)

    def test_serialized_request_binding_is_persistable_and_response_is_checked(self):
        captured = {}

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(self.payload).encode("utf-8")

        def transport(request, timeout):
            captured["data"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            binding = captured["data"]["opencoding_binding"]
            response = {
                **binding,
                "summary": "本地评估",
                "choice": "local",
                "reasons": ["单机"],
            }
            return Response({"choices": [{"message": {"content": json.dumps(response)}}]})

        adapter = AIAdapter(base_url="https://example.invalid/v1", api_key="test-key", model="test-model")
        with unittest.mock.patch("opencoding.aiadapter.urllib.request.urlopen", side_effect=transport):
            result = adapter.complete(
                [{"role": "user", "content": "合成请求"}],
                request_kind="evaluate",
                run_id="run-wire",
                task_id="task-wire",
                attempt=4,
                request_id="req-wire",
                nonce="nonce-wire",
                input_digest=INPUT_DIGEST,
                allowed_outputs=[],
            )
        binding = captured["data"]["opencoding_binding"]
        self.assertEqual(binding["request_id"], "req-wire")
        self.assertEqual(binding["input_digest"], INPUT_DIGEST)
        self.assertEqual(binding["attempt"], 4)
        self.assertEqual(result["structured"]["request_id"], "req-wire")
        self.assertEqual(result["structured"]["run_id"], "run-wire")

        def conflicting_transport(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            binding = payload["opencoding_binding"]
            response = {
                **binding,
                "task_id": "foreign-task",
                "summary": "conflict",
                "choice": "local",
                "reasons": [],
            }
            return Response({"choices": [{"message": {"content": json.dumps(response)}}]})

        with unittest.mock.patch("opencoding.aiadapter.urllib.request.urlopen", side_effect=conflicting_transport):
            with self.assertRaises(AIRequestError) as caught:
                adapter.complete(
                    [{"role": "user", "content": "合成请求"}],
                    request_kind="evaluate",
                    run_id="run-wire",
                    task_id="task-wire",
                    attempt=4,
                    request_id="req-wire",
                    nonce="nonce-wire",
                    input_digest=INPUT_DIGEST,
                    allowed_outputs=[],
                )
        self.assertEqual(caught.exception.code, "response_binding_mismatch")

    def test_openai_compatible_request_applies_configured_output_token_cap(self):
        captured = {}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit):
                binding = captured["body"]["opencoding_binding"]
                payload = {**binding, "summary": "bounded", "choice": "cli", "reasons": ["test"]}
                return json.dumps({"choices": [{"message": {"content": json.dumps(payload)},
                                                 "finish_reason": "stop"}]}).encode()

        def transport(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return Response()

        adapter = AIAdapter(base_url="https://example.invalid/v1", api_key="test-key", model="test-model")
        with unittest.mock.patch("opencoding.aiadapter.MAX_OUTPUT_TOKENS", 2048), \
             unittest.mock.patch("opencoding.aiadapter.urllib.request.urlopen", side_effect=transport):
            adapter.complete([{"role": "user", "content": "synthetic"}],
                             request_kind="evaluate", run_id="run-cap", task_id="task-cap", attempt=1)
        self.assertEqual(captured["body"]["max_tokens"], 2048)


class ProbeConnectionTests(unittest.TestCase):
    class Response:
        def __init__(self, status, payload=b""):
            self.status = status
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return self.status

        def read(self, _limit=-1):
            return self.payload

    def test_gateway_http_error_does_not_prove_credentials(self):
        def transport(_request, timeout=None):
            raise urllib.error.HTTPError(
                "https://gateway.example", 404, "missing", {}, io.BytesIO(b"not found"))

        with unittest.mock.patch("opencoding.aiadapter.urllib.request.urlopen", side_effect=transport):
            result = probe_connection({
                "provider": "workbuddy_gateway",
                "endpoint": "https://gateway.example",
                "publishable_key": "synthetic-key",
                "model": "synthetic-model",
            })
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["http_status"], 404)

    def test_openai_models_catalog_is_explicit_evidence(self):
        def transport(_request, timeout=None):
            return self.Response(200, b'{"data":[{"id":"model-a"}]}')

        with unittest.mock.patch("opencoding.aiadapter.urllib.request.urlopen", side_effect=transport):
            result = probe_connection({
                "provider": "openai_compatible",
                "base_url": "https://api.example/v1",
                "api_key": "synthetic-key",
                "model": "model-a",
            })
        self.assertEqual(result["state"], "verified")
        self.assertEqual(result["evidence_kind"], "models_json_v1")


class EmptyContentTests(unittest.TestCase):
    """C6 实测:思考型模型耗尽预算后正文为空,必须与非 JSON 明确区分。"""

    def test_empty_content_is_not_reported_as_not_json(self):
        with self.assertRaises(AIRequestError) as caught:
            validate_structured_response("evaluate", "", nonce="n-1", run_id="r",
                                        task_id="t", attempt=1)
        self.assertEqual(caught.exception.code, "response_empty")

    def test_whitespace_only_content_is_empty(self):
        with self.assertRaises(AIRequestError) as caught:
            validate_structured_response("implement", "  \n ", nonce="n-1", run_id="r",
                                        task_id="t", attempt=1)
        self.assertEqual(caught.exception.code, "response_empty")

    def test_prose_still_reported_as_not_json(self):
        with self.assertRaises(AIRequestError) as caught:
            validate_structured_response("evaluate", "已经做好了", nonce="n-1", run_id="r",
                                        task_id="t", attempt=1)
        self.assertEqual(caught.exception.code, "response_not_json")


class GatewayThinkingGuardTests(unittest.TestCase):
    """网关请求体必须显式关闭思考(结构化请求要正文,不要思考过程)。"""

    class _SseResponse:
        def __init__(self, frames):
            self.buf = b"".join(frames)
            self.pos = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read1(self, limit):
            chunk = self.buf[self.pos:self.pos + limit]
            self.pos += len(chunk)
            return chunk

        def close(self):
            return None

    def _frames(self, content):
        delta = {"choices": [{"delta": {"content": content}}]}
        return [b"data: " + json.dumps(delta).encode("utf-8") + b"\n\n",
                b"data: [DONE]\n\n"]

    def test_request_body_disables_thinking(self):
        captured = {}

        def transport(request, timeout=None, **_kw):
            captured["data"] = json.loads(request.data.decode("utf-8"))
            binding = captured["data"]["opencoding_binding"]
            payload = {**binding, "summary": "合成", "choice": "web", "reasons": ["r"]}
            return self._SseResponse(self._frames(json.dumps(payload)))

        adapter = WorkBuddyGatewayAdapter(endpoint="https://example.invalid",
                                         publishable_key="k", model="m")
        with unittest.mock.patch("opencoding.aiadapter.urllib.request.urlopen",
                                side_effect=transport):
            adapter.complete([{"role": "user", "content": "合成"}],
                             request_kind="evaluate", run_id="run-t", task_id="task-t",
                             attempt=1, nonce="nonce-t",
                             input_digest=INPUT_DIGEST, allowed_outputs=[])
        body = captured["data"]
        if DISABLE_THINKING:
            self.assertEqual(body["thinking"], {"type": "disabled"})
        else:
            self.assertNotIn("thinking", body)

    def test_empty_stream_is_reported_as_empty(self):
        def transport(_request, timeout=None, **_kw):
            return self._SseResponse([b"data: [DONE]\n\n"])

        adapter = WorkBuddyGatewayAdapter(endpoint="https://example.invalid",
                                         publishable_key="k", model="m")
        with unittest.mock.patch("opencoding.aiadapter.urllib.request.urlopen",
                                side_effect=transport):
            with self.assertRaises(AIRequestError) as caught:
                adapter.complete([{"role": "user", "content": "合成"}],
                                 request_kind="evaluate", run_id="run-t", task_id="task-t",
                                 attempt=1, nonce="nonce-t",
                                 input_digest=INPUT_DIGEST, allowed_outputs=[])
        self.assertEqual(caught.exception.code, "response_empty")


if __name__ == "__main__":
    unittest.main()
