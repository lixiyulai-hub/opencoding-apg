# -*- coding: utf-8 -*-
"""C6-04:AI 响应失败分类拆分的回归测试(全部走受控替身,不发真实请求)。

审计要求把五类失败分开,不再互相冒充:
- response_empty           正文为空(思考预算被占用等)——已在既有测试覆盖;
- response_not_json        完整返回但内容不是合法 JSON;
- response_length_truncated 生成预算耗尽(finish_reason=length)导致截断——本批新增,
                           不得再报成一般 response_not_json;
- transport-unknown        网关流中断/空闲超时/网络不可达——既有既有测试覆盖;
- model_declined           模型明确拒绝(真实业务失败)——既有测试覆盖。

另验证:SSE 成功结果必须在 stream_timing 里如实记录 finish_reason。
"""
import json
import unittest
from unittest import mock

from opencoding.aiadapter import (
    AIRequestError,
    AIAdapter,
    WorkBuddyGatewayAdapter,
)

INPUT_DIGEST = "a" * 64


def _eval_payload(nonce: str) -> str:
    return json.dumps({"summary": "合成", "choice": "web", "reasons": ["r"],
                       "nonce": nonce}, ensure_ascii=False)


class _SseResponse:
    """最小 SSE 替身:按块回放预置帧。"""

    def __init__(self, frames: list[bytes]):
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


def _sse_frames(content: str, finish_reason: str | None) -> list[bytes]:
    frames = [b"data: " + json.dumps(
        {"choices": [{"delta": {"content": content}, "finish_reason": None}]}).encode("utf-8")
        + b"\n\n"]
    frames.append(b"data: " + json.dumps(
        {"choices": [{"delta": {}, "finish_reason": finish_reason}]}).encode("utf-8")
        + b"\n\n")
    frames.append(b"data: [DONE]\n\n")
    return frames


def _gateway():
    return WorkBuddyGatewayAdapter(endpoint="https://example.invalid",
                                   publishable_key="k", model="m")


def _run_gateway(transport):
    adapter = _gateway()
    with mock.patch("opencoding.aiadapter.urllib.request.urlopen",
                    side_effect=transport):
        return adapter.complete(
            [{"role": "user", "content": "合成"}],
            request_kind="evaluate", run_id="run-c6", task_id="task-c6",
            attempt=1, nonce="nonce-c6",
            input_digest=INPUT_DIGEST, allowed_outputs=[])


class GatewayLengthTruncationTests(unittest.TestCase):
    """finish_reason=length 且内容无效 → response_length_truncated。"""

    def test_truncated_json_with_length_finish_is_length_truncated(self):
        # 截断的 JSON(花括号未闭合):完整流结束但生成预算耗尽
        transport = lambda request, timeout=None, **_kw: _SseResponse(
            _sse_frames('{"summary": "被截断的一半', "length"))
        with self.assertRaises(AIRequestError) as caught:
            _run_gateway(transport)
        self.assertEqual(caught.exception.code, "response_length_truncated")
        self.assertIn("finish_reason=length", str(caught.exception))

    def test_complete_but_invalid_with_stop_finish_stays_not_json(self):
        # 完整返回但内容不是 JSON(finish_reason=stop)→ 维持 response_not_json
        transport = lambda request, timeout=None, **_kw: _SseResponse(
            _sse_frames("这不是 JSON 正文", "stop"))
        with self.assertRaises(AIRequestError) as caught:
            _run_gateway(transport)
        self.assertEqual(caught.exception.code, "response_not_json")

    def test_empty_with_length_finish_stays_empty(self):
        # 正文为空(思考耗尽预算)仍报 response_empty——这是实测主因,语义独立
        transport = lambda request, timeout=None, **_kw: _SseResponse(
            _sse_frames("", "length"))
        with self.assertRaises(AIRequestError) as caught:
            _run_gateway(transport)
        self.assertEqual(caught.exception.code, "response_empty")

    def test_success_records_finish_reason(self):
        transport = lambda request, timeout=None, **_kw: _SseResponse(
            _sse_frames(_eval_payload("nonce-c6"), "stop"))
        result = _run_gateway(transport)
        self.assertEqual(result["stream_timing"]["finish_reason"], "stop")


class _NonStreamResponse:
    """非流式替身:urlopen 上下文返回固定 JSON 正文。"""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self._payload

    def close(self):
        return None


class NonStreamLengthTruncationTests(unittest.TestCase):
    """OpenAI 兼容非流式通道:信封完整、正文被截断 → 同样拆分判定。"""

    def _envelope(self, content: str, finish_reason: str) -> bytes:
        return json.dumps({"choices": [{"message": {"content": content},
                                        "finish_reason": finish_reason}]}).encode("utf-8")

    def test_envelope_ok_but_content_truncated(self):
        transport = lambda request, timeout=None, **_kw: _NonStreamResponse(
            self._envelope('{"summary": "被截断', "length"))
        adapter = AIAdapter(base_url="https://example.invalid", api_key="k", model="m")
        with mock.patch("opencoding.aiadapter.urllib.request.urlopen",
                        side_effect=transport):
            with self.assertRaises(AIRequestError) as caught:
                adapter.complete([{"role": "user", "content": "合成"}],
                                 request_kind="evaluate", run_id="run-c6",
                                 task_id="task-c6", attempt=1, nonce="nonce-c6",
                                 input_digest=INPUT_DIGEST, allowed_outputs=[])
        self.assertEqual(caught.exception.code, "response_length_truncated")

    def test_envelope_ok_but_content_invalid_with_stop(self):
        transport = lambda request, timeout=None, **_kw: _NonStreamResponse(
            self._envelope("还是不是 JSON", "stop"))
        adapter = AIAdapter(base_url="https://example.invalid", api_key="k", model="m")
        with mock.patch("opencoding.aiadapter.urllib.request.urlopen",
                        side_effect=transport):
            with self.assertRaises(AIRequestError) as caught:
                adapter.complete([{"role": "user", "content": "合成"}],
                                 request_kind="evaluate", run_id="run-c6",
                                 task_id="task-c6", attempt=1, nonce="nonce-c6",
                                 input_digest=INPUT_DIGEST, allowed_outputs=[])
        self.assertEqual(caught.exception.code, "response_not_json")


class TransportAndBusinessClassesUntouchedTests(unittest.TestCase):
    """传输未知与真实业务失败的既有码不受本批改动影响。"""

    def test_stream_interrupted_is_transport_class(self):
        def transport(request, timeout=None, **_kw):
            return _SseResponse([b"data: " + json.dumps(
                {"choices": [{"delta": {"content": "{"}}]}).encode("utf-8") + b"\n\n"])
            # 无 [DONE]:EOF → gateway_stream_interrupted
        with self.assertRaises(AIRequestError) as caught:
            _run_gateway(transport)
        self.assertEqual(caught.exception.code, "gateway_stream_interrupted")

    def test_model_declined_is_business_class(self):
        def transport(request, timeout=None, **_kw):
            frame = {"choices": [{"delta": {}, "finish_reason": None}],
                     "error": {"code": "policy_violation", "message": "拒绝"}}
            return _SseResponse([b"data: " + json.dumps(frame).encode("utf-8") + b"\n\n"])
        with self.assertRaises(AIRequestError) as caught:
            _run_gateway(transport)
        self.assertEqual(caught.exception.code, "policy_violation")


if __name__ == "__main__":
    unittest.main()
