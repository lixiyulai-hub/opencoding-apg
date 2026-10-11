"""Persistent request reservations. Unknown outcomes consume capacity, never refund it."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from pathlib import Path

from .facts import _atomic_write_json
from .grants import _grant_store_lock
from .safety import _root_path, sanitize_text
from .sessions import _check_existing_chain


class RequestBudgetError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _path(root, scope):
    root = _root_path(root)
    if not isinstance(scope, str) or not scope.strip() or len(scope) > 256:
        raise RequestBudgetError("request_budget_scope_invalid", "请求预算范围无效")
    path = root / ".opencoding" / "request-budgets" / (hashlib.sha256(scope.encode()).hexdigest() + ".json")
    _check_existing_chain(path, root=root)
    return path


def _load(path):
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (value["schema_version"] != 1 or value["root"] != str(_root_path(path.parents[2]))
                or value["scope_digest"] != path.stem or type(value["limit"]) is not int
                or value["limit"] < 1 or type(value["window_limit"]) is not int
                or value["window_limit"] < 1 or type(value["window_seconds"]) is not int
                or value["window_seconds"] < 1 or not isinstance(value["requests"], list)
                or not isinstance(value["extensions"], list)):
            raise ValueError()
        seen = set()
        for request in value["requests"]:
            if (not isinstance(request["reservation_id"], str)
                    or not re.fullmatch(r"[0-9a-f]{32}", request["reservation_id"])
                    or request["reservation_id"] in seen
                    or type(request["reserved_at"]) not in (int, float)
                    or not math.isfinite(request["reserved_at"])
                    or request["reserved_at"] < 0
                    or request["status"] not in ("unknown", "succeeded", "failed")):
                raise ValueError()
            seen.add(request["reservation_id"])
            if "finished_at" in request and (type(request["finished_at"]) not in (int, float)
                    or not math.isfinite(request["finished_at"]) or request["finished_at"] < 0):
                raise ValueError()
            if not isinstance(request["request_id"], str):
                raise ValueError()
            for field in ("request_id", "run_id", "task_id", "session_id"):
                identity = request[field]
                if identity is not None and (not isinstance(identity, str)
                        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identity)):
                    raise ValueError()
        for extension in value["extensions"]:
            if (type(extension["additional"]) is not int or not 1 <= extension["additional"] <= 100
                    or type(extension["at"]) not in (int, float) or not math.isfinite(extension["at"])
                    or any(not isinstance(extension[key], str) or not extension[key].strip()
                           for key in ("reason", "issued_by"))):
                raise ValueError()
        return value
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        raise RequestBudgetError("request_budget_invalid", "请求预算记录损坏，禁止继续调用") from exc


def _write(root, path, value):
    _check_existing_chain(path, root=_root_path(root))
    _atomic_write_json(path, value)


def reserve(root, scope, *, limit=20, window_limit=5, window_seconds=60,
            request_id=None, run_id=None, task_id=None, session_id=None):
    """Reserve before dispatch; policies are immutable after the first reservation."""
    for number in (limit, window_limit, window_seconds):
        if type(number) is not int or number < 1:
            raise RequestBudgetError("request_budget_policy_invalid", "请求预算必须是正整数")
    request_id = ("req-" + uuid.uuid4().hex) if request_id is None else request_id
    identity = {"request_id": request_id, "run_id": run_id, "task_id": task_id, "session_id": session_id}
    for key, value in (("request_id", request_id), ("run_id", run_id),
                       ("task_id", task_id), ("session_id", session_id)):
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                raise RequestBudgetError("request_budget_identity_invalid", "请求身份无效")
            identity[key] = value
    with _grant_store_lock(root):
        path = _path(root, scope)
        book = _load(path) or {"schema_version": 1, "root": str(_root_path(root)),
                              "scope_digest": path.stem, "limit": limit,
                              "window_limit": window_limit, "window_seconds": window_seconds,
                              "requests": [], "extensions": []}
        now = time.time()
        if len(book["requests"]) >= book["limit"]:
            raise RequestBudgetError("request_budget_exhausted", "累计请求预算已用尽，请明确扩额后重试")
        if sum(item["reserved_at"] > now - book["window_seconds"] for item in book["requests"]) >= book["window_limit"]:
            raise RequestBudgetError("request_budget_rate_limited", "请求过于频繁，请稍后重试")
        reservation = {"reservation_id": uuid.uuid4().hex, "reserved_at": now,
                       "status": "unknown", **identity}
        book["requests"].append(reservation)
        _write(root, path, book)
        return dict(reservation)


def finish(root, scope, reservation_id, *, status):
    if status not in ("unknown", "succeeded", "failed"):
        raise RequestBudgetError("request_budget_status_invalid", "请求结果无效")
    with _grant_store_lock(root):
        path = _path(root, scope)
        book = _load(path)
        for request in (book or {}).get("requests", []):
            if request["reservation_id"] == reservation_id:
                if request["status"] != "unknown" and request["status"] != status:
                    raise RequestBudgetError("request_budget_outcome_conflict", "请求结果已记录")
                request.update(status=status, finished_at=time.time())
                _write(root, path, book)
                return dict(request)
        raise RequestBudgetError("request_budget_reservation_missing", "请求预算预留不存在")


def read(root, scope):
    with _grant_store_lock(root, create=False):
        book = _load(_path(root, scope))
        if book is None:
            return {"schema_version": 1, "initialized": False, "used": 0, "requests": [], "extensions": []}
        active = sorted(item["reserved_at"] for item in book["requests"]
                        if item["reserved_at"] > time.time() - book["window_seconds"])
        retry_after = (max(0, math.ceil(active[len(active) - book["window_limit"]]
                       + book["window_seconds"] - time.time()))
                       if len(active) >= book["window_limit"] else 0)
        return {**book, "initialized": True, "used": len(book["requests"]),
                "remaining": max(0, book["limit"] - len(book["requests"])),
                "window_remaining": max(0, book["window_limit"] - len(active)),
                "retry_after_seconds": retry_after}


def extend(root, scope, *, additional, confirmed, reason, issued_by):
    if confirmed is not True or type(additional) is not int or not 1 <= additional <= 100:
        raise RequestBudgetError("request_budget_extension_invalid", "扩额需要明确确认和 1 到 100 的整数额度")
    if any(not isinstance(value, str) or not value.strip() for value in (reason, issued_by)):
        raise RequestBudgetError("request_budget_extension_invalid", "扩额必须记录原因和确认者")
    with _grant_store_lock(root):
        path = _path(root, scope)
        book = _load(path)
        if book is None:
            raise RequestBudgetError("request_budget_missing", "尚无可扩额的请求预算")
        book["limit"] += additional
        book["extensions"].append({"additional": additional, "at": time.time(),
                                   "reason": sanitize_text(reason)[:300], "issued_by": sanitize_text(issued_by)[:120]})
        _write(root, path, book)
        return {**book, "used": len(book["requests"]), "remaining": book["limit"] - len(book["requests"])}
