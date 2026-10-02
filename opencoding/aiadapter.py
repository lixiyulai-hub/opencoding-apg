"""已授权真实 AI 调用的最小适配层（OpenAI 兼容协议，纯标准库）。

适配器只做三类请求：评估方案、实现任务、修复失败。每次请求绑定运行、
任务、尝试编号与请求随机标识；响应必须带相同随机标识并通过结构校验，
否则返回结构化错误，绝不把一段中文“已经做好了”当成成功。

默认适配器走真实 HTTP；MockAdapter 仅供离线开发与测试，明确标记为模拟，
不能冒充真实接通。API Key 只进入请求头，不写入日志、收据或报告。
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .safety import canonical_json, inspect_sensitive, sanitize_text, sha256_bytes


ADAPTER_SCHEMA_VERSION = "1.1"
REQUEST_KINDS = ("evaluate", "implement", "repair")
_REQUIRED_FIELDS = {
    "evaluate": {
        "schema_version", "request_id", "run_id", "task_id", "attempt",
        "request_kind", "nonce", "input_digest", "allowed_outputs",
        "summary", "choice", "reasons",
    },
    "implement": {
        "schema_version", "request_id", "run_id", "task_id", "attempt",
        "request_kind", "nonce", "input_digest", "allowed_outputs",
        "summary", "files",
    },
    "repair": {
        "schema_version", "request_id", "run_id", "task_id", "attempt",
        "request_kind", "nonce", "input_digest", "allowed_outputs",
        "summary", "fix", "files",
    },
}
MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 60.0
# 思考型模型(如 glm-5.3)默认把预算消耗在 reasoning_content;结构化请求要的是正文,
# 故默认显式关闭思考。可用环境变量 OPENCODING_AI_THINKING=1 恢复思考行为。
DISABLE_THINKING = os.environ.get("OPENCODING_AI_THINKING", "0") != "1"
# 显式给出生成上限(0=不发送该字段,沿用网关默认)。
# C6 实测:不显式给上限时,多文件契约的 JSON 正文会被网关默认上限截断。
# W3 实测(2026-09-30):16384 在 glm-5.3 上仍会被思考过程占满导致正文为空
# (response_empty,run2/run3 复现);32768 实测可正常产出结构化正文(run4)。
MAX_OUTPUT_TOKENS = int(os.environ.get("OPENCODING_AI_MAX_TOKENS", "32768") or 0)


class AIRequestError(ValueError):
    """A fail-closed AI adapter error with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_binding(run_id: str, task_id: str, attempt: int, request_kind: str, nonce: str) -> None:
    for name, value in (("run_id", run_id), ("task_id", task_id), ("nonce", nonce)):
        if not isinstance(value, str) or not value.strip():
            raise AIRequestError("binding_invalid", name + " 必须是非空字符串")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise AIRequestError("binding_invalid", "attempt 必须是正整数")
    if request_kind not in REQUEST_KINDS:
        raise AIRequestError("binding_invalid", "未知请求类别")


