# -*- coding: utf-8 -*-
"""AI 提供方配置:小白向导落盘、产品运行时读取的最小配置层。

设计要点:
- 配置文件存放在用户级目录(Windows: %LOCALAPPDATA%/OpenCoding/ai_provider.json),
  不进项目树、不进日志;文件权限收紧到当前用户。
- 支持两类提供方:
  * workbuddy_gateway —— WorkBuddy 云网关(免个人密钥,按应用绑定,流式接口);
  * openai_compatible —— 任意 OpenAI 兼容端点(如 DashScope/DeepSeek/本地 Ollama)。
- api_key / publishable_key 只进内存与配置文件,绝不写入日志、收据、会话或报告。
- 配置损坏/缺失一律返回 None 并给出中文原因,绝不抛出带敏感内容的异常。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Optional

CONFIG_SCHEMA_VERSION = 1
PROVIDERS = ("workbuddy_gateway", "openai_compatible")

_FILE_NAME = "ai_provider.json"


def config_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "OpenCoding" / _FILE_NAME


def _redact(doc: dict) -> dict:
    """返回可安全展示/记录的配置视图(密钥以指纹代替)。"""
    import hashlib

    view = {k: v for k, v in doc.items() if k != "schema_version"}
    for key_field in ("api_key", "publishable_key"):
        if view.get(key_field):
            digest = hashlib.sha256(str(view[key_field]).encode("utf-8")).hexdigest()
            view[key_field] = "sha256:" + digest[:16] + "…"
    return view


def redacted_view(doc: Optional[dict]) -> Optional[dict]:
    if not isinstance(doc, dict):
        return None
    return _redact(doc)


def load_config() -> tuple[Optional[dict], Optional[str]]:
    """读取配置。返回 (config|None, 中文错误|None)。永不抛异常、永不泄露密钥。"""
    path = config_path()
    if not path.is_file():
        return None, "尚未配置 AI 接入"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, "AI 配置文件无法读取:" + type(exc).__name__
    if not isinstance(doc, dict) or doc.get("schema_version") != CONFIG_SCHEMA_VERSION:
        return None, "AI 配置版本不受支持,请重新运行接入向导"
    provider = doc.get("provider")
    if provider not in PROVIDERS:
        return None, "AI 配置的提供方类型无效,请重新运行接入向导"
    if provider == "workbuddy_gateway":
        for field in ("endpoint", "publishable_key", "model"):
            if not isinstance(doc.get(field), str) or not doc[field].strip():
                return None, "AI 配置缺少字段:" + field + ",请重新运行接入向导"
    else:
        for field in ("base_url", "api_key", "model"):
            if not isinstance(doc.get(field), str) or not doc[field].strip():
                return None, "AI 配置缺少字段:" + field + ",请重新运行接入向导"
        if not doc["base_url"].startswith(("https://", "http://")):
            return None, "AI 配置的 base_url 必须是 http(s) 地址"
    return doc, None


def save_config(doc: dict) -> tuple[bool, Optional[str]]:
    """写入配置(权限收紧);返回 (是否成功, 中文错误)。"""
    if doc.get("provider") not in PROVIDERS:
        return False, "未知的 AI 提供方类型"
    doc = {**doc, "schema_version": CONFIG_SCHEMA_VERSION}
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    except OSError as exc:
        return False, "AI 配置写入失败:" + type(exc).__name__
    return True, None


def clear_config() -> bool:
    try:
        config_path().unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _last_test_path() -> Path:
    return config_path().with_name("ai_last_test.json")


def save_last_test(result: dict) -> None:
    """记录最近一次**真实连通性测试**结果(与"配置已保存"分开,不得混为一谈)。"""
    try:
        path = _last_test_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def load_last_test() -> dict:
    path = _last_test_path()
    if not path.is_file():
        return {"state": "not_tested", "note": "尚未做过连通性测试"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "not_tested", "note": "上次测试记录不可读"}
    if not isinstance(value, dict):
        return {"state": "not_tested"}
    if value.get("state") == "verified" and value.get("evidence_kind") != "models_json_v1":
        # 旧版把 HTTP 错误页/网关首页也记为 verified；只降级展示，不改写历史文件。
        return {**value, "state": "not_tested",
                "reason": "旧探测缺少有效模型目录证据，需重新测试；生成能力仍需真实评估"}
    return value


def describe_status() -> dict[str, Any]:
    """给工作台首屏用的当前 AI 能力状态(脱敏)。"""
    doc, err = load_config()
    if doc is None:
        return {"configured": False, "reason": err, "provider": None,
                "test_state": "not_configured"}
    view = _redact(doc)
    view["configured"] = True
    view["test_state"] = str(load_last_test().get("state") or "not_tested")
    return view


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "PROVIDERS",
    "clear_config",
    "config_path",
    "describe_status",
    "load_config",
    "load_last_test",
    "redacted_view",
    "save_config",
    "save_last_test",
]