def _extract_content(payload: Mapping[str, Any]) -> str:
    try:
        choices = payload["choices"]
        message = choices[0]["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIRequestError("response_shape_invalid", "模型响应缺少 choices/message/content") from exc
    if not isinstance(content, str):
        raise AIRequestError("response_shape_invalid", "模型响应内容不是文本")
    if len(content.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise AIRequestError("response_oversized", "模型响应超过大小上限")
    return content


def validate_structured_response(
    request_kind: str,
    content: str,
    *,
    nonce: str,
    run_id: str,
    task_id: str,
    attempt: int,
    request_id: str | None = None,
    input_digest: str | None = None,
    allowed_outputs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """解析并校验结构化响应；任何不完整都返回结构化错误。"""

    _validate_binding(run_id, task_id, attempt, request_kind, nonce)
    if not str(content).strip():
        # C6 实测:思考型模型把预算全部用于 reasoning_content 时正文为空。
        # 这里如实区分,不伪装成"不是合法 JSON",便于上层给出可执行的处置建议。
        raise AIRequestError("response_empty",
                             "模型未产出正文(生成预算可能被思考过程占用)")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AIRequestError("response_not_json", "模型响应不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise AIRequestError("response_not_json", "模型响应不是 JSON 对象")
    if payload.get("error"):
        raise AIRequestError("model_declined", sanitize_text(str(payload["error"]))[:200])
    required = _REQUIRED_FIELDS[request_kind]
    missing = sorted(field for field in required if field not in payload)
    if missing:
        raise AIRequestError("response_schema_invalid", "模型响应缺少字段：" + ",".join(missing))
    if (
        payload.get("schema_version") != ADAPTER_SCHEMA_VERSION
        or not isinstance(payload.get("request_id"), str)
        or not payload["request_id"].strip()
        or not isinstance(payload.get("run_id"), str)
        or not isinstance(payload.get("task_id"), str)
        or type(payload.get("attempt")) is not int
        or payload.get("request_kind") != request_kind
        or not isinstance(payload.get("input_digest"), str)
        or len(payload["input_digest"]) != 64
        or any(char not in "0123456789abcdef" for char in payload["input_digest"])
        or not isinstance(payload.get("allowed_outputs"), list)
        or any(not isinstance(path, str) or not path for path in payload["allowed_outputs"])
    ):
        raise AIRequestError("response_binding_invalid", "响应绑定字段类型或格式无效")
    _validate_payload_body(request_kind, payload)
    if payload["nonce"] != nonce:
        raise AIRequestError("nonce_mismatch", "响应随机标识与请求不匹配，拒绝旧响应或错任务响应")
    expected_binding = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "attempt": attempt,
        "request_kind": request_kind,
        "nonce": nonce,
        "request_id": request_id,
        "input_digest": input_digest,
        "allowed_outputs": list(allowed_outputs) if allowed_outputs is not None else None,
    }
    for name, expected in expected_binding.items():
        if expected is not None and payload[name] != expected:
            raise AIRequestError("response_binding_mismatch", "响应绑定字段不匹配：" + name)
    sensitive = inspect_sensitive(content)
    if sensitive["sensitive"]:
        raise AIRequestError("response_sensitive", "模型响应包含疑似凭据内容，已阻止")
    return dict(payload)


def _validate_payload_body(request_kind: str, payload: Mapping[str, Any]) -> None:
    """Validate the versioned response body before recovery or apply."""

    if not isinstance(payload.get("summary"), str) or not payload["summary"].strip():
        raise AIRequestError("response_schema_invalid", "summary 必须是非空字符串")
    if request_kind == "evaluate":
        if not isinstance(payload.get("choice"), str) or not payload["choice"].strip():
            raise AIRequestError("response_schema_invalid", "choice 必须是非空字符串")
        if not isinstance(payload.get("reasons"), list) or any(
            not isinstance(item, str) or not item.strip() for item in payload["reasons"]
        ):
            raise AIRequestError("response_schema_invalid", "reasons 必须是非空字符串列表")
        return
    if request_kind == "repair" and (
        not isinstance(payload.get("fix"), str) or not payload["fix"].strip()
    ):
        raise AIRequestError("response_schema_invalid", "fix 必须是非空字符串")
    files = payload.get("files")
    if type(files) is not list:
        raise AIRequestError("response_schema_invalid", "files 必须是列表")
    for index, entry in enumerate(files):
        if not isinstance(entry, Mapping):
            raise AIRequestError("response_schema_invalid", f"files[{index}] 必须是对象")
        if set(entry) != {"path", "content"}:
            raise AIRequestError("response_schema_invalid", f"files[{index}] 字段不完整")
        if not isinstance(entry["path"], str) or not entry["path"].strip():
            raise AIRequestError("response_schema_invalid", f"files[{index}].path 必须是非空字符串")
        if not isinstance(entry["content"], str):
            raise AIRequestError("response_schema_invalid", f"files[{index}].content 必须是字符串")


def validate_structured_payload(
    request_kind: str,
    payload: Mapping[str, Any],
    *,
    nonce: str,
    run_id: str,
    task_id: str,
    attempt: int,
    request_id: str | None = None,
    input_digest: str | None = None,
    allowed_outputs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate an already-persisted structured payload through the same schema path."""

    if not isinstance(payload, Mapping):
        raise AIRequestError("response_not_json", "持久化响应不是 JSON 对象")
    return validate_structured_response(
        request_kind,
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
        nonce=nonce,
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        request_id=request_id,
        input_digest=input_digest,
        allowed_outputs=allowed_outputs,
    )


class AIAdapter:
    """OpenAI 兼容的真实调用适配器；需要显式传入已授权服务配置。"""

    def __init__(self, *, base_url: str, api_key: str, model: str, provider: str = "unknown", timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        if not isinstance(base_url, str) or not base_url.startswith(("https://", "http://")):
            raise AIRequestError("config_invalid", "base_url 必须是 http(s) URL")
        if not isinstance(api_key, str) or not api_key.strip():
            raise AIRequestError("config_invalid", "api_key 不能为空")
        if not isinstance(model, str) or not model.strip():
            raise AIRequestError("config_invalid", "model 不能为空")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = sanitize_text(provider)
        self.timeout_seconds = float(timeout_seconds)

    @property
    def real(self) -> bool:
        return True

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        request_kind: str,
        run_id: str,
        task_id: str,
        attempt: int,
        nonce: str | None = None,
        request_id: str | None = None,
        input_digest: str | None = None,
        allowed_outputs: Sequence[str] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """发送一次真实请求并返回绑定后的结构化结果。"""

        nonce = nonce or ("nonce-" + uuid.uuid4().hex)
        request_id = request_id or ("req-" + uuid.uuid4().hex)
        allowed_outputs = list(allowed_outputs or [])
        input_digest = input_digest or sha256_bytes(canonical_json({
            "messages": list(messages),
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "request_kind": request_kind,
            "allowed_outputs": allowed_outputs,
        }))
        _validate_binding(run_id, task_id, attempt, request_kind, nonce)
        binding = {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "request_kind": request_kind,
            "nonce": nonce,
            "input_digest": input_digest,
            "allowed_outputs": allowed_outputs,
        }
        wire_messages = [
            {
                "role": "system",
                "content": "OpenCoding 请求绑定（必须原样回传 nonce）："
                + json.dumps(binding, ensure_ascii=False, sort_keys=True),
            },
            *[{"role": item["role"], "content": item["content"]} for item in messages],
        ]
        body = {
            "model": self.model,
            "messages": wire_messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "opencoding_binding": binding,
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = datetime.now(timezone.utc)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES * 4)
        except urllib.error.HTTPError as exc:
            raise AIRequestError("http_%d" % exc.code, "模型服务返回 HTTP %d" % exc.code) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise AIRequestError("network_unavailable", sanitize_text(str(exc))[:200]) from exc
        finished = datetime.now(timezone.utc)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AIRequestError("response_not_json", "模型服务返回非 JSON") from exc
        content = _extract_content(payload)
        # C6-04:非流式响应同样取 finish_reason,把"预算耗尽截断"从
        # 一般 JSON 无效里拆出来(信封完整但正文字符串被截断的场景)。
        finish_reason: str | None = None
        try:
            fr = payload["choices"][0].get("finish_reason")
            finish_reason = str(fr) if fr else None
        except (AttributeError, IndexError, KeyError, TypeError):
            pass
        try:
            structured = validate_structured_response(
                request_kind, content, nonce=nonce, run_id=run_id, task_id=task_id,
                attempt=attempt, request_id=request_id, input_digest=input_digest,
                allowed_outputs=allowed_outputs,
            )
        except AIRequestError as exc:
            if exc.code == "response_not_json" and finish_reason == "length":
                raise AIRequestError(
                    "response_length_truncated",
                    "生成预算耗尽(finish_reason=length),响应可能被截断;原判定:" + str(exc),
                ) from exc
            raise
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "real": True,
            "provider": self.provider,
            "model": self.model,
            "request_kind": request_kind,
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "nonce": nonce,
            "started_at": started.isoformat().replace("+00:00", "Z"),
            "finished_at": finished.isoformat().replace("+00:00", "Z"),
            "latency_ms": int((finished - started).total_seconds() * 1000),
            "response_sha256": sha256_bytes(canonical_json(structured)),
            "input_sha256": input_digest,
            "structured": structured,
        }


class MockAdapter:
    """离线开发/测试专用模拟适配器；返回参数化响应，不能冒充真实接通。"""

    def __init__(self, *, responder=None, model: str = "mock-model"):
        self._responder = responder
        self.model = model

    @property
    def real(self) -> bool:
        return False

    def complete(
        self, messages, *, request_kind, run_id, task_id, attempt, nonce=None,
        request_id=None, input_digest=None, allowed_outputs=None, temperature=0.2,
    ):
        nonce = nonce or ("nonce-" + uuid.uuid4().hex)
        request_id = request_id or ("req-" + uuid.uuid4().hex)
        allowed_outputs = list(allowed_outputs or [])
        input_digest = input_digest or sha256_bytes(canonical_json({
            "messages": list(messages), "run_id": run_id, "task_id": task_id,
            "attempt": attempt, "request_kind": request_kind,
            "allowed_outputs": allowed_outputs,
        }))
        _validate_binding(run_id, task_id, attempt, request_kind, nonce)
        if self._responder is not None:
            content = self._responder(messages, request_kind=request_kind, nonce=nonce)
        elif request_kind == "evaluate":
            content = json.dumps({"nonce": nonce, "summary": "模拟响应（非真实模型）", "choice": "mock-choice", "reasons": ["模拟依据"]}, ensure_ascii=False)
        elif request_kind == "implement":
            content = json.dumps({"nonce": nonce, "summary": "模拟响应（非真实模型）", "files": []}, ensure_ascii=False)
        else:
            content = json.dumps(
                {
                    "nonce": nonce,
                    "summary": "模拟响应（非真实模型）",
                    "fix": "模拟修复说明",
                    "files": [],
                },
                ensure_ascii=False,
            )
        try:
            simulated_payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            simulated_payload = None
        if isinstance(simulated_payload, dict):
            # The offline adapter represents a cooperative test responder. Real
            # transport responses are never filled in and must echo these fields.
            simulated_payload.setdefault("schema_version", ADAPTER_SCHEMA_VERSION)
            simulated_payload.setdefault("request_id", request_id)
            simulated_payload.setdefault("run_id", run_id)
            simulated_payload.setdefault("task_id", task_id)
            simulated_payload.setdefault("attempt", attempt)
            simulated_payload.setdefault("request_kind", request_kind)
            simulated_payload.setdefault("nonce", nonce)
            simulated_payload.setdefault("input_digest", input_digest)
            simulated_payload.setdefault("allowed_outputs", allowed_outputs)
            content = json.dumps(simulated_payload, ensure_ascii=False)
        structured = validate_structured_response(
            request_kind, content, nonce=nonce, run_id=run_id, task_id=task_id,
            attempt=attempt, request_id=request_id, input_digest=input_digest,
            allowed_outputs=allowed_outputs,
        )
        now = _now()
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "real": False,
            "provider": "mock",
            "model": self.model,
            "request_kind": request_kind,
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "nonce": nonce,
            "started_at": now,
            "finished_at": now,
            "latency_ms": 0,
            "response_sha256": sha256_bytes(canonical_json(structured)),
            "input_sha256": input_digest,
            "structured": structured,
        }


class WorkBuddyGatewayAdapter:
    """WorkBuddy 云网关适配器(应用级授权,流式接口,免个人密钥)。

    与 AIAdapter 暴露完全相同的 complete() 契约;差异仅在传输层:
    - 端点是应用的 /.cloud/llm/chat/completions(只支持流式 SSE);
    - 鉴权头是 x-wb-webapp-access-key(应用级 publishableKey),外加 Origin;
    - 响应按 SSE 帧累积成完整文本后走同一结构化校验(绑定/nonce/敏感检查不变)。
    publishable_key 只进请求头,不写日志/回执/报告。
    """

    def __init__(
        self,
        *,
        endpoint: str,
        publishable_key: str,
        model: str,
        timeout_seconds: float = 180.0,
        connect_timeout: float = 15.0,
        idle_timeout: float = 90.0,
        deadline_seconds: float = 420.0,
    ):
        if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
            raise AIRequestError("config_invalid", "endpoint 必须是 http(s) URL")
        if not isinstance(publishable_key, str) or not publishable_key.strip():
            raise AIRequestError("config_invalid", "publishable_key 不能为空")
        if not isinstance(model, str) or not model.strip():
            raise AIRequestError("config_invalid", "model 不能为空")
        self.endpoint = endpoint.rstrip("/")
        self.publishable_key = publishable_key
        self.model = model
        self.provider = "workbuddy_gateway"
        self.timeout_seconds = float(timeout_seconds)
        self.connect_timeout = float(connect_timeout)
        self.idle_timeout = float(idle_timeout)
        self.deadline_seconds = float(deadline_seconds)

    @property
    def real(self) -> bool:
        return True

    def _consume_sse(self, response: Any) -> tuple[str, dict[str, Any]]:
        """消费 SSE(字节级读取):空闲由 socket 超时兜底,整体截止按每次读块检查。

        不按行迭代——持续字节流无换行时行读取会阻塞过久(CP1 审核对照);这里用
        read1/read 小块读取 + 自维护缓冲解析完整 SSE 帧,任何读块后都检查截止。
        """
        import time

        content_parts: list[str] = []
        reasoning_chars = 0
        saw_done = False
        started = time.monotonic()
        first_chunk_at = None
        buf = b""
        # C6-04:记录 finish_reason,供上层区分"正常结束但内容无效"与
        # "生成预算耗尽被截断(finish_reason=length)"——两者处置建议不同。
        finish_reason: str | None = None
        meta: dict[str, Any] = {}

        while True:
            now = time.monotonic()
            remaining = self.deadline_seconds - (now - started)
            if remaining <= 0:
                try:
                    response.close()
                except OSError:
                    pass
                raise AIRequestError("deadline_exceeded",
                                     "整体截止时间已到(%.0fs),流未完成" % self.deadline_seconds)
            try:
                # R06:阻塞读取必须在剩余总时间内结束——把 socket 超时收敛到
                # min(空闲上限, 剩余总时间),静默/缓慢流都不可能越过截止。
                bounded_by_deadline = remaining < self.idle_timeout
                try:
                    sock = response.fp.raw._sock
                    sock.settimeout(max(0.05, min(self.idle_timeout, remaining)))
                except (AttributeError, OSError):
                    pass
                if hasattr(response, "read1"):
                    chunk = response.read1(65536)
                else:
                    chunk = response.read(4096)
            except (socket.timeout, TimeoutError):
                if bounded_by_deadline:
                    # 剩余总时间已小于空闲上限:这次超时由总截止引起,标签如实区分
                    raise AIRequestError("deadline_exceeded",
                                         "整体截止时间已到(%.0fs),流未完成" % self.deadline_seconds) from None
                raise AIRequestError("read_idle_timeout",
                                     "读取空闲超时(%.0fs 无数据)" % self.idle_timeout) from None
            if not chunk:
                break  # EOF
            now2 = time.monotonic()
            if first_chunk_at is None:
                first_chunk_at = now2 - started
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue  # 注释/保活/事件名
                data = line[5:].strip()
                if data == b"[DONE]":
                    saw_done = True
                    break
                try:
                    payload = json.loads(data.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                    code = str(payload["error"].get("code") or "model_error")
                    raise AIRequestError(sanitize_text(code)[:40],
                                         sanitize_text(str(payload["error"].get("message")))[:200])
                choices = payload.get("choices") or [] if isinstance(payload, dict) else []
                if choices and isinstance(choices[0], dict) and choices[0].get("finish_reason"):
                    finish_reason = str(choices[0]["finish_reason"])
                delta = (choices[0].get("delta") or {}) if choices else {}
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    content_parts.append(piece)
                # W3:思考型模型会把预算花在 reasoning_content 上;单独记录,
                # 让"空正文"这类失败能被准确归因,而不是笼统报"无内容"。
                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    reasoning_chars += len(reasoning.encode("utf-8"))
                if len("".join(content_parts).encode("utf-8")) > MAX_RESPONSE_BYTES:
                    raise AIRequestError("response_oversized", "模型响应超过大小上限")
            if saw_done:
                break
        if not saw_done:
            raise AIRequestError("gateway_stream_interrupted", "模型流式响应未正常结束")
        # CP2-03:静默后迟到的完整响应同样受截止约束
        if time.monotonic() - started > self.deadline_seconds:
            raise AIRequestError("deadline_exceeded",
                                 "整体截止时间已到(%.0fs),迟到完整响应不接受" % self.deadline_seconds)
        meta = {
            "content_chars": len("".join(content_parts).encode("utf-8")),
            "reasoning_chars": reasoning_chars,
            "first_chunk_seconds": round(first_chunk_at, 2) if first_chunk_at is not None else None,
            "total_seconds": round(time.monotonic() - started, 2),
            "idle_timeout": self.idle_timeout,
            "deadline_seconds": self.deadline_seconds,
            "finish_reason": finish_reason,
        }
        return "".join(content_parts), meta

    @staticmethod
    def _extract_json_text(content: str) -> tuple[str, bool]:
        """容错提取 JSON 文本:剥 Markdown 围栏/前后杂文;找不到则原样返回。"""
        text = content.strip()
        stripped = False
        if text.startswith("```"):
            text = text[3:]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
            if text.endswith("```"):
                text = text[:-3].strip()
            stripped = True
        if text.startswith("{"):
            return text, stripped
        start = text.find("{")
        if start < 0:
            return content, False
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1], True
        return content, False

    @staticmethod
    def _missing_binding_fields(content: str, binding: Mapping[str, Any]) -> list[str]:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        return [f for f in binding if f != "nonce" and f not in payload]

    @staticmethod
    def _fill_binding(content: str, binding: Mapping[str, Any]) -> str:
        """补齐静态绑定字段(nonce 除外——它必须由模型回传以证明活性)。"""
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return content
        if not isinstance(payload, dict):
            return content
        changed = False
        for field, value in binding.items():
            if field == "nonce":
                continue
            if field not in payload:
                payload[field] = value
                changed = True
        return json.dumps(payload, ensure_ascii=False) if changed else content

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        request_kind: str,
        run_id: str,
        task_id: str,
        attempt: int,
        nonce: str | None = None,
        request_id: str | None = None,
        input_digest: str | None = None,
        allowed_outputs: Sequence[str] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        nonce = nonce or ("nonce-" + uuid.uuid4().hex)
        request_id = request_id or ("req-" + uuid.uuid4().hex)
        allowed_outputs = list(allowed_outputs or [])
        input_digest = input_digest or sha256_bytes(canonical_json({
            "messages": list(messages),
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "request_kind": request_kind,
            "allowed_outputs": allowed_outputs,
        }))
        _validate_binding(run_id, task_id, attempt, request_kind, nonce)
        binding = {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "request_kind": request_kind,
            "nonce": nonce,
            "input_digest": input_digest,
            "allowed_outputs": allowed_outputs,
        }
        wire_messages = [
            {
                "role": "system",
                "content": "OpenCoding 请求绑定（必须原样回传 nonce）："
                + json.dumps(binding, ensure_ascii=False, sort_keys=True),
            },
            *[{"role": item["role"], "content": item["content"]} for item in messages],
        ]
        body = {
            "model": self.model,
            "messages": wire_messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "stream": True,
            "opencoding_binding": binding,
        }
        # C6 实测:glm-5.3 是思考型模型,默认会把生成预算全部消耗在
        # reasoning_content 上,delta.content 全程为空(finish_reason=length、
        # reasoning_tokens≈completion_tokens),结构化请求必然拿不到正文。
        # 结构化响应要的是正文,不是思考过程,故显式关闭思考。
        if DISABLE_THINKING:
            body["thinking"] = {"type": "disabled"}
            # W3 实测(2026-09-30):同一网关对 `thinking` 字段的具体键名/位置存在
            # 实现差异,补一份 OpenAI 生态里常见的等价开关,二者并存不冲突。
            body["enable_thinking"] = False
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if MAX_OUTPUT_TOKENS:
            body["max_tokens"] = int(MAX_OUTPUT_TOKENS)
        request = urllib.request.Request(
            self.endpoint + "/.cloud/llm/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "x-wb-webapp-access-key": self.publishable_key,
                "Origin": self.endpoint,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = datetime.now(timezone.utc)
        try:
            with urllib.request.urlopen(request, timeout=self.connect_timeout) as response:
                try:
                    sock = response.fp.raw._sock  # CPython HTTPResponse 底层 socket
                    sock.settimeout(self.idle_timeout)
                except (AttributeError, OSError):
                    pass
                content, stream_meta = self._consume_sse(response)
        except urllib.error.HTTPError as exc:
            raise AIRequestError("http_%d" % exc.code, "模型网关返回 HTTP %d" % exc.code) from exc
        except AIRequestError:
            raise
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            code = "read_idle_timeout" if isinstance(exc, (socket.timeout, TimeoutError)) else "network_unavailable"
            raise AIRequestError(code, sanitize_text(str(exc))[:200]) from exc
        finished = datetime.now(timezone.utc)
        raw_content_sha256 = sha256_bytes(content.encode("utf-8"))
        content, extraction_used = self._extract_json_text(content)
        filled_fields = self._missing_binding_fields(content, binding)
        content = self._fill_binding(content, binding)
        # C6-04:失败分类拆分——finish_reason=length 说明生成预算耗尽、
        # 响应可能被中途截断,与"完整返回但内容无效"(response_not_json)
        # 是不同的失败形态,处置建议也不同(提高 max_tokens/精简输出 vs 重试)。
        try:
            structured = validate_structured_response(
                request_kind, content, nonce=nonce, run_id=run_id, task_id=task_id,
                attempt=attempt, request_id=request_id, input_digest=input_digest,
                allowed_outputs=allowed_outputs,
            )
        except AIRequestError as exc:
            if exc.code == "response_not_json" and stream_meta.get("finish_reason") == "length":
                raise AIRequestError(
                    "response_length_truncated",
                    "生成预算耗尽(finish_reason=length),响应可能被截断;原判定:" + str(exc),
                ) from exc
            raise
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "real": True,
            "provider": self.provider,
            "model": self.model,
            "request_kind": request_kind,
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "nonce": nonce,
            "started_at": started.isoformat().replace("+00:00", "Z"),
            "finished_at": finished.isoformat().replace("+00:00", "Z"),
            "latency_ms": int((finished - started).total_seconds() * 1000),
            "stream_timing": stream_meta,
            "json_extraction_used": extraction_used,
            "raw_content_sha256": raw_content_sha256,
            "binding_fill": {"filled_fields": filled_fields,
                             "note": "nonce 必须由模型回传;其余绑定字段为本地可信补齐,见 raw_content_sha256"},
            "response_sha256": sha256_bytes(canonical_json(structured)),
            "input_sha256": input_digest,
            "structured": structured,
        }


def probe_connection(config: Mapping[str, Any], *, timeout_seconds: float = 15.0) -> dict[str, Any]:
    """W1:真实连通性探测——检查地址可达性;普通 GET 不单独证明认证或生成能力。

    明确边界:
    - 这是一次真实外部请求,只在用户显式点击"测试连接"后调用;
    - OpenAI 兼容端点走 ``GET <base_url>/models``(不产生生成消费);
    - 云网关走 ``GET <endpoint>`` 并带应用访问密钥头(不发起补全);
    - 结果为 ``reachable_unverified`` / ``blocked`` / ``unreachable``,**不宣称生成
      能力已验证**;生成能力由一次真实评估调用来证明;
    - 返回体不回显任何密钥,只给状态码类别与中文原因。
    """
    provider = config.get("provider")
    timeout = float(timeout_seconds)
    if provider == "openai_compatible":
        url = str(config.get("base_url", "")).rstrip("/") + "/models"
        headers = {"Authorization": "Bearer " + str(config.get("api_key", "")),
                   "Accept": "application/json"}
    elif provider == "workbuddy_gateway":
        url = str(config.get("endpoint", "")).rstrip("/")
        headers = {"x-wb-webapp-access-key": str(config.get("publishable_key", "")),
                   "Accept": "application/json"}
    else:
        return {"state": "blocked", "provider": str(provider),
                "reason": "未知 AI 提供方类型,无法探测"}
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = int(getattr(response, "status", 0) or response.getcode())
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        if code in (401, 403):
            return {"state": "blocked", "provider": str(provider), "http_status": code,
                    "reason": "服务可达但凭据未被接受(密钥/访问密钥无效或已过期)"}
        if code in (404, 405, 422):
            # 该响应只能证明地址可达；无认证端点的错误不能证明凭据有效。
            return {"state": "reachable_unverified", "reachability": "reachable",
                    "authentication": "unverified", "generation": "unverified",
                    "provider": str(provider), "http_status": code,
                    "reason": "服务地址可达,但认证未核实(该地址不提供可验证的探测接口)"}
        return {"state": "blocked", "provider": str(provider), "http_status": code,
                "reason": "服务返回 " + str(code) + ",连通性未核实"}
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        return {"state": "unreachable", "provider": str(provider),
                "reason": "无法连接:" + sanitize_text(type(exc).__name__ + " " + str(exc))[:160]}
    except Exception as exc:  # noqa: BLE001 - 探测失败也必须如实报告,不抛出
        return {"state": "unreachable", "provider": str(provider),
                "reason": "探测失败:" + sanitize_text(type(exc).__name__)[:80]}
    if 200 <= code < 300:
        return {"state": "reachable_unverified", "reachability": "reachable",
                "authentication": "unverified", "generation": "unverified",
                "provider": str(provider), "http_status": code,
                "reason": "服务地址可达;普通 GET 成功不能独立证明凭据有效,生成能力尚未验证"}
    return {"state": "blocked", "provider": str(provider), "http_status": code,
            "reason": "服务返回 " + str(code) + ",连通性未核实"}


def adapter_from_config(config: Mapping[str, Any]) -> "AIAdapter | WorkBuddyGatewayAdapter":
    """从 aiconfig 的已校验配置构造真实适配器(工厂)。"""
    provider = config.get("provider")
    if provider == "workbuddy_gateway":
        return WorkBuddyGatewayAdapter(
            endpoint=str(config["endpoint"]),
            publishable_key=str(config["publishable_key"]),
            model=str(config["model"]),
        )
    if provider == "openai_compatible":
        return AIAdapter(
            base_url=str(config["base_url"]),
            api_key=str(config["api_key"]),
            model=str(config["model"]),
            provider=str(config.get("provider_label") or "openai_compatible"),
        )
    raise AIRequestError("config_invalid", "未知 AI 提供方类型")


__all__ = [
    "ADAPTER_SCHEMA_VERSION",
    "AIAdapter",
    "AIRequestError",
    "MAX_RESPONSE_BYTES",
    "MockAdapter",
    "REQUEST_KINDS",
    "WorkBuddyGatewayAdapter",
    "adapter_from_config",
    "probe_connection",
    "validate_structured_payload",
    "validate_structured_response",
]
