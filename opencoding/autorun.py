"""自主执行循环：把受支持的任务在有限批次授权内连续推进到验收。

控制层负责：依赖排序、逐步凭据、受限写入、独立验收、有限修复、预算
登记、断点接续与中文进度。AI 适配器只产出候选文件，不能签发凭据、
不能改写验收器、不能扩大授权。借还登记十任务例的任务图与验收器是
合成示例装置，业务源码必须由适配器在运行时真实产出。

安全返修（F03–F07/F09）后的写入与验收结构：
- 候选先整体校验，再在登记所有权的隔离工作副本中叠加并验收；真实项目
  在验收通过前零写入，未知候选代码从不在真实用户项目中直接运行。
- 提交点一次核对：有效授权、请求时前像、每文件凭据消费；随后经既有
  事务层（transactions.apply_changes）原子应用并保留前像与回执。
- 取消状态存于独立受控取消存储，账本旧对象不能覆盖；接收结果后、
  应用候选前、提交后三个检查点重查。
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import importlib.machinery
import json
import os
from collections import Counter
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from . import grants, sandbox, transactions, uxtext
from .aiadapter import ADAPTER_SCHEMA_VERSION, AIRequestError, validate_structured_payload
from .safety import canonical_json, inspect_sensitive, sanitize_text, sha256_bytes


AUTORUN_SCHEMA_VERSION = "1.0"
MAX_REPAIR_ROUNDS = 2
MAX_CONTENT_BYTES = 128 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_BINDING_MAX_BYTES = 4 * 1024 * 1024

Verifying = Callable[[Path], tuple[bool, list[str]]]


class AutorunError(ValueError):
    """A fail-closed autonomous-run error with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_SANDBOX_STATE: dict[str, dict[str, str]] = {}


def _control_root(project: Path) -> Path | None:
    """定位项目控制元数据区（.opencoding）；找不到返回 None。"""

    current = Path(project).resolve()
    for candidate in (current, *current.parents):
        if candidate.name == ".opencoding":
            return candidate
        if (candidate / ".opencoding").is_dir():
            return candidate / ".opencoding"
    return None


def _set_sandbox_state(project: Path, scratch: Path, guard_dir: Path) -> None:
    _SANDBOX_STATE["current"] = {
        "project": str(Path(project).resolve()),
        "scratch": str(Path(scratch).resolve()),
        "guard_dir": str(guard_dir),
    }


_TRUSTED_FIXTURES: dict[str, str | None] = {}


def _set_trusted_fixture(project: Path, fixture: str | None) -> None:
    """N01-a：受信任夹具按项目根登记（仅测试代码显式传入已登记身份时生效）。"""

    _TRUSTED_FIXTURES[str(Path(project).resolve())] = fixture


def _trusted_fixture_for(project: Path) -> str | None:
    return _TRUSTED_FIXTURES.get(str(Path(project).resolve()))


def _guard_for(project: Path) -> Path:
    """取当前工作副本对应的守卫目录；未处于受控上下文时按调用点现算。"""

    state = _SANDBOX_STATE.get("current")
    resolved = str(Path(project).resolve())
    if isinstance(state, dict) and state.get("scratch") == resolved:
        return Path(state["guard_dir"])
    control = _control_root(project)
    return sandbox.build_guard(
        writable_root=project,
        protected_roots=[control] if control is not None else [],
        read_exempt=[project],
    )


def _prepare_sandbox(project: Path, scratch: Path, trusted_fixture: str | None = None) -> Path:
    """为本次隔离运行建立真实受限环境，并自检限制是否生效（S01/F07）。"""

    control = _control_root(project)
    guard_dir = sandbox.build_guard(
        writable_root=scratch,
        protected_roots=[control] if control is not None else [],
        read_exempt=[scratch],
    )
    try:
        sandbox.self_check(guard_dir, writable_root=scratch, protected_probe_dir=control if control is not None else scratch)
    except sandbox.SandboxError as exc:
        raise AutorunError(exc.code, "无法在本机建立受控执行环境，拒绝执行候选代码：" + str(exc)) from exc
    # N01 纵深防御：即使自检通过，也只有在具备实际受限执行环境（或显式受信任合成通道）时
    # 才允许建立候选执行上下文。
    capability = sandbox.execution_capability(guard_verified=True)
    fixture_ok = trusted_fixture in sandbox.REVIEWED_FIXTURES
    if not capability["available"] and not fixture_ok:
        raise AutorunError(
            "execution_capability_unavailable",
            "未知候选的自动执行不可用（" + str(capability["kind"]) + "）：" + str(capability["reason"]),
        )
    if fixture_ok:
        _set_trusted_fixture(project, trusted_fixture)
        # 验收器以 scratch 为"project"参数运行，须同时登记隔离工作副本根。
        _set_trusted_fixture(scratch, trusted_fixture)
    _set_sandbox_state(project, scratch, guard_dir)
    return guard_dir


def _run_cli(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return sandbox.run(
        [sys.executable, "-m", "lendreg", *args],
        cwd=project,
        guard_dir=_guard_for(project),
        trusted_fixture=_trusted_fixture_for(project),
        timeout=30,
    )


def _read_data(project: Path) -> dict[str, Any]:
    import json as _json

    path = project / "data" / "store.json"
    if not path.exists():
        return {"items": {}, "loans": []}
    return _json.loads(path.read_text(encoding="utf-8"))


# F08/D01（r8/Y01）：信任来源最终分层。r7 的实现把密语与结果路径都放进候选
# 所在解释器的环境变量——审核 Y01 证明：候选即使没有任何运行时业务函数，也可以
# 在模块导入阶段读取自己的 OPENCODING_D01_SECRET/OPENCODING_D01_RESULT，按同一
# SHA-256/HMAC 算法写出合法密封结果并退出，验证层无法区分"驱动写出"与"候选写
# 出"。HMAC 验签真实，但它证明的是"持有密语者写出"，不是"可信业务驱动写出"。
#
# 本轮边界声明（可写边界、输入/期望持有者、完成依据）：
# 1. 候选与验证器在同一解释器、同一写根内：密语（env）与结果文件路径对候选
#    同样可读可写。**密封观察本身不是候选不可制造的**——这是执行边界事实，
#    不再以"理论上残留"表述。
# 2. 因此 D01 的实际验收只允许两种互斥结论：
#    a) **材料身份层**：候选提交的导入面字节（lendreg/__init__.py 与
#       lendreg/models.py）与下方登记的已审查固定参考逐字节一致——候选代码
#       被字节钉死，不可能在探针进程内执行参考之外的行为，密封观察才可能
#       来自真实业务调用。此结论标注为"固定参考合成控制流验收"，不构成对
#       未知候选的独立动态验收。
#    b) 其余一切候选：**verification_unavailable / 冻结**。当前可用执行边界
#       不能提供候选不能自行制造的业务观察，安全拒绝是终态，不计实际功能
#       验收；真实未知候选动态验收按缺失条件（独立于候选解释器与写根的观察
#       通道）保留未验证。不再以换签名/密语藏法/结果文件位置宣称独立可信。
# 3. 材料身份登记值来自已审查测试夹具（tests 包 d01-models 任务剧本）的
#    UTF-8 字节摘要；持久测试绑定该登记值与夹具实际字节，夹具变更必须显式
#    更新登记并同步说明。不能凭任意字符串标签（trusted_fixture）或模型自报
#    给予豁免——标签只门禁沙箱执行，不门禁验收结论。
_D01_MATERIAL_SURFACE = ("lendreg/__init__.py", "lendreg/models.py")
_D01_REVIEWED_MATERIAL: dict[str, str] = {
    "lendreg/__init__.py": "aadeecb996a6f24dd66261b0f8a1461bbad2dd3c4d7f21b47cc1706e589a4c51",
    "lendreg/models.py": "ddb8686f6b52999c7e7a74c9d95e55f4fa86818b4614666322f164191785661f",
}


def _d01_material_problems(project: Path) -> list[str]:
    """材料身份层：核对候选导入面字节是否与已审查固定参考逐字节一致。"""

    problems: list[str] = []
    for relative in _D01_MATERIAL_SURFACE:
        path = project / relative
        try:
            digest = sha256_bytes(path.read_bytes())
        except (OSError, ValueError):
            problems.append("D01 材料身份核对失败：缺少 " + relative + "。")
            continue
        expected = _D01_REVIEWED_MATERIAL.get(relative)
        if expected is None or digest != expected:
            problems.append(
                "D01 材料身份未登记：" + relative + " 的字节与已审查固定参考不一致。"
            )
    return problems


# F08/r8-Z01：材料层只钉住两个文件的字节，不等于探针实际加载的代码。Python 的
# 实际解析规则下，同名包目录（lendreg/models/__init__.py）会替代 lendreg/models.py
# 被加载；视图外任何额外模块、缓存或可变加载入口都可能改变实际执行的代码。
# 因此 D01 的固定参考执行规则是：
# 1. 探针只在**严格参考视图**内运行——视图仅包含刚刚逐字节核对过的两个登记
#    参考文件，不含项目内其他任何可执行内容；视图外的同名包/缓存/别名因此
#    不可能进入参考信任范围。
# 2. 视图内用 PathFinder.find_spec **只定位、不执行**地证明实际解析来源恰好
#    就是这两份登记字节（lendreg/__init__.py 与 lendreg/models.py）。探针的
#    其余导入（hashlib/hmac/json/os/sys）属于解释器自带可信运行时。
# 3. 无法建立或证明该边界时，在执行前结构化 verification_unavailable：不判
#    可信、不可交付（安全拒绝，不是独立动态验收）。绝不先 import 未知代码
#    再回看 __file__ 补做"事前"检查——那时未知代码已经执行。
def _d01_import_surface_problems(root: Path) -> tuple[list[str], dict[str, str]]:
    """静态解析 `lendreg`/`lendreg.models` 的实际导入来源（不执行任何代码）。"""

    problems: list[str] = []
    resolution: dict[str, str] = {}
    init_path = root / "lendreg" / "__init__.py"
    models_path = root / "lendreg" / "models.py"
    lendreg_dir = root / "lendreg"
    for label, path in (("lendreg/__init__.py", init_path), ("lendreg/models.py", models_path)):
        if path.exists() and _is_link(path):
            problems.append("D01 实际导入面身份不安全：" + label + " 是链接或重解析点。")
    if _is_link(lendreg_dir):
        problems.append("D01 实际导入面身份不安全：lendreg 目录是链接或重解析点。")
    if problems:
        return problems, resolution
    lendreg_spec = importlib.machinery.PathFinder.find_spec("lendreg", [str(root)])
    if lendreg_spec is None or lendreg_spec.origin != str(init_path):
        found = "未解析到" if lendreg_spec is None else str(lendreg_spec.origin)
        problems.append("D01 实际导入解析来源不是 lendreg/__init__.py（实际：" + found + "）。")
        return problems, resolution
    resolution["lendreg"] = lendreg_spec.origin
    search = list(lendreg_spec.submodule_search_locations or [])
    models_spec = importlib.machinery.PathFinder.find_spec("models", search)
    if models_spec is None or models_spec.origin != str(models_path):
        found = "未解析到" if models_spec is None else str(models_spec.origin)
        problems.append(
            "D01 实际导入解析来源不是 lendreg/models.py（实际：" + found + "）；"
            "同名包或其他可加载入口不能替代已登记参考。"
        )
        return problems, resolution
    resolution["lendreg.models"] = models_spec.origin
    return problems, resolution


def _stage_d01_strict_view(project: Path) -> tuple[Path | None, list[str], dict[str, str]]:
    """构造仅含已登记参考字节的严格参考视图；构建或解析失败返回问题列表。"""

    view = project / (".d01-strict-view-" + uuid.uuid4().hex)
    problems: list[str] = []
    try:
        (view / "lendreg").mkdir(parents=True)
        for relative in _D01_MATERIAL_SURFACE:
            source = project / relative
            shutil.copyfile(source, view / relative)
            digest = sha256_bytes((view / relative).read_bytes())
            if digest != _D01_REVIEWED_MATERIAL.get(relative):
                problems.append("D01 严格参考视图字节与登记参考不一致：" + relative + "。")
        if problems:
            return None, problems, {}
        surface_problems, resolution = _d01_import_surface_problems(view)
        if surface_problems:
            return None, surface_problems, resolution
    except OSError as exc:
        return None, ["D01 严格参考视图构建失败：" + type(exc).__name__ + " " + sanitize_text(str(exc))[:120]], {}
    return view, [], resolution


_D01_PROBE_SOURCE = (
    "import hashlib\n"
    "import hmac\n"
    "import json\n"
    "import os\n"
    "import sys\n"
    "sys.path.insert(0, '.')\n"
    "secret = os.environ.get('OPENCODING_D01_SECRET')\n"
    "result_path = os.environ.get('OPENCODING_D01_RESULT')\n"
    "if not secret or not result_path:\n"
    "    sys.exit(3)\n"
    "key = hashlib.sha256(secret.encode('utf-8')).digest()\n"
    "def seal(value):\n"
    "    payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')\n"
    "    return hmac.new(key, payload, hashlib.sha256).hexdigest()\n"
    "try:\n"
    "    from lendreg import models as _models\n"
    "except BaseException:\n"
    "    sys.exit(4)\n"
    "new_item = getattr(_models, 'new_item', None)\n"
    "new_loan = getattr(_models, 'new_loan', None)\n"
    "if not callable(new_item) or not callable(new_loan):\n"
    "    sys.exit(5)\n"
    "def rejected(call):\n"
    "    try:\n"
    "        call()\n"
    "    except ValueError:\n"
    "        return True\n"
    "    except BaseException:\n"
    "        return False\n"
    "    return False\n"
    "challenges = ['d01c-a', 'd01c-b']\n"
    "observations = []\n"
    "for challenge in challenges:\n"
    "    try:\n"
    "        item = new_item(challenge, '测试物品')\n"
    "        loan = new_loan(challenge + '-loan', challenge, '借用人甲')\n"
    "        payload = {\n"
    "            'item': item,\n"
    "            'loan': loan,\n"
    "            'invalid_empty_id_rejected': rejected(lambda: new_item('', '名称')),\n"
    "            'invalid_long_id_rejected': rejected(lambda: new_item('x' * 300, '名称')),\n"
    "            'empty_name_rejected': rejected(lambda: new_item(challenge + '-x', '')),\n"
    "        }\n"
    "        json.dumps(payload, ensure_ascii=False, sort_keys=True)\n"
    "    except BaseException:\n"
    "        sys.exit(6)\n"
    "    observations.append(payload)\n"
    "sealed = seal(observations)\n"
    "with open(result_path, 'w', encoding='utf-8') as handle:\n"
    "    handle.write(sealed + '\\n' + json.dumps(observations, ensure_ascii=False, sort_keys=True) + '\\n')\n"
)


_D01_EXIT_REASONS = {
    3: "D01 可信驱动缺少密语或结果路径，无法建立独立观察通道。",
    4: "D01 候选模型模块导入失败。",
    5: "D01 业务入口在运行时不可调用（仅在文本中声明不算）。",
    6: "D01 业务调用抛出异常。",
}


def _run_d01_trusted_probe(project: Path, exec_root: Path | None = None) -> tuple[list[dict[str, Any]] | None, str]:
    """启动可信驱动子进程并回收其密封的业务观察。

    密语与结果路径经 env 注入：密钥派生自密语；结果文件是执行根内控制器
    随机命名的文件（守卫只放行写根内路径，驱动自身可写）；stdout 与 argv 都
    不承载完成信号。F08/r8-Z01：`exec_root` 指定探针的实际执行根——探针的
    cwd、探针脚本与结果文件都放在严格参考视图内，`sys.path.insert(0, '.')`
    实际解析到的只能是视图内已核对的参考字节。返回 `(observations, output)`；
    无法取得密封且验签一致的观察时返回 `(None, output)`。
    """

    root = exec_root if exec_root is not None else project
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    guard_dir = _guard_for(project)
    result_path = root / (".d01-observation-" + uuid.uuid4().hex + ".json")
    probe = root / ".probe_d01.py"
    probe.write_text(_D01_PROBE_SOURCE, encoding="utf-8")
    try:
        result = sandbox.run(
            [sys.executable, ".probe_d01.py"],
            cwd=root,
            guard_dir=guard_dir,
            trusted_fixture=_trusted_fixture_for(project),
            timeout=30,
            base_env={
                "OPENCODING_D01_SECRET": secret,
                "OPENCODING_D01_RESULT": str(result_path),
            },
        )
    finally:
        probe.unlink(missing_ok=True)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        reason = _D01_EXIT_REASONS.get(result.returncode, "D01 可信驱动异常退出：" + str(result.returncode))
        return None, (reason + " | " + output)[-400:]
    try:
        raw = result_path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        sealed, payload_json = lines[0], "\n".join(lines[1:])
        observations = json.loads(payload_json)
    except (OSError, UnicodeError, ValueError, IndexError):
        return None, ("D01 未取得可信侧密封的业务观察结果。" + output)[-400:]
    finally:
        result_path.unlink(missing_ok=True)
    if not isinstance(observations, list):
        return None, ("D01 可信侧观察格式无效。" + output)[-400:]
    expected = hmac.new(key, payload_json.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sealed):
        return None, ("D01 密封观察验签失败，完成信号不能采信。" + output)[-400:]
    return observations, output[-400:]


def _d01_contract_problems(record: Mapping[str, Any], challenge: str) -> list[str]:
    item = record.get("item")
    loan = record.get("loan")
    problems: list[str] = []
    if not isinstance(item, dict):
        problems.append("D01 物品结果不是对象。")
    else:
        if item.get("id") != challenge:
            problems.append("D01 物品编号不是可信侧本轮挑战值。")
        if item.get("name") != "测试物品":
            problems.append("D01 物品名称不符合业务契约。")
        if item.get("status") != "available":
            problems.append("D01 新增物品状态不是 available。")
    if not isinstance(loan, dict):
        problems.append("D01 借出记录结果不是对象。")
    else:
        if loan.get("id") != challenge + "-loan":
            problems.append("D01 借出记录编号不是可信侧本轮挑战值。")
        if loan.get("item_id") != challenge:
            problems.append("D01 借出记录未关联本轮挑战值对应的物品。")
        if loan.get("closed") is not False:
            problems.append("D01 借出记录初始状态不是未归还。")
    for flag in ("invalid_empty_id_rejected", "invalid_long_id_rejected", "empty_name_rejected"):
        if record.get(flag) is not True:
            problems.append("D01 非法输入未被业务校验拒绝：" + flag)
    return problems


_D01_CHALLENGES = ("d01c-a", "d01c-b")


def _verify_d01_models(project: Path) -> tuple[bool, list[str]]:
    """D01 验收：先材料身份、后独立业务观察（r8/Y01 分层）。

    层 1（材料身份）：候选导入面字节必须与登记的已审查固定参考逐字节一致，
    否则当前执行边界无法提供候选不能自行制造的业务观察——按
    verification unavailable 冻结，不判可信、不可交付（安全拒绝，不计功能验收）。
    层 2（独立业务验证）：仅对字节钉死的固定参考运行密封探针并核对业务契约，
    结论标注为固定参考合成控制流验收。仍不声称对未知候选完成独立动态验收。
    """

    evidence: list[str] = []
    # r8/Y01 层 1：材料身份。任何字节偏差（包括对参考的注释级修改）都会改变
    # 探针进程内执行的代码，密封观察不再可归因于可信业务驱动。
    material_problems = _d01_material_problems(project)
    if material_problems:
        return False, evidence + material_problems + [
            "D01 当前执行边界无法提供候选不能自行制造的业务观察，"
            "验收不可用（verification unavailable）：不判可信、不可交付。"
        ]
    evidence.append("D01 材料身份=已登记固定参考（合成控制流验收，非未知候选独立动态验收）。")
    model_path = project / "lendreg" / "models.py"
    try:
        model_tree = ast.parse(model_path.read_text(encoding="utf-8"), filename=str(model_path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        return False, ["D01 模型源码不可读或无法编译：" + type(exc).__name__]
    declared = {
        node.name for node in ast.walk(model_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if not {"new_item", "new_loan"}.issubset(declared):
        return False, ["D01 未发现可信侧要求的显式 new_item/new_loan 业务入口。"]
    # F08/r8-Z01：材料层之后、执行之前，把探针限制在仅含已登记参考字节的
    # 严格参考视图内，并用不执行的静态解析证明实际导入来源就是这两份字节。
    # 视图外的同名包、缓存或可变加载入口因此无法替代实际运行的代码。
    view, view_problems, resolution = _stage_d01_strict_view(project)
    if view is None:
        return False, evidence + view_problems + [
            "D01 实际导入面无法限定为已登记参考，验收不可用（verification unavailable）："
            "不判可信、不可交付。"
        ]
    evidence.append(
        "D01 实际导入面=严格参考视图（仅登记 lendreg/__init__.py 与 lendreg/models.py，"
        "不含项目内其他可执行内容）。"
    )
    evidence.append(
        "D01 视图内实际解析来源（PathFinder 定位，未执行）："
        + "; ".join(name + " -> " + origin for name, origin in sorted(resolution.items()))
        + "。"
    )
    try:
        observations, output = _run_d01_trusted_probe(project, exec_root=view)
    finally:
        shutil.rmtree(view, ignore_errors=True)
    # r7/X01：新探针不再向 stdout 打印完成标记，正常运行时 output 为空串；
    # repair_context 绑定校验要求证据逐项非空白，空输出不得进入证据列表。
    if output:
        evidence.append(output)
    if observations is None:
        return False, evidence + ["D01 未取得可信侧独立密封的业务观察，验收不可用（verification unavailable）。"]
    if len(observations) != len(_D01_CHALLENGES):
        return False, evidence + ["D01 可信侧观察数量与可信挑战数量不一致。"]
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            return False, evidence + ["D01 第 " + str(index + 1) + " 组可信观察无效。"]
        problems = _d01_contract_problems(observation, _D01_CHALLENGES[index])
        if problems:
            return False, evidence + problems
    first_item = observations[0].get("item")
    second_item = observations[1].get("item")
    if (
        isinstance(first_item, dict)
        and isinstance(second_item, dict)
        and first_item.get("id") == second_item.get("id")
    ):
        return False, evidence + ["D01 两组可信挑战得到相同业务结果，完成信号可能来自固定输出。"]
    return True, evidence


def _verify_d02_storage(project: Path) -> tuple[bool, list[str]]:
    probe = project / ".probe_d02.py"
    probe.write_text(
        "import sys; sys.path.insert(0, '.')\n"
        "from lendreg import storage\n"
        "data = {'items': {'a': {'id': 'a', 'name': '甲', 'status': 'available'}}, 'loans': []}\n"
        "storage.save_data('data/store.json', data)\n"
        "loaded = storage.load_data('data/store.json')\n"
        "assert loaded == data, '重读数据不一致'\n"
        "print('D02 OK')\n",
        encoding="utf-8",
    )
    try:
        result = sandbox.run(
            [sys.executable, ".probe_d02.py"],
            cwd=project,
            guard_dir=_guard_for(project),
        trusted_fixture=_trusted_fixture_for(project),
            timeout=30,
        )
        return result.returncode == 0 and "D02 OK" in result.stdout, [(result.stdout + result.stderr).strip()[-400:]]
    finally:
        probe.unlink(missing_ok=True)


def _verify_d03_add(project: Path) -> tuple[bool, list[str]]:
    evidence: list[str] = []
    ok = True
    first = _run_cli(project, "add", "it-1", "瑜伽垫")
    evidence.append((first.stdout + first.stderr).strip()[-300:])
    ok = ok and first.returncode == 0
    data = _read_data(project)
    ok = ok and "it-1" in data.get("items", {})
    if ok:
        second = _run_cli(project, "add", "it-1", "重复瑜伽垫")
        evidence.append((second.stdout + second.stderr).strip()[-300:])
        ok = ok and second.returncode != 0 and "it-1" in _read_data(project).get("items", {})
    return ok, evidence


def _verify_d04_lend(project: Path) -> tuple[bool, list[str]]:
    _run_cli(project, "add", "it-2", "打气筒")
    result = _run_cli(project, "lend", "it-2", "借用人乙")
    evidence = [(result.stdout + result.stderr).strip()[-300:]]
    data = _read_data(project)
    item = data.get("items", {}).get("it-2", {})
    loans = data.get("loans", [])
    ok = result.returncode == 0 and item.get("status") == "borrowed" and any(
        loan.get("item_id") == "it-2" and loan.get("borrower") == "借用人乙" and not loan.get("closed") for loan in loans
    )
    return ok, evidence


def _verify_d05_guard(project: Path) -> tuple[bool, list[str]]:
    artifact_ok, artifact_evidence = _verify_python_test_artifact(
        project, "tests/test_borrow_guard.py", "test_borrow_guard"
    )
    _run_cli(project, "add", "it-3", "工具箱")
    _run_cli(project, "lend", "it-3", "借用人丙")
    before = _read_data(project)
    result = _run_cli(project, "lend", "it-3", "借用人丁")
    after = _read_data(project)
    evidence = artifact_evidence + [(result.stdout + result.stderr).strip()[-300:]]
    ok = (
        artifact_ok
        and result.returncode != 0
        and after == before
        and after.get("items", {}).get("it-3", {}).get("status") == "borrowed"
    )
    return ok, evidence


def _verify_d06_return(project: Path) -> tuple[bool, list[str]]:
    _run_cli(project, "add", "it-4", "折叠椅")
    _run_cli(project, "lend", "it-4", "借用人戊")
    result = _run_cli(project, "return", "it-4")
    evidence = [(result.stdout + result.stderr).strip()[-300:]]
    data = _read_data(project)
    item = data.get("items", {}).get("it-4", {})
    loans = data.get("loans", [])
    ok = result.returncode == 0 and item.get("status") == "available" and any(
        loan.get("item_id") == "it-4" and loan.get("closed") for loan in loans
    )
    return ok, evidence


def _verify_d07_list(project: Path) -> tuple[bool, list[str]]:
    _run_cli(project, "add", "it-5", "雨伞")
    _run_cli(project, "add", "it-6", "台灯")
    _run_cli(project, "lend", "it-6", "借用人己")
    result = _run_cli(project, "list")
    evidence = [(result.stdout + result.stderr).strip()[-500:]]
    ok = (
        result.returncode == 0
        and "雨伞" in result.stdout
        and "台灯" in result.stdout
        and "借用人己" in result.stdout
    )
    return ok, evidence


def _verify_d08_chinese(project: Path) -> tuple[bool, list[str]]:
    result = _run_cli(project, "lend", "不存在的物品", "某人")
    evidence = [(result.stdout + result.stderr).strip()[-300:]]
    text = result.stderr + result.stdout
    chinese = any("一" <= char <= "鿿" for char in text)
    english_bare = any(token in text for token in ("Traceback", "KeyError", "Usage:"))
    ok = result.returncode != 0 and chinese and not english_bare
    return ok, evidence


def _verify_d09_restart(project: Path) -> tuple[bool, list[str]]:
    artifact_ok, artifact_evidence = _verify_python_test_artifact(
        project, "tests/test_restart_persistence.py", "test_restart_persistence"
    )
    _run_cli(project, "add", "it-7", "电磁炉")
    _run_cli(project, "lend", "it-7", "借用人庚")
    result = _run_cli(project, "list")
    evidence = artifact_evidence + [(result.stdout + result.stderr).strip()[-400:]]
    ok = artifact_ok and result.returncode == 0 and "电磁炉" in result.stdout and "借用人庚" in result.stdout
    return ok, evidence


def _verify_d10_full_flow(project: Path) -> tuple[bool, list[str]]:
    evidence: list[str] = []
    script_path = project / "scripts" / "full_flow.py"
    recovery_path = project / "RECOVERY.md"
    try:
        script_source = script_path.read_text(encoding="utf-8")
        compile(script_source, str(script_path), "exec")
        recovery = recovery_path.read_text(encoding="utf-8")
    except (OSError, SyntaxError, UnicodeError) as exc:
        return False, ["交付物缺失或无效：" + type(exc).__name__]
    recovery_lower = recovery.casefold()
    required_recovery_paths = (
        "lendreg/__init__.py",
        "lendreg/models.py",
        "lendreg/storage.py",
        "lendreg/app.py",
        "lendreg/__main__.py",
        "lendreg/messages.py",
        "tests/test_borrow_guard.py",
        "tests/test_restart_persistence.py",
        "scripts/full_flow.py",
        "RECOVERY.md",
    )
    if (
        not script_source.strip()
        or not recovery.strip()
        or any(path.casefold() not in recovery_lower for path in required_recovery_paths)
        or not any(term in recovery_lower for term in ("data/", "业务数据", "data\\"))
        or not any(term in recovery_lower for term in ("回滚", "rollback"))
    ):
        return False, ["RECOVERY.md 未提供有效源码清单和保留业务数据的回滚说明。"]
    flow_root = project / ".probe-full-flow"
    flow_root.mkdir(parents=True, exist_ok=False)
    shutil.copytree(project / "lendreg", flow_root / "lendreg")
    (flow_root / "scripts").mkdir()
    shutil.copy2(script_path, flow_root / "scripts" / "full_flow.py")
    script_result = sandbox.run(
        [sys.executable, "scripts/full_flow.py"],
        cwd=flow_root,
        guard_dir=_guard_for(flow_root),
        timeout=30,
        base_env={"PYTHONUTF8": "1"},
        trusted_fixture=_trusted_fixture_for(project),
    )
    evidence.append((script_result.stdout + script_result.stderr).strip()[-500:])
    flow_data = _read_data(flow_root)
    flow_items = flow_data.get("items", {})
    flow_loans = flow_data.get("loans", [])
    flow_ok = (
        script_result.returncode == 0
        and flow_items.get("ff-1", {}).get("name") == "投影仪"
        and flow_items.get("ff-1", {}).get("status") == "available"
        and flow_items.get("ff-2", {}).get("name") == "插线板"
        and flow_items.get("ff-2", {}).get("status") == "available"
        and len(flow_loans) == 1
        and flow_loans[0].get("item_id") == "ff-1"
        and flow_loans[0].get("borrower") == "借用人辛"
        and flow_loans[0].get("closed") is True
    )
    if not flow_ok:
        evidence.append("流程脚本未在独立副本中形成完整且一致的业务后态。")
    ok = flow_ok
    for args in (("add", "it-8", "投影仪"), ("add", "it-9", "插线板"), ("lend", "it-8", "借用人辛")):
        result = _run_cli(project, *args)
        evidence.append((result.stdout + result.stderr).strip()[-200:])
        ok = ok and result.returncode == 0
    guard = _run_cli(project, "lend", "it-8", "借用人壬")
    ok = ok and guard.returncode != 0
    back = _run_cli(project, "return", "it-8")
    ok = ok and back.returncode == 0
    listing = _run_cli(project, "list")
    ok = ok and listing.returncode == 0 and "投影仪" in listing.stdout and "插线板" in listing.stdout
    data = _read_data(project)
    ok = ok and data.get("items", {}).get("it-8", {}).get("status") == "available"
    ok = ok and any(loan.get("item_id") == "it-8" and loan.get("closed") for loan in data.get("loans", []))
    evidence.append((listing.stdout + listing.stderr).strip()[-400:])
    return ok, evidence


_TEST_MARKER = "__TRUSTED_TEST__"

# F08/D05-D09：可信侧的测试运行驱动。结果记录由可信驱动从真实 unittest 结果
# 对象生成（区分断言失败 failure 与非断言错误 error），并携带可信侧生成的
# 一次性 nonce；不再靠解析 stdout 文本判断业务故障。
_TEST_RUNNER_SOURCE = (
    "import io\n"
    "import json\n"
    "import sys\n"
    "import unittest\n"
    "sys.path.insert(0, '.')\n"
    "nonce = sys.argv[1]\n"
    "module = sys.argv[2]\n"
    "def emit(record):\n"
    "    print('" + _TEST_MARKER + "' + json.dumps(record, ensure_ascii=False, sort_keys=True))\n"
    "    sys.exit(0)\n"
    "loader = unittest.TestLoader()\n"
    "try:\n"
    "    suite = loader.loadTestsFromName('tests.' + module)\n"
    "except BaseException as exc:\n"
    "    emit({'trusted': False, 'nonce': nonce, 'reason': 'import_failed:' + type(exc).__name__})\n"
    "stream = io.StringIO()\n"
    "result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)\n"
    "rows = []\n"
    "for test, _detail in list(getattr(result, 'failures', [])):\n"
    "    rows.append({'id': str(test.id()), 'status': 'failed'})\n"
    "for test, _detail in list(getattr(result, 'errors', [])):\n"
    "    rows.append({'id': str(test.id()), 'status': 'errored'})\n"
    "for test, _detail in list(getattr(result, 'skipped', [])):\n"
    "    rows.append({'id': str(test.id()), 'status': 'skipped'})\n"
    "emit({\n"
    "    'trusted': True,\n"
    "    'nonce': nonce,\n"
    "    'ran': int(result.testsRun),\n"
    "    'failures': len(list(getattr(result, 'failures', []))),\n"
    "    'errors': len(list(getattr(result, 'errors', []))),\n"
    "    'rows': rows,\n"
    "})\n"
)


def _run_trusted_test_suite(root: Path, module_name: str) -> dict[str, Any] | None:
    """在给定参考根上运行交付测试，并由可信驱动给出结构化结果。"""

    script = root / ".probe_tests.py"
    script.write_text(_TEST_RUNNER_SOURCE, encoding="utf-8")
    # 交付测试的子进程输出会混入同一 stdout，因此只认可信驱动打印的唯一标记行，
    # 并核对可信侧生成的一次性 nonce。
    nonce = uuid.uuid4().hex
    try:
        result = sandbox.run(
            [sys.executable, ".probe_tests.py", nonce, module_name],
            cwd=root,
            guard_dir=_guard_for(root),
            trusted_fixture=_trusted_fixture_for(root),
            timeout=45,
            base_env={"PYTHONUTF8": "1"},
        )
    finally:
        script.unlink(missing_ok=True)
    rows = [
        line.strip() for line in result.stdout.splitlines()
        if line.strip().startswith(_TEST_MARKER)
    ]
    if len(rows) != 1:
        return None
    try:
        record = json.loads(rows[0][len(_TEST_MARKER):])
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("trusted") is not True:
        return None
    if record.get("nonce") != nonce:
        return None
    return record


def _mutate_borrow_guard(root: Path) -> bool:
    app_path = root / "lendreg" / "app.py"
    try:
        source = app_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(app_path))
    except (OSError, SyntaxError, UnicodeError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        left = node.test.left
        is_status_check = (
            isinstance(left, ast.Subscript)
            and isinstance(left.value, ast.Name)
            and left.value.id == "item"
            and isinstance(left.slice, ast.Constant)
            and left.slice.value == "status"
        )
        if not is_status_check:
            continue
        node.test = ast.Constant(value=False)
        app_path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")
        return True
    return False


def _mutate_restart_persistence(root: Path) -> bool:
    storage_path = root / "lendreg" / "storage.py"
    try:
        source = storage_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(storage_path))
    except (OSError, SyntaxError, UnicodeError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "save_data":
            node.body = [ast.Return(value=None)]
            storage_path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")
            return True
    return False


_FAULT_MARKER = "__TRUSTED_FAULT__"

# 可信侧业务故障确认探针：一次进程内完成该故障所需的全部业务调用，避免多次
# 子进程启动造成的时序差异，同时保证正常参考与错误参考使用同一段可信代码。
_FAULT_PROBE_SOURCE = (
    "import json\n"
    "import os\n"
    "import subprocess\n"
    "import sys\n"
    "kind = sys.argv[1]\n"
    "def cli(*args):\n"
    "    done = subprocess.run([sys.executable, '-m', 'lendreg', *args], cwd='.', capture_output=True, text=True)\n"
    "    return done.returncode, done.stdout\n"
    "record = {'kind': kind}\n"
    "if kind == 'repeat_lend':\n"
    "    cli('add', 'fx-1', '故障确认物')\n"
    "    record['first'], _out = cli('lend', 'fx-1', '借用人A')\n"
    "    record['second'], _out = cli('lend', 'fx-1', '借用人B')\n"
    "elif kind == 'restart_loss':\n"
    "    record['added'], _out = cli('add', 'rp-1', '电磁炉')\n"
    "    record['listed'], out = cli('list')\n"
    "    record['has_item'] = '电磁炉' in out\n"
    "    record['store_exists'] = os.path.exists(os.path.join('data', 'store.json'))\n"
    "print('" + _FAULT_MARKER + "' + json.dumps(record, ensure_ascii=False, sort_keys=True))\n"
)


def _run_fault_probe(root: Path, kind: str) -> dict[str, Any] | None:
    script = root / ".probe_fault.py"
    script.write_text(_FAULT_PROBE_SOURCE, encoding="utf-8")
    try:
        result = sandbox.run(
            [sys.executable, ".probe_fault.py", kind],
            cwd=root,
            guard_dir=_guard_for(root),
            trusted_fixture=_trusted_fixture_for(root),
            timeout=45,
            base_env={"PYTHONUTF8": "1"},
        )
    finally:
        script.unlink(missing_ok=True)
    rows = [
        line.strip() for line in result.stdout.splitlines()
        if line.strip().startswith(_FAULT_MARKER)
    ]
    if len(rows) != 1:
        return None
    try:
        record = json.loads(rows[0][len(_FAULT_MARKER):])
    except (TypeError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("kind") == kind else None


_FAULT_CONFIRMATION_CACHE: dict[tuple[str, str], tuple[bool, list[str]]] = {}


def _work_snapshot_manifest(root: Path) -> dict[str, str]:
    """对候选可写区做逐文件 SHA-256 清单（r7/X02：初始快照要可核对）。"""

    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            manifest[path.relative_to(root).as_posix()] = sha256_bytes(path.read_bytes())
    return manifest


def _work_snapshot_problems(snapshot_root: Path, work_root: Path) -> list[str]:
    """核对重建后的候选可写区与初始快照逐文件一致（r7/X02）。

    每次参考运行前从同一初始快照完整重建候选可写区（不只清一个业务目录），
    重建后核对清单；不一致说明参考起点不等价，验收不可用。
    """

    expected = _work_snapshot_manifest(snapshot_root)
    actual = _work_snapshot_manifest(work_root)
    if expected == actual:
        return []
    problems: list[str] = []
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(name for name in set(expected) & set(actual) if expected[name] != actual[name])
    if missing:
        problems.append("快照重建后缺失文件：" + ",".join(missing[:5]))
    if extra:
        problems.append("快照重建后多出文件：" + ",".join(extra[:5]))
    if changed:
        problems.append("快照重建后内容不一致：" + ",".join(changed[:5]))
    return problems


def _confirm_repeat_lend_fault(normal: Mapping[str, Any] | None, mutant: Mapping[str, Any] | None) -> tuple[bool, list[str]]:
    """可信侧确认：错误参考确实体现“重复借出被错误允许”（不是语法/启动/导入错误）。"""

    def read(record: Mapping[str, Any] | None) -> tuple[int, int]:
        if not isinstance(record, dict):
            return -1, -1
        return int(record.get("first", -1)), int(record.get("second", -1))

    normal_first, normal_second = read(normal)
    mutant_first, mutant_second = read(mutant)
    evidence = [
        "正常参考：首次借出退出码 " + str(normal_first) + "，重复借出退出码 " + str(normal_second),
        "错误参考：首次借出退出码 " + str(mutant_first) + "，重复借出退出码 " + str(mutant_second),
    ]
    ok = normal_first == 0 and normal_second != 0 and mutant_first == 0 and mutant_second == 0
    return ok, evidence


def _confirm_restart_loss_fault(normal: Mapping[str, Any] | None, mutant: Mapping[str, Any] | None) -> tuple[bool, list[str]]:
    """可信侧确认：错误参考确实体现“重启后数据丢失”（不是语法/启动/导入错误）。"""

    def read(record: Mapping[str, Any] | None) -> tuple[int, int, str, bool]:
        if not isinstance(record, dict):
            return -1, -1, "", False
        return (
            int(record.get("added", -1)),
            int(record.get("listed", -1)),
            "电磁炉" if record.get("has_item") else "",
            bool(record.get("store_exists")),
        )

    normal_add, normal_list, normal_out, normal_store = read(normal)
    mutant_add, mutant_list, mutant_out, mutant_store = read(mutant)
    evidence = [
        "正常参考：新增退出码 " + str(normal_add) + "，list 退出码 " + str(normal_list)
        + "，含电磁炉=" + str("电磁炉" in normal_out) + "，数据文件存在=" + str(normal_store),
        "错误参考：新增退出码 " + str(mutant_add) + "，list 退出码 " + str(mutant_list)
        + "，含电磁炉=" + str("电磁炉" in mutant_out) + "，数据文件存在=" + str(mutant_store),
    ]
    ok = (
        normal_add == 0
        and normal_list == 0
        and "电磁炉" in normal_out
        and normal_store
        and mutant_add == 0
        and mutant_list == 0
        and "电磁炉" not in mutant_out
        and not mutant_store
    )
    return ok, evidence


def _verify_python_test_artifact(project: Path, relative_path: str, module_name: str) -> tuple[bool, list[str]]:
    """交付测试验收：正常参考通过，且错误参考因目标业务断言失败。

    静态结构检查保留，但归因只认可信侧结构化运行结果：错误参考必须出现
    failure（断言失败）且没有 error（导入/启动/语法类错误），测试数量与
    正常参考一致；正常与错误参考使用等价调用条件（相同目录名、相同命令与
    环境），路径标签或目录名差异不能代替业务原因。r7/X02：每次参考运行前
    从初始快照完整重建候选可写区并逐文件核对，另加 normal→normal 顺序对照，
    顺序依赖或状态残留一律 verification unavailable。
    """

    path = project / relative_path
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        return False, ["必需测试文件缺失或无法编译：" + type(exc).__name__]
    test_methods = [
        method
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(
            (base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")) == "TestCase"
            for base in node.bases
        )
        for method in node.body
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        and method.name.startswith("test_")
    ]
    def reachable_calls(method: ast.AST) -> list[ast.Call]:
        calls: list[ast.Call] = []

        def visit(node: ast.AST) -> None:
            if isinstance(node, ast.If) and isinstance(node.test, ast.Constant) and node.test.value is False:
                for child in node.orelse:
                    visit(child)
                return
            if isinstance(node, ast.Call):
                calls.append(node)
            for child in ast.iter_child_nodes(node):
                visit(child)

        visit(method)
        return calls

    reachable_by_method = {method: reachable_calls(method) for method in test_methods}
    meaningful_test = any(
        any(
            isinstance(call.func, ast.Attribute)
            and call.func.attr.startswith("assert")
            for call in calls
        )
        and any(
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "run"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "subprocess"
            for call in calls
        )
        for calls in reachable_by_method.values()
    )
    calls = [call for method_calls in reachable_by_method.values() for call in method_calls]
    called_attributes = [
        call.func.attr
        for call in calls
        if isinstance(call.func, ast.Attribute)
    ]
    attribute_names: list[str] = []
    for method in test_methods:
        def collect_attributes(node: ast.AST) -> None:
            if isinstance(node, ast.If) and isinstance(node.test, ast.Constant) and node.test.value is False:
                for child in node.orelse:
                    collect_attributes(child)
                return
            if isinstance(node, ast.Attribute):
                attribute_names.append(node.attr)
            for child in ast.iter_child_nodes(node):
                collect_attributes(child)
        collect_attributes(method)
    constant_text = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    if module_name == "test_borrow_guard":
        task_contract = (
            called_attributes.count("run") >= 3
            and called_attributes.count("read_bytes") >= 2
            and "assertNotEqual" in called_attributes
            and "assertEqual" in called_attributes
        )
    elif module_name == "test_restart_persistence":
        task_contract = (
            called_attributes.count("run") >= 3
            and called_attributes.count("assertIn") >= 2
            and {"电磁炉", "借用人庚", "stdout"}.issubset(constant_text | set(attribute_names))
        )
    else:
        task_contract = False
    if not meaningful_test or not task_contract:
        return False, ["必需测试文件必须含有 unittest 用例、断言和独立子进程运行。"]
    # r7/X02：正常与错误参考不再共用"只清业务目录"的工作根。初始状态整目录
    # 快照留档；每次参考运行前从快照完整重建候选可写区并逐文件核对（候选测试
    # 可能在 tests/、根目录等任意位置留状态），保证起点等价；另加 normal→normal
    # 顺序对照，"第二次总失败"不得被认作检出。不对用户原目录做任何清理。
    base_root = Path(tempfile.mkdtemp(prefix="opencoding-verify-"))
    snapshot_root = base_root / "snapshot"
    work_root = base_root / "work"
    try:
        work_root.mkdir()
        shutil.copytree(project / "lendreg", work_root / "lendreg")
        shutil.copytree(project / "tests", work_root / "tests")
        _set_trusted_fixture(work_root, _trusted_fixture_for(project))
        if module_name == "test_borrow_guard":
            fault_kind = "repeat_lend"
            fault_label = "重复借出被错误允许"
            reference_path = work_root / "lendreg" / "app.py"
            mutate = lambda: _mutate_borrow_guard(work_root)
            missing_note = "参考实现缺少可定位的重复借出业务分支。"
            compare = _confirm_repeat_lend_fault
        elif module_name == "test_restart_persistence":
            fault_kind = "restart_loss"
            fault_label = "重启后数据丢失"
            reference_path = work_root / "lendreg" / "storage.py"
            mutate = lambda: _mutate_restart_persistence(work_root)
            missing_note = "参考实现缺少 save_data 业务入口。"
            compare = _confirm_restart_loss_fault
        else:
            return False, ["未知交付测试类型。"]

        def restore_work_root() -> list[str]:
            """从初始快照完整重建候选可写区并核对（唯一变量由调用方控制）。"""

            shutil.rmtree(work_root, ignore_errors=True)
            shutil.copytree(snapshot_root, work_root)
            return _work_snapshot_problems(snapshot_root, work_root)

        shutil.copytree(work_root, snapshot_root)
        # 同一进程内对同一份参考实现字节只做一次可信故障确认（按字节摘要缓存）：
        # 故障是否成立只取决于参考实现字节与可信探针，与环境时序无关。
        cache_key = (module_name, sha256_bytes(reference_path.read_bytes()))
        cached = _FAULT_CONFIRMATION_CACHE.get(cache_key)
        if cached is None:
            if restore_work_root():
                return False, ["候选可写区初始快照重建核对失败，验收不可用（verification unavailable）。"]
            normal_probe = _run_fault_probe(work_root, fault_kind)
            if restore_work_root():
                return False, ["候选可写区初始快照重建核对失败，验收不可用（verification unavailable）。"]
            if not mutate():
                return False, [missing_note]
            fault_probe = _run_fault_probe(work_root, fault_kind)
            cached = compare(normal_probe, fault_probe)
            _FAULT_CONFIRMATION_CACHE[cache_key] = cached
        fault_ok, fault_evidence = cached
        evidence = ["可信侧业务故障确认（" + fault_label + "）：" + ("成立" if fault_ok else "不成立")]
        evidence.extend(fault_evidence)
        if not fault_ok:
            return False, evidence + ["错误参考没有体现指定业务故障，不能据此判定测试检出能力。"]
        if restore_work_root():
            return False, evidence + ["候选可写区初始快照重建核对失败，验收不可用（verification unavailable）。"]
        normal = _run_trusted_test_suite(work_root, module_name)
        if restore_work_root():
            return False, evidence + ["候选可写区初始快照重建核对失败，验收不可用（verification unavailable）。"]
        normal_repeat = _run_trusted_test_suite(work_root, module_name)
        if restore_work_root():
            return False, evidence + ["候选可写区初始快照重建核对失败，验收不可用（verification unavailable）。"]
        if not mutate():
            return False, evidence + [missing_note]
        mutant = _run_trusted_test_suite(work_root, module_name)
        if normal is None or normal_repeat is None:
            return False, evidence + ["正常参考未取得可信侧结构化测试结果。"]
        if mutant is None:
            return False, evidence + ["错误参考未取得可信侧结构化测试结果。"]
        sequence_consistent = (
            normal["ran"] == normal_repeat["ran"]
            and normal["failures"] == normal_repeat["failures"]
            and normal["errors"] == normal_repeat["errors"]
        )
        evidence.append(
            "顺序对照（normal→normal）：重复正常参考运行"
            + ("一致" if sequence_consistent else "不一致")
            + "（第二次：" + str(normal_repeat["ran"]) + " 项/失败 " + str(normal_repeat["failures"])
            + "/错误 " + str(normal_repeat["errors"]) + "）"
        )
        if not sequence_consistent:
            return False, evidence + [
                "候选测试在等价起点上重复运行结果不一致，存在顺序依赖或状态残留，"
                "验收不可用（verification unavailable）。",
            ]
        normal_ok = normal["ran"] > 0 and normal["failures"] == 0 and normal["errors"] == 0
        failed_ids = [row["id"] for row in mutant["rows"] if row.get("status") == "failed"]
        fault_detected = (
            mutant["ran"] == normal["ran"]
            and mutant["ran"] > 0
            and mutant["failures"] >= 1
            and mutant["errors"] == 0
            and bool(failed_ids)
        )
        evidence.extend([
            "正常参考：运行 " + str(normal["ran"]) + " 项，失败 " + str(normal["failures"])
            + "，错误 " + str(normal["errors"]),
            "错误参考：运行 " + str(mutant["ran"]) + " 项，失败 " + str(mutant["failures"])
            + "，错误 " + str(mutant["errors"]),
            "错误参考失败用例：" + ",".join(failed_ids),
            "受控错误实现：" + ("被测试拒绝" if fault_detected else "未被检测"),
        ])
        return normal_ok and fault_detected, evidence
    finally:
        _TRUSTED_FIXTURES.pop(str(work_root), None)
        shutil.rmtree(base_root, ignore_errors=True)


LENDREG_VERIFIERS: dict[str, Verifying] = {
    "d01-models": _verify_d01_models,
    "d02-storage": _verify_d02_storage,
    "d03-add": _verify_d03_add,
    "d04-lend": _verify_d04_lend,
    "d05-borrow-guard": _verify_d05_guard,
    "d06-return": _verify_d06_return,
    "d07-list": _verify_d07_list,
    "d08-chinese-entry": _verify_d08_chinese,
    "d09-restart": _verify_d09_restart,
    "d10-full-flow": _verify_d10_full_flow,
}


def _task(task_id: str, title: str, objective: str, depends_on: list[str], outputs: list[str], prompt: str, acceptance: list[str]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "title": title,
        "objective": objective,
        "depends_on": depends_on,
        "outputs": outputs,
        "acceptance": acceptance,
        "prompt": prompt,
    }


LENDREG_SCENARIO: dict[str, Any] = {
    "name": "单机借还登记（合成示例）",
    "goal": "实现一个中文命令行的单机借还登记工具，合成物品与借用人，无联网、无登录、无支付。",
    "tasks": [
        _task(
            "d01-models", "数据对象与字段约束",
            "产出 lendreg 包的数据模型：物品与借还记录具有稳定 ID，非法字段输入被拒绝。",
            [], ["lendreg/__init__.py", "lendreg/models.py"],
            "创建 Python 包 lendreg。models.py 提供 new_item(item_id, name) 与 new_loan(loan_id, item_id, borrower)：物品含 id/name/status（初始 available），记录含 id/item_id/borrower/borrowed_at/closed（初始 False）。空 ID、超过 64 字符的 ID、空名称必须抛 ValueError。只用标准库。",
            ["合法对象可创建；非法字段抛 ValueError。"],
        ),
        _task(
            "d02-storage", "本地保存层",
            "数据可保存、重读；失败不破坏已有合成记录。",
            ["d01-models"], ["lendreg/storage.py"],
            "storage.py 提供 save_data(path, data) 与 load_data(path)：JSON 保存到 data/store.json（目录自动创建，原子写：临时文件+替换），load_data 在文件缺失时返回 {'items': {}, 'loans': []}；JSON 损坏时抛 ValueError 且不改动原文件。",
            ["保存后重读数据一致；文件缺失返回空表。"],
        ),
        _task(
            "d03-add", "新建物品",
            "能新增并查到物品；重复标识被正确处理。",
            ["d02-storage"], ["lendreg/app.py", "lendreg/__main__.py"],
            "app.py 提供主流程：命令 `python -m lendreg add <物品ID> <名称>` 新增物品并保存；重复 ID 打印中文错误并无退出码 0。__main__.py 调用 app.main()。错误与成功提示都用中文。",
            ["新增后可查到；重复 ID 失败且不破坏数据。"],
        ),
        _task(
            "d04-lend", "办理借出",
            "可用物品变成已借出，记录关联关系。",
            ["d03-add"], ["lendreg/app.py"],
            "扩展 app.py：命令 `python -m lendreg lend <物品ID> <借用人>`：物品存在且 available 时，状态改 borrowed，追加一条未闭合借还记录（ID 用 uuid4 hex 前 8 位），保存。物品不存在或已借出时中文错误、非零退出。",
            ["借出后物品状态为 borrowed，记录关联物品与借用人。"],
        ),
        _task(
            "d05-borrow-guard", "阻止重复借出（黑盒测试）",
            "对已借出物品再次借出失败，状态不被破坏。",
            ["d04-lend"], ["tests/test_borrow_guard.py"],
            "创建 tests/test_borrow_guard.py：用 subprocess 以独立进程运行 `python -m lendreg`：先 add 再 lend 成功；再次 lend 同一物品返回非零，且 data/store.json 内容在两次尝试之间字节级不变。",
            ["重复借出被拒绝；数据文件未被破坏。"],
        ),
        _task(
            "d06-return", "办理归还",
            "借出记录闭合、物品可重新借出。",
            ["d04-lend"], ["lendreg/app.py"],
            "扩展 app.py：命令 `python -m lendreg return <物品ID>`：对已借出物品，把未闭合记录标记 closed=True、闭合时间写入 returned_at，物品状态改回 available，保存。无未闭合记录时中文错误、非零退出。",
            ["归还后记录闭合、物品可再次借出。"],
        ),
        _task(
            "d07-list", "查看记录",
            "查询可区分可用、借出和历史记录。",
            ["d06-return"], ["lendreg/app.py"],
            "扩展 app.py：命令 `python -m lendreg list` 中文输出三段：可用物品、借出中（含借用人）、历史借还记录（含闭合标记）。",
            ["可用、借出、历史三类信息都能区分。"],
        ),
        _task(
            "d08-chinese-entry", "中文命令入口",
            "用户用已说明的中文步骤完成操作，错误提示可理解。",
            ["d07-list"], ["lendreg/messages.py", "lendreg/app.py"],
            "创建 messages.py 集中全部中文提示文案（如 物品不存在、物品已借出、参数不足、操作成功等），app.py 全部错误提示改用它；对不存在的物品执行 lend 时，stderr 为中文提示、非零退出，不得出现 Traceback 或英文 Usage。",
            ["错误提示为中文且可理解，无英文裸露。"],
        ),
        _task(
            "d09-restart", "重启后的持久化（黑盒测试）",
            "退出再启动仍可读取上次合成数据。",
            ["d07-list"], ["tests/test_restart_persistence.py"],
            "创建 tests/test_restart_persistence.py：用 subprocess 两次独立进程：第一次 add+lend，第二次直接 list，断言第二次输出包含该物品与借用人（证明跨进程持久化）。",
            ["重启后数据保留。"],
        ),
        _task(
            "d10-full-flow", "整体验证及受控恢复",
            "完整流程测试；恢复本批代码变更不丢失其他文件。",
            ["d05-borrow-guard", "d08-chinese-entry", "d09-restart"], ["scripts/full_flow.py", "RECOVERY.md"],
            "创建 scripts/full_flow.py：串行执行 新增两件物品→借出其一→重复借出（应失败）→归还→list，任一步失败即非零退出并中文说明。创建 RECOVERY.md：列出本工具全部源码文件清单与“回滚=删除/还原这些文件、保留 data/ 业务数据”的说明。",
            ["完整流程通过；回滚清单明确区分代码与业务数据。"],
        ),
    ],
}


def validate_scenario(scenario: Mapping[str, Any]) -> None:
    if not isinstance(scenario, Mapping):
        raise AutorunError("scenario_invalid", "任务场景必须是对象")
    if not isinstance(scenario.get("tasks"), list) or not scenario["tasks"]:
        raise AutorunError("scenario_invalid", "任务场景必须包含任务列表")
    seen: set[str] = set()
    for task in scenario["tasks"]:
        if not isinstance(task, Mapping) or set(task) != {"task_id", "title", "objective", "depends_on", "outputs", "acceptance", "prompt"}:
            raise AutorunError("scenario_invalid", "任务字段不完整")
        task_id = task["task_id"]
        if not isinstance(task_id, str) or not task_id or task_id in seen:
            raise AutorunError("scenario_invalid", "任务编号无效或重复")
        seen.add(task_id)
        if not all(dep in seen for dep in task["depends_on"]):
            raise AutorunError("scenario_invalid", "任务依赖必须指向已定义的前序任务")
    verifiers = scenario.get("verifiers", {})
    missing = [task["task_id"] for task in scenario["tasks"] if task["task_id"] not in verifiers]
    if missing:
        raise AutorunError("scenario_invalid", "缺少验收器：" + ",".join(missing))


def _safe_run_segment(run_id: str) -> str:
    safe = run_id.replace("\\", "/").split("/")[-1]
    if not safe or safe in {".", ".."}:
        raise AutorunError("run_id_invalid", "运行编号无效")
    return safe


def _ledger_path(root: Path, run_id: str) -> Path:
    return root / ".opencoding" / "autoruns" / f"{_safe_run_segment(run_id)}.json"


def _load_ledger(root: Path, run_id: str) -> dict[str, Any]:
    path = _ledger_path(root, run_id)
    if not path.exists():
        return {"schema_version": AUTORUN_SCHEMA_VERSION, "run_id": run_id, "started_at": _now(), "tasks": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            ledger = json.load(handle)
    except (OSError, ValueError) as exc:  # json.JSONDecodeError 是 ValueError 的子类
        raise AutorunError("ledger_invalid", "运行账本不可读或格式损坏") from exc
    if not isinstance(ledger, dict) or ledger.get("schema_version") != AUTORUN_SCHEMA_VERSION or ledger.get("run_id") != run_id:
        raise AutorunError("ledger_invalid", "运行账本损坏或版本不受支持")
    return ledger


def _save_ledger(root: Path, ledger: Mapping[str, Any]) -> None:
    path = _ledger_path(root, ledger["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(ledger, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_committed_binding(
    project: Path, run_id: str, granted_id: str | None, recovery: Mapping[str, Any],
    receipt: Mapping[str, Any], recovery_attempt: int, transaction_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """F10/r8-Z02：从受控绑定目录安全读取产生本次提交的权威绑定记录。

    r8 之前的新增绑定读取直接 glob+read_text：不验证文件/目录身份与大小，
    不核对 run/grant/schema，files 经 set 去重会掩盖重复。本读取收口为：
    1. 绑定目录经受控路径解析定位（逐段拒绝链接、重解析点、大小写别名），
       不依据可变路径扩展到其他目录；状态查看全程零写入。
    2. 只接受规范文件名（`{task_id}-{attempt}.binding.json`）的常规文件：
       拒绝符号链接、重解析点、硬链接与超大文件；读取/结构故障返回稳定
       结构化错误，不静默跳过。
    3. 核对唯一受支持 schema 与 kind、run/task/grant/attempt、事务编号与
       已提交状态；计划摘要必须与同次事务回执一致；输入摘要集合必须与
       绑定文件清单一一对应。
    4. files 必须是完整、唯一、类型正确的条目集合；重复路径或重复条目
       明确拒绝，不用 set 去重掩盖。
    5. 同一次提交存在其他 committed 绑定记录（或同任务存在不可读/不安全
       绑定文件）时保守阻断；不按排序取第一份可用 JSON。无关文件与其他
       任务的绑定不受全局拒绝。
    """

    task_id = recovery.get("task_id")
    if not isinstance(task_id, str) or not task_id or task_id in {".", ".."} \
            or "/" in task_id or "\\" in task_id:
        return None, ["回滚证据缺少有效任务编号，无法定位提交绑定"]
    canonical_name = f"{task_id}-{recovery_attempt}.binding.json"
    try:
        binding_dir = transactions._secure_dir(
            project,
            (".opencoding", "dev-runs", _safe_run_segment(run_id), "commit-bindings"),
            create=False,
        )
    except (OSError, ValueError):
        return None, ["提交绑定目录缺失或路径身份不安全，无法核对提交绑定"]
    canonical_path = binding_dir / canonical_name
    if not canonical_path.exists():
        return None, ["回滚证据引用的事务没有对应的已提交绑定记录"]
    try:
        transactions._safe_evidence_file(canonical_path, binding_dir)
        if os.path.getsize(canonical_path) > _BINDING_MAX_BYTES:
            raise ValueError("commit binding file is oversized")
    except (OSError, ValueError):
        return None, ["提交绑定记录文件身份不安全（链接/重解析点/硬链接/超大），无法核对提交绑定"]
    try:
        binding = json.loads(canonical_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None, ["提交绑定记录不可读或不是有效 JSON，无法核对提交绑定"]
    if not isinstance(binding, dict):
        return None, ["提交绑定记录不是有效对象，无法核对提交绑定"]
    if binding.get("schema_version") != AUTORUN_SCHEMA_VERSION or binding.get("kind") != "binding":
        return None, ["提交绑定记录的 schema 或类型不受支持，无法核对提交绑定"]
    if binding.get("run_id") != run_id:
        return None, ["提交绑定的运行编号与本次运行不一致"]
    if binding.get("task_id") != task_id:
        return None, ["提交绑定的任务编号与回滚证据不一致"]
    if type(binding.get("attempt")) is not int or binding["attempt"] != recovery_attempt:
        return None, ["回滚证据的尝试号不是产生本次提交的真实尝试"]
    if not isinstance(binding.get("grant_id"), str) or not binding["grant_id"]:
        return None, ["提交绑定缺少批次授权编号"]
    if granted_id is not None and binding["grant_id"] != granted_id:
        return None, ["提交绑定的批次授权与本次运行绑定不一致"]
    if binding.get("state") != "committed":
        return None, ["绑定记录不是已提交状态，无法核验可交付"]
    if binding.get("transaction_id") != transaction_id:
        return None, ["提交绑定的事务编号与回滚证据不一致"]
    plan_digest = binding.get("plan_digest")
    if not isinstance(plan_digest, str) or not _HEX64.fullmatch(plan_digest) \
            or plan_digest != receipt.get("plan_digest"):
        return None, ["提交绑定的计划摘要与同次事务回执不一致"]
    binding_files = binding.get("files")
    if not isinstance(binding_files, list) or not binding_files:
        return None, ["提交绑定文件清单缺失或为空"]
    seen_paths: set[str] = set()
    seen_entries: set[tuple[str, str, Any, Any]] = set()
    for item in binding_files:
        if not isinstance(item, dict) or set(item) != {"path", "operation", "before_sha256", "after_sha256"}:
            return None, ["提交绑定文件条目字段不完整"]
        path = item.get("path")
        operation = item.get("operation")
        before = item.get("before_sha256")
        after = item.get("after_sha256")
        # r10/AA01：先明确 operation 是字符串再做集合成员判断——列表/对象等
        # 不可哈希类型直接统一归为"条目无效"，不依赖底层 TypeError 泄露内部文字。
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(operation, str)
            or operation not in {"create", "update"}
        ):
            return None, ["提交绑定文件条目无效：" + sanitize_text(str(path))[:80]]
        if before is not None and (not isinstance(before, str) or not _HEX64.fullmatch(before)):
            return None, ["提交绑定文件条目前像摘要无效"]
        if not isinstance(after, str) or not _HEX64.fullmatch(after):
            return None, ["提交绑定文件条目后像摘要无效"]
        if path in seen_paths:
            return None, ["提交绑定文件清单含有重复路径，不能以集合去重掩盖"]
        entry = (path, operation, before, after)
        if entry in seen_entries:
            return None, ["提交绑定文件清单含有重复条目，不能以集合去重掩盖"]
        seen_paths.add(path)
        seen_entries.add(entry)
    input_digests = binding.get("input_digests")
    if not isinstance(input_digests, dict) or set(input_digests) != seen_paths:
        return None, ["提交绑定的输入摘要集合与文件清单不一致"]
    for item in binding_files:
        if input_digests.get(item["path"]) != item["after_sha256"]:
            return None, ["提交绑定的输入摘要与文件后像不一致"]
    try:
        candidates = sorted(os.scandir(binding_dir), key=lambda item: item.name)
    except OSError:
        return None, ["提交绑定目录不可读，无法核对提交绑定"]
    for candidate in candidates:
        if not candidate.name.endswith(".binding.json") or candidate.name == canonical_name:
            continue
        other_path = binding_dir / candidate.name
        same_task = candidate.name.startswith(task_id + "-")
        # r10/AA02：旁支在读取内容之前复用与主记录完全一致的身份检查——受控
        # 目录内常规文件、无符号链接/重解析点/硬链接（st_nlink≤1）、大小受限。
        # 不得先读 JSON 再决定是否安全，也不得借内容为空或 state 不同提前跳过
        # 来源验证；与本任务相关的旁支身份不安全即结构化保守阻断，无关旁支
        # 与合法历史 prepared/failed 记录继续允许。
        try:
            transactions._safe_evidence_file(other_path, binding_dir)
            if os.path.getsize(other_path) > _BINDING_MAX_BYTES:
                raise ValueError("commit binding file is oversized")
        except (OSError, ValueError):
            if same_task:
                return None, ["同任务存在身份不安全或不可读的其他绑定记录，保守阻断"]
            continue
        try:
            other = json.loads(other_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            if same_task:
                return None, ["同任务存在不可读的其他绑定记录，保守阻断"]
            continue
        if (
            isinstance(other, dict)
            and other.get("kind") == "binding"
            and other.get("task_id") == task_id
            and other.get("state") == "committed"
            and other.get("transaction_id") == transaction_id
        ):
            return None, ["同一次提交存在多份 committed 绑定记录，保守阻断"]
    return binding, []


def _authoritative_recovery_state(
    project: Path, recovery: Mapping[str, Any], granted_id: str | None,
    *, task_record: Mapping[str, Any] | None = None, run_id: str | None = None,
) -> tuple[str | None, list[str]]:
    """F10/X04：从权威事务回执重建一条回滚证据对应的实际状态。

    返回 `(state, notes)`；回执缺失、路径或后像摘要与事务清单不一致时返回
    `None`，调用方必须按"状态不可核验"阻断，不能显示可交付。
    r7/X04：核验覆盖——①receipt_path 指向该事务编号的规范回执文件；②attempt
    能在账本该任务的尝试历史中找到（账本可用时）；③回滚证据文件集合与事务
    清单**双向相等**且无重复（漏报与越界多报都拒绝）；④逐条目核对前像摘要；
    ⑤操作语义与回执 before_exists 推导一致（create/update）。
    r8/Y03：⑥恢复索引必须绑定**产生本次提交的真实尝试**——由提交绑定记录
    （state=committed 且事务编号一致）回溯核对尝试号与文件集合；账本历史中
    存在该数字不等于该数字属于这一次提交（失败尝试/复验事件都不算）。
    """

    notes: list[str] = []
    transaction_id = recovery.get("transaction_id")
    if not isinstance(transaction_id, str) or not transaction_id:
        # 没有真实事务编号的待恢复记录：按未成功提交保守处理，不显示可交付。
        return "blocked", ["回滚证据缺少事务编号，无法核验真实提交"]
    if granted_id and str(recovery.get("grant_id") or "") != granted_id:
        return None, ["回滚证据的批次授权与本次运行绑定不一致"]
    # r7/X04：receipt_path 必须与规范事务回执路径一致，防止证据指向别处的回执。
    receipt_path = recovery.get("receipt_path")
    canonical_receipt = Path(project, ".opencoding", "transactions", transaction_id, "receipt.json")
    if (
        not isinstance(receipt_path, str)
        or not receipt_path
        or os.path.normcase(str(Path(receipt_path).resolve())) != os.path.normcase(str(canonical_receipt.resolve()))
    ):
        return None, ["回滚证据的 receipt_path 与规范事务回执路径不一致"]
    # r7/X04：attempt 与运行账本该任务的尝试历史绑定；账本缺失本身即不可核验。
    if task_record is None or not isinstance(task_record, Mapping):
        return None, ["回滚证据引用的任务在运行账本中没有记录"]
    attempt_ids = {
        int(item.get("attempt"))
        for item in (task_record.get("attempts") or [])
        if isinstance(item, dict) and isinstance(item.get("attempt"), int)
    }
    recovery_attempt = recovery.get("attempt")
    if type(recovery_attempt) is not int or recovery_attempt not in attempt_ids:
        return None, ["回滚证据的尝试号与运行账本的尝试历史不一致"]
    try:
        receipt = transactions.read_receipt_state(project, transaction_id)
    except (OSError, TypeError, ValueError):
        return None, ["回滚证据指向的事务回执缺失或不可核验"]
    entries = {item["path"]: item for item in receipt["entries"]}
    recovery_files = recovery.get("files", [])
    recovery_paths = [str(item.get("path")) for item in recovery_files]
    if len(set(recovery_paths)) != len(recovery_paths):
        return None, ["回滚证据文件清单含有重复路径"]
    # r7/X04：完整唯一文件集合——与事务清单双向相等，少报或多报都不可核验。
    if set(recovery_paths) != set(entries):
        return None, ["回滚证据文件集合与事务清单不一致"]
    for item in recovery_files:
        if not isinstance(item, dict):
            return None, ["回滚证据文件条目无效"]
        entry = entries[str(item.get("path"))]
        if entry["after_sha256"] != item.get("after_sha256"):
            return None, ["回滚证据后像摘要与事务清单不一致"]
        # r7/X04：前像摘要逐条核对（create 条目的回执前像为空）。
        if entry["before_sha256"] != item.get("before_sha256"):
            return None, ["回滚证据前像摘要与事务清单不一致"]
        # r7/X04：操作语义与回执 before_exists 推导一致。
        expected_operation = "update" if entry["before_exists"] else "create"
        if item.get("operation") != expected_operation:
            return None, ["回滚证据操作语义与事务清单不一致"]
    # r8/Y03：精确提交绑定——声称已提交的恢复索引，其 (事务, 尝试, 文件集合)
    # 必须命中产生本次提交的那条 committed 绑定记录，而不是任一历史成员
    # （失败尝试、复验事件或另一成功提交都不算）。声称非 committed 终态的
    # 记录（部分失败/已回滚/待恢复）不投影为可交付，走回执状态分支。
    if str(recovery.get("state")) == "committed":
        if not isinstance(run_id, str) or not run_id:
            return None, ["回滚证据缺少运行编号，无法核对提交绑定"]
        binding, binding_problems = _read_committed_binding(
            project, run_id, granted_id, recovery, receipt, recovery_attempt, transaction_id,
        )
        if binding is None:
            return None, binding_problems
        binding_files = binding["files"]
        recovery_entries = [
            (
                str(item.get("path")),
                str(item.get("operation")),
                item.get("before_sha256"),
                item.get("after_sha256"),
            )
            for item in recovery_files
            if isinstance(item, dict)
        ]
        binding_entries = [
            (
                str(item.get("path")),
                str(item.get("operation")),
                item.get("before_sha256"),
                item.get("after_sha256"),
            )
            for item in binding_files
            if isinstance(item, dict)
        ]
        if len(binding_entries) != len(recovery_entries) or Counter(binding_entries) != Counter(recovery_entries):
            return None, ["回滚证据文件集合与提交绑定记录不一致"]
    rollback_status = receipt.get("rollback_status")
    if rollback_status == "rolled_back":
        return "rolled_back", notes
    if rollback_status == "partial_failure":
        return "partial_failure", notes
    if rollback_status == "blocked":
        return "blocked", notes
    if receipt["status"] == "applied":
        index_state = str(recovery.get("state") or "")
        if index_state in {"rolled_back", "partial_failure", "needs_recovery"}:
            notes.append("恢复索引声称已回滚，但权威事务回执没有回滚事实")
            return "unknown", notes
        return "committed", notes
    if receipt["status"] in {"partial_failure", "failed"}:
        return "blocked", notes
    notes.append("权威事务回执状态不可判定")
    return "unknown", notes


def read_status_snapshot(root: str | Path, task_id: str | None = None) -> dict[str, Any]:
    """Read autonomous ledgers without following links or mutating project state.

    F10：任务状态从权威事务回执重建，而不是只看恢复索引里的字符串；索引与
    回执冲突、回执缺失或关联不一致时一律按不可核验处理，不显示可交付。
    """

    project = Path(root).resolve()
    metadata = project / ".opencoding"
    directory = metadata / "autoruns"
    if not metadata.exists() and not metadata.is_symlink():
        return {"runs": [], "tasks": [], "events": []}
    if metadata.is_symlink() or not metadata.is_dir():
        raise AutorunError("autorun_status_store_unsafe", "自主状态元数据目录不是可信目录")
    if not directory.exists() and not directory.is_symlink():
        return {"runs": [], "tasks": [], "events": []}
    if directory.is_symlink() or not directory.is_dir():
        raise AutorunError("autorun_status_store_unsafe", "自主状态目录不是可信目录")

    runs: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        if path.name.endswith(".cancel.json"):
            continue
        try:
            info = path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_nlink", 1) != 1
                or info.st_size > 8 * 1024 * 1024
                or path.resolve(strict=True).parent != directory.resolve(strict=True)
            ):
                raise AutorunError("autorun_status_store_unsafe", "自主状态账本身份不安全")
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except AutorunError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AutorunError("autorun_status_store_invalid", "自主状态账本不可读或格式损坏") from exc
        run_id = path.stem
        if (
            not isinstance(ledger, dict)
            or ledger.get("schema_version") != AUTORUN_SCHEMA_VERSION
            or ledger.get("run_id") != run_id
            or not isinstance(ledger.get("tasks"), dict)
            or any(not isinstance(key, str) or not isinstance(value, dict) for key, value in ledger["tasks"].items())
        ):
            raise AutorunError("autorun_status_store_invalid", "自主状态账本字段不完整或编号不匹配")
        try:
            canonical_json(ledger)
        except (TypeError, ValueError) as exc:
            raise AutorunError("autorun_status_store_invalid", "自主状态账本不是有效 JSON 数据") from exc

        run_task_records = ledger["tasks"]
        rollback_projection: dict[str, str] = {}
        recovery_records_by_task: dict[str, list[dict[str, Any]]] = {}
        recovery_dir = _recovery_dir(project, run_id)
        if recovery_dir.is_dir():
            for recovery_path in recovery_dir.glob("*.json"):
                try:
                    info = recovery_path.lstat()
                    if (
                        stat.S_ISLNK(info.st_mode)
                        or not stat.S_ISREG(info.st_mode)
                        or getattr(info, "st_nlink", 1) != 1
                        or info.st_size > 8 * 1024 * 1024
                        or recovery_path.resolve(strict=True).parent != recovery_dir.resolve(strict=True)
                    ):
                        raise AutorunError("autorun_status_store_unsafe", "回滚证据身份不安全")
                    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
                except AutorunError:
                    raise
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise AutorunError(
                        "autorun_status_store_invalid",
                        "回滚证据不可读或格式损坏，当前交付状态未知",
                    ) from exc
                if not isinstance(recovery, dict):
                    raise AutorunError(
                        "autorun_status_store_invalid",
                        "回滚证据不是对象，当前交付状态未知",
                    )
                if recovery.get("kind") == "binding":
                    raise AutorunError(
                        "autorun_status_store_invalid",
                        "回滚证据目录混入提交绑定记录",
                    )
                recovered_task = recovery.get("task_id")
                required_recovery = {
                    "schema_version", "run_id", "task_id", "attempt",
                    "grant_id", "transaction_id", "recorded_at", "seq",
                    "state", "files", "receipt_path",
                }
                if (
                    not required_recovery.issubset(set(recovery))
                    or recovery.get("schema_version") != AUTORUN_SCHEMA_VERSION
                    or recovery.get("run_id") != run_id
                    or not isinstance(recovered_task, str)
                    or type(recovery.get("attempt")) is not int
                    or recovery["attempt"] < 1
                    or not isinstance(recovery.get("transaction_id"), str)
                    or not recovery["transaction_id"]
                    or recovery.get("state") not in {"committed", "rolled_back", "partial_failure", "needs_recovery"}
                    or not isinstance(recovery.get("files"), list)
                    or not recovery["files"]
                    or any(
                        not isinstance(entry, dict)
                        or set(entry) != {"path", "operation", "before_sha256", "after_sha256"}
                        or not isinstance(entry.get("path"), str)
                        or not entry["path"]
                        for entry in recovery["files"]
                    )
                ):
                    raise AutorunError(
                        "autorun_status_store_invalid",
                        "回滚证据字段、关联或终态不完整，当前交付状态未知",
                    )
                recovery_records_by_task.setdefault(recovered_task, []).append(recovery)
                authoritative, _notes = _authoritative_recovery_state(
                    project, recovery,
                    ledger.get("grant_id") if isinstance(ledger.get("grant_id"), str) else None,
                    task_record=run_task_records.get(recovered_task) if isinstance(run_task_records.get(recovered_task), dict) else None,
                    run_id=run_id,
                )
                if authoritative is None:
                    raise AutorunError(
                        "autorun_status_store_invalid",
                        "回滚证据与权威事务回执不一致或不可核验，当前交付状态未知",
                    )
                if isinstance(recovered_task, str) and authoritative in {"rolled_back", "partial_failure", "blocked", "unknown"}:
                    rollback_projection[recovered_task] = authoritative
        for current_task_id, task_record in run_task_records.items():
            if not isinstance(task_record, dict):
                continue
            has_committed_attempt = any(
                isinstance(item, dict) and item.get("committed") is True
                for item in task_record.get("attempts", [])
                if isinstance(task_record.get("attempts"), list)
            )
            if (
                task_record.get("state") == "succeeded"
                and (has_committed_attempt or isinstance(task_record.get("file_hashes"), dict))
                and not recovery_records_by_task.get(current_task_id)
            ):
                raise AutorunError(
                    "autorun_status_store_invalid",
                    "成功任务缺少必要回滚证据，当前交付状态未知",
                )
        projected_states = {
            key: rollback_projection.get(key, str(value.get("state", "unknown")))
            for key, value in run_task_records.items()
            if isinstance(value, dict)
        }
        selected = {
            key: value for key, value in run_task_records.items()
            if isinstance(key, str) and isinstance(value, dict)
            and (task_id is None or key == task_id)
        }
        if task_id is not None and not selected:
            continue
        task_states = [projected_states.get(key, "unknown") for key in selected]
        if task_states and all(state == "rolled_back" for state in task_states):
            run_state = "rolled_back"
        elif task_states and all(state == "succeeded" for state in task_states):
            run_state = "succeeded"
        elif task_states and all(state == "cancelled" for state in task_states):
            run_state = "cancelled"
        elif any(state == "unknown" for state in task_states):
            # F10：必要事务关联不可核验时保守阻断，不再显示为运行中或可交付。
            run_state = "blocked"
        elif any(state == "running" for state in task_states):
            run_state = "running"
        elif any(state == "pending" for state in task_states):
            run_state = "running"
        elif any(state in {"partial_failure", "blocked"} for state in task_states):
            run_state = "failed"
        else:
            run_state = "failed"
        runs.append({
            "schema_version": AUTORUN_SCHEMA_VERSION,
            "kind": "autonomous",
            "run_id": run_id,
            "task_id": task_id,
            "attempt": max(
                [
                    int(item.get("attempt", 0))
                    for record in selected.values()
                    for item in record.get("requests", [])
                    if isinstance(item, dict) and type(item.get("attempt")) is int
                ] + [0]
            ),
            "started_at": ledger.get("started_at"),
            "finished_at": ledger.get("finished_at"),
            "status": run_state,
            "exit_code": None,
            "succeeded": sum(state == "succeeded" for state in task_states),
            "current_state": run_state,
            "current_deliverable": run_state == "succeeded",
            "total": len(selected),
        })
        for current_task_id, record in selected.items():
            if current_task_id not in run_task_records or not isinstance(record, dict):
                raise AutorunError("autorun_status_store_invalid", "自主状态任务记录无效")
            requests = record.get("requests", [])
            attempts = record.get("attempts", [])
            request_rows = [item for item in requests if isinstance(item, dict)] if isinstance(requests, list) else []
            attempt_rows = [item for item in attempts if isinstance(item, dict)] if isinstance(attempts, list) else []
            latest_attempt = max(
                [
                    int(item.get("attempt", 0))
                    for item in request_rows
                    if type(item.get("attempt")) is int
                ] + [0]
            )
            state = projected_states.get(current_task_id, str(record.get("state", "unknown")))
            tasks.append({
                "schema_version": AUTORUN_SCHEMA_VERSION,
                "kind": "autonomous",
                "run_id": run_id,
                "task_id": current_task_id,
                "state": "queued" if state == "pending" else (
                     state if state in {"running", "succeeded", "frozen", "cancelled", "rolled_back", "partial_failure", "blocked"} else "unknown"
                ),
                "attempt": latest_attempt,
                "max_attempts": MAX_REPAIR_ROUNDS + 1,
                "last_run_id": run_id,
                "last_error": sanitize_text(str(record.get("error_code") or ""))[:100],
            })
            for request in request_rows:
                events.append({
                    "kind": "autonomous",
                    "run_id": run_id,
                    "task_id": current_task_id,
                    "attempt": request.get("attempt"),
                    "event_type": "request",
                    "request_id": request.get("request_id"),
                    "stage": request.get("stage"),
                    "request_kind": request.get("kind"),
                    "at": request.get("dispatched_at") or request.get("at"),
                })
            for attempt_row in attempt_rows:
                events.append({
                    "kind": "autonomous",
                    "run_id": run_id,
                    "task_id": current_task_id,
                    "attempt": attempt_row.get("attempt"),
                    "event_type": str(attempt_row.get("kind") or "attempt"),
                    "request_id": attempt_row.get("request_id"),
                    "verification_ok": attempt_row.get("verification_ok"),
                    "error_code": sanitize_text(str(attempt_row.get("error_code") or ""))[:100],
                })
            if current_task_id in rollback_projection:
                events.append({
                    "kind": "autonomous",
                    "run_id": run_id,
                    "task_id": current_task_id,
                    "event_type": "rollback",
                    "stage": rollback_projection[current_task_id],
                    "at": record.get("rolled_back_at"),
                })
    events.sort(key=lambda item: (
        str(item.get("at") or ""),
        str(item.get("run_id") or ""),
        str(item.get("task_id") or ""),
        str(item.get("attempt") or ""),
        str(item.get("event_type") or ""),
    ))
    return {"runs": runs, "tasks": tasks, "events": events}


def query_saved_results(root: str | Path, run_id: str, *, adapter: Any | None = None) -> dict[str, Any]:
    """Verify saved results or query one existing request; never dispatch or apply a candidate."""

    project = Path(root).resolve()
    ledger = _load_ledger(project, run_id)
    if ledger.get("cancel_requested"):
        return {
            "schema_version": AUTORUN_SCHEMA_VERSION,
            "run_id": run_id,
            "status": "cancelled",
            "checked": [],
            "new_dispatches": 0,
            "budget_changed": False,
        }
    cancel_path = _cancel_control_path(project, run_id)
    if cancel_path.exists() or cancel_path.is_symlink():
        try:
            info = cancel_path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) != 1:
                raise ValueError("cancel_control_unsafe")
            cancel_control = json.loads(cancel_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return {
                "schema_version": AUTORUN_SCHEMA_VERSION,
                "run_id": run_id,
                "status": "invalid",
                "checked": [],
                "new_dispatches": 0,
                "budget_changed": False,
            }
        if isinstance(cancel_control, dict) and cancel_control.get("cancel_requested") is True:
            return {
                "schema_version": AUTORUN_SCHEMA_VERSION,
                "run_id": run_id,
                "status": "cancelled",
                "checked": [],
                "new_dispatches": 0,
                "budget_changed": False,
            }

    tasks_by_id = {item["task_id"]: item for item in LENDREG_SCENARIO["tasks"]}
    resolver = getattr(adapter, "get_result", None)
    grant_id = ledger.get("grant_id")
    if callable(resolver) and isinstance(grant_id, str):
        try:
            grant = grants.load_grant(project, grant_id)
            active, _reason = grants.grant_valid(grant)
        except (grants.GrantError, OSError, ValueError):
            active = False
        if not active:
            return {
                "schema_version": AUTORUN_SCHEMA_VERSION,
                "run_id": run_id,
                "status": "revoked",
                "checked": [],
                "new_dispatches": 0,
                "budget_changed": False,
            }
    checked: list[dict[str, Any]] = []
    unresolved = False
    invalid = False
    for task_id, record in ledger["tasks"].items():
        if not isinstance(record, dict):
            return {
                "schema_version": AUTORUN_SCHEMA_VERSION,
                "run_id": run_id,
                "status": "invalid",
                "checked": [],
                "new_dispatches": 0,
                "budget_changed": False,
            }
        request = _last_open_request(record)
        if request is None:
            continue
        request_id = str(request.get("request_id") or "")
        entry = {
            "task_id": task_id,
            "request_id": request_id,
            "attempt": request.get("attempt"),
            "stage": request.get("stage"),
        }
        if request.get("stage") != "result_received":
            if callable(resolver):
                candidate = _try_resolve_open_request(adapter, request)
                task = tasks_by_id.get(task_id)
                if candidate is not None and task is not None:
                    valid, reason = _validate_recovered_result(project, run_id, task, request, candidate)
                    if valid:
                        payload_bytes = canonical_json(candidate)
                        _mark_request_stage(
                            project,
                            ledger,
                            record,
                            task_id,
                            request_id,
                            "result_received",
                            response_digest=sha256_bytes(payload_bytes),
                            result_payload=payload_bytes.decode("utf-8"),
                            nonce=str(candidate.get("nonce") or ""),
                            resolved_by="query_only",
                        )
                        checked.append({**entry, "status": "saved_response_valid"})
                        continue
                    invalid = True
                    checked.append({**entry, "status": "invalid", "reason_code": reason})
                    continue
            unresolved = True
            checked.append({**entry, "status": "not_supported"})
            continue
        try:
            payload = json.loads(str(request.get("result_payload") or ""))
            digest = sha256_bytes(canonical_json(payload))
            if digest != request.get("response_digest"):
                raise ValueError("saved_response_digest_mismatch")
            task = tasks_by_id.get(task_id)
            if task is None:
                raise ValueError("saved_response_task_unknown")
            valid, reason = _validate_recovered_result(project, run_id, task, request, payload)
            if not valid:
                raise ValueError(reason)
            checked.append({**entry, "status": "saved_response_valid"})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            invalid = True
            checked.append({**entry, "status": "invalid", "reason_code": sanitize_text(str(exc))[:100]})
    if invalid:
        status = "invalid"
    elif unresolved:
        status = "not_supported"
    elif checked:
        status = "verified"
    else:
        status = "nothing_to_query"
    return {
        "schema_version": AUTORUN_SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "checked": checked,
        "new_dispatches": 0,
        "budget_changed": False,
    }


def _snapshot_files(project: Path, outputs: list[str]) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for relative in outputs:
        target = project / relative
        if target.exists():
            snapshot[relative] = sha256_bytes(target.read_bytes())
    return snapshot


def _snapshot_bytes(project: Path, outputs: list[str]) -> dict[str, str | None]:
    """请求时前像：包含缺失文件（None），用于提交点精确比对（F03/R09）。"""
    snapshot: dict[str, str | None] = {}
    for relative in outputs:
        target = project / relative
        snapshot[relative] = sha256_bytes(target.read_bytes()) if target.exists() else None
    return snapshot


def _run_dir(project: Path, run_id: str) -> Path:
    return project / ".opencoding" / "dev-runs" / _safe_run_segment(run_id)


def _cancel_control_path(root: Path, run_id: str) -> Path:
    return root / ".opencoding" / "autoruns" / f"{_safe_run_segment(run_id)}.cancel.json"


def _persist_cancel_control(project: Path, run_id: str, *, reason: str) -> None:
    """取消状态写入独立受控存储；运行中的账本旧对象不能覆盖它（F04/R07）。"""
    payload = {
        "schema_version": AUTORUN_SCHEMA_VERSION,
        "run_id": run_id,
        "cancel_requested": True,
        "reason": sanitize_text(reason),
        "requested_at": _now(),
    }
    path = _cancel_control_path(project, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _cancel_requested(project: Path, run_id: str) -> bool:
    """三时点共用的取消重查；取消存储不可读时按已取消处理（fail-closed）。"""
    control = _cancel_control_path(project, run_id)
    if control.exists():
        try:
            payload = json.loads(control.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return True
        return bool(payload.get("cancel_requested"))
    try:
        ledger = _load_ledger(project, run_id)
    except AutorunError:
        return False
    if ledger.get("cancel_requested"):
        _persist_cancel_control(project, run_id, reason=str(ledger.get("cancel_reason") or "账本内既有取消标记"))
        return True
    return False


def _require_grant_active(project: Path, grant_id: str) -> None:
    grant = grants.load_grant(project, grant_id)
    valid, reason = grants.grant_valid(grant)
    if not valid:
        raise AutorunError(reason, "批次授权在提交点核对失效：" + reason)


def _validate_candidate_entries(grant: Mapping[str, Any], task: Mapping[str, Any], files: Any) -> list[tuple[str, str]]:
    """一次性校验完整候选；任何拒绝都发生在任何写入之前（F03/R04）。"""
    if not isinstance(files, list) or not files:
        raise AutorunError("candidate_empty", "候选实现不含任何文件，不能标记成功")
    allowed_outputs = {item for item in task["outputs"]}
    validated: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, Mapping) or set(entry) != {"path", "content"}:
            raise AutorunError("candidate_shape_invalid", "候选文件必须是 path/content 对象")
        relative = entry["path"]
        content = entry["content"]
        if not isinstance(relative, str) or not isinstance(content, str):
            raise AutorunError("candidate_shape_invalid", "候选文件字段必须是字符串")
        normalized = relative.replace("\\", "/").lstrip("./")
        if normalized in seen:
            raise AutorunError("candidate_duplicate_path", "候选文件重复出现：" + normalized)
        if normalized not in allowed_outputs:
            raise AutorunError("candidate_path_not_allowed", "候选文件不在本任务允许产物内：" + normalized)
        scope_error = grants.check_grant_scope(grant, action_kind="local_write", targets=[normalized])
        if scope_error:
            raise AutorunError("candidate_out_of_grant", "候选文件越出批次授权范围：" + normalized)
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise AutorunError("candidate_oversized", "候选文件超过大小上限：" + normalized)
        if inspect_sensitive(content)["sensitive"]:
            raise AutorunError("candidate_sensitive", "候选文件包含疑似凭据内容：" + normalized)
        seen.add(normalized)
        validated.append((normalized, content))
    return validated


def _create_scratch(project: Path, run_id: str, task_id: str, attempt: int) -> Path:
    """登记所有权的隔离工作副本：候选代码与验收器只在这里运行（F07）。"""
    scratch = _run_dir(project, run_id) / "verify" / f"{task_id}-{attempt}-{uuid.uuid4().hex[:8]}"
    scratch.mkdir(parents=True, exist_ok=False)
    return scratch


def _is_link(path: Path) -> bool:
    """链接、别名与重解析点一律视为不可信路径（S01/Q18）。"""

    if path.is_symlink():
        return True
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if hasattr(info, "st_reparse_tag") and info.st_reparse_tag:
        return True
    return False


def _excluded(grant: Mapping[str, Any], relative: str) -> bool:
    return grants.check_grant_scope(grant, action_kind="local_write", targets=[relative]) == "step_target_excluded"


def _copy_granted_paths(project: Path, grant: Mapping[str, Any], scratch: Path) -> list[str]:
    """逐项复制授权范围内的必要输入；排除项、链接与重解析点不进入副本（S01/Q07/Q18）。"""

    skipped: list[str] = []
    for relative in grant["allowed_paths"]:
        source = project / relative
        if not source.exists():
            continue
        if _is_link(source):
            skipped.append(relative + "（链接或重解析点，未复制）")
            continue
        if _excluded(grant, relative):
            skipped.append(relative + "（显式排除，未复制）")
            continue
        target = scratch / relative
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            skipped.extend(_copy_tree(source, target, relative, grant))
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            skipped.append(relative + "（非常规文件，未复制）")
    return skipped


def _copy_tree(source: Path, target: Path, relative: str, grant: Mapping[str, Any]) -> list[str]:
    """按条目复制目录：不跟随链接、逐项应用排除清单。"""

    skipped: list[str] = []
    try:
        entries = sorted(os.scandir(source), key=lambda item: item.name)
    except OSError:
        return [relative + "（目录不可读，未复制）"]
    for entry in entries:
        child_relative = f"{relative}/{entry.name}"
        child_source = source / entry.name
        child_target = target / entry.name
        if entry.is_symlink() or _is_link(child_source):
            skipped.append(child_relative + "（链接或重解析点，未复制）")
            continue
        if _excluded(grant, child_relative):
            skipped.append(child_relative + "（显式排除，未复制）")
            continue
        if entry.is_dir():
            child_target.mkdir(parents=True, exist_ok=True)
            skipped.extend(_copy_tree(child_source, child_target, child_relative, grant))
        elif entry.is_file():
            shutil.copy2(child_source, child_target)
        else:
            skipped.append(child_relative + "（非常规文件，未复制）")
    return skipped


def _issue_run_credential(project: Path, grant: Mapping[str, Any], task_id: str, attempt: int, digests: Mapping[str, Any], verifier: Verifying | None = None) -> dict[str, Any]:
    """真实运行候选/验收器前核对 local_run 授权动作；未授权即拒绝（F07）。

    单步凭据同时绑定**实际文件内容摘要（键与值）**与固定验收器身份（S01）。
    """

    if "local_run" not in grant["action_kinds"]:
        raise AutorunError("local_run_not_granted", "批次授权未包含 local_run，不能运行候选代码或验收器")
    verifier_id = getattr(verifier, "__name__", None) or getattr(verifier, "__qualname__", None) or "unknown"
    return grants.issue_step_credential(
        project, grant["grant_id"],
        task_id=task_id, attempt=attempt, action_kind="local_run",
        targets=sorted(digests),
        action_digest=sha256_bytes(canonical_json({"task": task_id, "kind": "local_run", "attempt": attempt, "verifier": str(verifier_id)})),
        input_digest=sha256_bytes(canonical_json({"files": {key: digests[key] for key in sorted(digests)}})),
    )


def _stage_and_verify(project: Path, grant: Mapping[str, Any], task: Mapping[str, Any], files: Any, run_id: str, attempt: int, verifier: Verifying, trusted_fixture: str | None = None) -> tuple[bool, list[str], dict[str, str]]:
    """阶段一：隔离工作副本中叠加候选并运行验收；真实项目零写入。"""
    validated = _validate_candidate_entries(grant, task, files)
    digests = {relative: sha256_bytes(content.encode("utf-8")) for relative, content in validated}
    scratch = _create_scratch(project, run_id, task["task_id"], attempt)
    _prepare_sandbox(project, scratch, trusted_fixture=trusted_fixture)
    skipped = _copy_granted_paths(project, grant, scratch)
    for relative, content in validated:
        target = scratch / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    credential = _issue_run_credential(project, grant, task["task_id"], attempt, digests, verifier)
    grants.check_step_credential(project, credential["credential_id"], expect_task_id=task["task_id"], consume=True)
    try:
        ok, evidence = verifier(scratch)
    except Exception as exc:
        ok, evidence = False, [
            "verifier_exception",
            type(exc).__name__ + ": " + sanitize_text(str(exc))[:300],
        ]
    return ok, evidence + [f"隔离副本未复制：{item}" for item in skipped], digests


def _verify_copy(project: Path, grant: Mapping[str, Any], task: Mapping[str, Any], run_id: str, attempt: int, verifier: Verifying, trusted_fixture: str | None = None) -> tuple[bool, list[str]]:
    """对当前项目内容做一次隔离验收（无候选叠加）；用于待核实与总体检查。"""
    digests = _snapshot_files(project, list(task["outputs"]))
    scratch = _create_scratch(project, run_id, task["task_id"], attempt)
    _prepare_sandbox(project, scratch, trusted_fixture=trusted_fixture)
    _copy_granted_paths(project, grant, scratch)
    credential = _issue_run_credential(project, grant, task["task_id"], attempt, digests, verifier)
    grants.check_step_credential(project, credential["credential_id"], expect_task_id=task["task_id"], consume=True)
    try:
        return verifier(scratch)
    except Exception as exc:
        return False, [
            "verifier_exception",
            type(exc).__name__ + ": " + sanitize_text(str(exc))[:300],
        ]


def _rollback_transaction(project: Path, transaction_id: str | None) -> dict[str, Any]:
    """真实执行回滚并如实返回状态；异常不再被吞掉（S04/Q15）。"""

    if not transaction_id:
        return {"status": "unavailable", "changed_paths": [], "residual_paths": [], "error": "缺少事务编号"}
    try:
        outcome = transactions.rollback_changes(project, transaction_id)
    except Exception as exc:  # 回滚失败必须可见，不能当成“已全部恢复”。
        return {
            "status": "failed",
            "changed_paths": [],
            "residual_paths": [],
            "error": sanitize_text(str(exc))[:300],
            "error_type": type(exc).__name__,
        }
    if not isinstance(outcome, dict):
        return {"status": "failed", "changed_paths": [], "residual_paths": [], "error": "回滚未返回结果"}
    return {
        "status": str(outcome.get("status", "unknown")),
        "changed_paths": list(outcome.get("changed_paths", [])),
        "residual_paths": list(outcome.get("residual_paths", outcome.get("uncertain_paths", []))),
    }


def _rollback_phrase(outcome: Mapping[str, Any]) -> str:
    """按真实回滚结果生成说明；只有完整恢复才能说“全部回滚”（S04）。"""

    status = str(outcome.get("status", "unknown"))
    if status == "rolled_back":
        return "已回滚本次全部写入"
    changed = list(outcome.get("changed_paths", []))
    residual = list(outcome.get("residual_paths", []))
    detail = outcome.get("error") or (",".join(residual) if residual else "无路径被恢复")
    return (
        "回滚未完成（" + status + "）：已恢复 " + ",".join(changed)
        + "；待人工核对 " + str(detail) + "；恢复入口见运行目录下的恢复记录。"
    )


def _path_hash(project: Path, relative: str) -> str | None:
    """当前文件字节摘要；缺失返回 None（S04/Q10 逐路径判定）。"""

    target = project / relative
    try:
        return sha256_bytes(target.read_bytes()) if target.exists() else None
    except OSError:
        return None


def _file_identity(target: Path) -> list[int] | None:
    """当前文件的设备/inode 身份；缺失返回 None（C02 归属核对用）。"""

    try:
        info = target.stat()
    except OSError:
        return None
    return [int(getattr(info, "st_dev", 0)), int(getattr(info, "st_ino", 0))]


def _transaction_postimage_identity(project: Path, transaction_id: str, relative: str) -> list[int] | None:
    """从事务事件日志取本事务写入后像的文件身份（C02 归属证据）。"""

    events = project / ".opencoding" / "transactions" / transaction_id / "events.jsonl"
    identity: list[int] | None = None
    try:
        for line in events.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if relative not in (event.get("changed_paths") or []):
                continue
            candidate = event.get("after_identity")
            if event.get("status") in {"write_intent", "write_applied"} and isinstance(candidate, list) and len(candidate) == 2:
                identity = [int(candidate[0]), int(candidate[1])]
    except OSError:
        return None
    return identity


def _restore_from_preimage(
    project: Path,
    transaction_id: str,
    relative: str,
    expected_after_sha256: str | None,
    *,
    identity_change_explained: bool = False,
) -> tuple[bool, str]:
    """整批回滚时的有记录补偿恢复（S04/Q10；C02/C03 收口）。

    逆序恢复后续事务会重写文件、改变文件身份，事务层会保守地判为漂移并拒绝早期事务。
    补偿只应用于**可证明归属本批受控版本链**的路径：
    - 当前内容必须严格等于本事务写入的后像字节；内容已被改动的路径一律跳过。
    - 当前文件身份必须等于本事务事件日志记录的后像身份；不一致时，只有当该身份
      变化由**本次回滚中更晚事务的恢复**造成（identity_change_explained）才继续，
      否则停止并报告，绝不删除或覆盖（C02：同字节用户替换文件不是本批所有物）。
    - 备份字节必须先通过 before_sha256 校验才允许写入目标（C03：不得先写坏目标
      再报告备份不完整）。
    """

    evidence = project / ".opencoding" / "transactions" / transaction_id
    try:
        manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "manifest_unreadable"
    entry: Mapping[str, Any] | None = None
    index = -1
    for position, item in enumerate(manifest.get("entries", [])):
        if str(item.get("path")) == relative:
            entry, index = item, position
            break
    if entry is None:
        return False, "path_not_in_manifest"
    try:
        target = transactions.safe_target(project, relative, allow_missing=True)
    except ValueError:
        return False, "unsafe_target"
    current = sha256_bytes(target.read_bytes()) if target.exists() else None
    if current != expected_after_sha256:
        return False, "content_is_not_this_transaction_postimage"
    # C02：身份归属核对。同字节但身份不同的文件可能是用户替换物，不是本批所有物。
    recorded_identity = _transaction_postimage_identity(project, transaction_id, relative)
    current_identity = _file_identity(target)
    if recorded_identity is None or current_identity is None:
        return False, "postimage_identity_unavailable"
    if current_identity != recorded_identity and not identity_change_explained:
        return False, "identity_changed_not_attributable"
    if not bool(entry.get("before_exists")):
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False, "preimage_restore_failed"
        return (not target.exists()), "restored_by_removal"
    try:
        payload = (evidence / "preimage" / f"{index}.bin").read_bytes()
    except OSError:
        return False, "preimage_missing"
    # C03：备份完整性先验——校验不过就不碰目标，保持当前文件原样。
    if sha256_bytes(payload) != entry.get("before_sha256"):
        return False, "preimage_verification_failed"
    temporary = target.with_name(target.name + ".opencoding-restore-" + uuid.uuid4().hex)
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return False, "preimage_restore_failed"
    restored_now = sha256_bytes(target.read_bytes()) if target.exists() else None
    if restored_now != entry.get("before_sha256"):
        return False, "preimage_verification_failed"
    return True, "restored_from_preimage"


def _recovery_dir(project: Path, run_id: str) -> Path:
    return _run_dir(project, run_id) / "recovery"


def _binding_dir(project: Path, run_id: str) -> Path:
    """提交绑定单独存放：绑定不是写入所有权记录，不能混进恢复目录（S03/Q14）。"""

    return _run_dir(project, run_id) / "commit-bindings"


def _write_json_atomic(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _recovery_sequence(directory: Path) -> int:
    """恢复记录序号：按写入先后单调递增，用于逆序回滚（S04/Q10）。"""

    directory.mkdir(parents=True, exist_ok=True)
    return len(list(directory.glob("*.json"))) + 1


def _record_commit_binding(project: Path, run_id: str, task_id: str, attempt: int, grant_id: str, plan_digest: str, entries: list[Mapping[str, Any]], input_digests: Mapping[str, Any]) -> Path:
    """在实际写入前持久绑定事务关联：运行/任务/候选与输入摘要（S03/Q14）。"""

    directory = _binding_dir(project, run_id)
    record = {
        "schema_version": AUTORUN_SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "attempt": attempt,
        "grant_id": grant_id,
        "state": "applying",
        "kind": "binding",
        "seq": _recovery_sequence(directory),
        "plan_digest": plan_digest,
        "input_digests": dict(input_digests),
        "recorded_at": _now(),
        "files": [
            {
                "path": entry["path"],
                "operation": entry["operation"],
                "before_sha256": entry["before_sha256"],
                "after_sha256": entry["after_sha256"],
            }
            for entry in entries
        ],
    }
    path = directory / f"{task_id}-{attempt}.binding.json"
    _write_json_atomic(path, record)
    return path


def _finalize_commit_binding(path: Path | None, *, state: str, transaction_id: str | None = None, note: str | None = None) -> None:
    """事务落地后更新绑定状态；崩溃时留下的 applying 记录可被接续时核对。"""

    if path is None or not Path(path).exists():
        return
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return
    if not isinstance(record, dict):
        return
    record["state"] = state
    record["finished_at"] = _now()
    if transaction_id:
        record["transaction_id"] = str(transaction_id)
    if note:
        record["note"] = sanitize_text(note)
    _write_json_atomic(Path(path), record)


class _CommitGuard:
    """受控提交点：与实际文件应用层衔接的取消/撤销复查（S02/Q08/Q16）。

    文件应用层会把提交点抛出的错误转成失败结果；这里同时记住首次拒绝原因，
    使上层能按真实原因（取消/授权撤销）归因，而不是笼统报“应用未完成”。
    """

    def __init__(self, project: Path, grant_id: str, run_id: str):
        self._project = project
        self._grant_id = grant_id
        self._run_id = run_id
        self.error: BaseException | None = None

    def __call__(self) -> None:
        try:
            if _cancel_requested(self._project, self._run_id):
                raise AutorunError("commit_cancelled", "提交点检测到取消：未写入任何文件")
            _require_grant_active(self._project, self._grant_id)
        except (AutorunError, grants.GrantError) as exc:
            if self.error is None:
                self.error = exc
            raise


def _record_pending_recovery(project: Path, run_id: str, task_id: str, attempt: int, grant_id: str, transaction_id: str | None, entries: list[Mapping[str, Any]], result: Mapping[str, Any], rollback: Mapping[str, Any] | None = None) -> Path:
    """部分/失败提交也留下可定位的恢复入口，不出现无主残留（S02/Q09）。"""

    directory = _recovery_dir(project, run_id)
    record = {
        "schema_version": AUTORUN_SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "attempt": attempt,
        "grant_id": grant_id,
        "transaction_id": str(transaction_id) if transaction_id else None,
        "recorded_at": _now(),
        "seq": _recovery_sequence(directory),
        "state": "needs_recovery" if str((rollback or {}).get("status")) != "rolled_back" else "rolled_back",
        "apply_status": str(result.get("status")),
        "changed_paths": list(result.get("changed_paths", [])),
        "uncertain_paths": list(result.get("uncertain_paths", [])),
        "rollback": dict(rollback or {}),
        "files": [
            {
                "path": entry["path"],
                "operation": entry["operation"],
                "before_sha256": entry["before_sha256"],
                "after_sha256": entry["after_sha256"],
            }
            for entry in entries
        ],
        "receipt_path": result.get("receipt_path"),
    }
    path = directory / f"{task_id}-{attempt}-pending-{transaction_id or 'none'}.json"
    _write_json_atomic(path, record)
    return path


def _record_recovery(project: Path, run_id: str, task_id: str, attempt: int, grant_id: str, transaction_id: str, entries: list[Mapping[str, Any]], result: Mapping[str, Any]) -> Path:
    """登记本次受控写入的所有权与恢复入口；前像本体由事务层证据保存（F09）。"""
    directory = _recovery_dir(project, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": AUTORUN_SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "attempt": attempt,
        "grant_id": grant_id,
        "transaction_id": transaction_id,
        "seq": _recovery_sequence(directory),
        "recorded_at": _now(),
        "state": "committed",
        "files": [
            {
                "path": entry["path"],
                "operation": entry["operation"],
                "before_sha256": entry["before_sha256"],
                "after_sha256": entry["after_sha256"],
            }
            for entry in entries
        ],
        "receipt_path": result.get("receipt_path"),
    }
    path = directory / f"{task_id}-{attempt}-{transaction_id}.json"
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path


def _commit_candidate(project: Path, grant: Mapping[str, Any], task: Mapping[str, Any], files: Any, run_id: str, task_id: str, attempt: int, expected: Mapping[str, str | None]) -> dict[str, str]:
    """阶段二：受控提交点核对有效授权、精确前像与凭据，经既有事务层应用（F03/F09/S02）。"""

    validated = _validate_candidate_entries(grant, task, files)
    _require_grant_active(project, grant["grant_id"])
    if _cancel_requested(project, run_id):
        raise AutorunError("commit_cancelled", "提交开始前已生效的取消：拒绝写入")
    entries: list[dict[str, Any]] = []
    for normalized, content in validated:
        target = project / normalized
        current = sha256_bytes(target.read_bytes()) if target.exists() else None
        if current != expected.get(normalized):
            raise AutorunError("preimage_drift", "候选提交前检测到文件被修改（用户或外部改动），拒绝覆盖：" + normalized)
        entries.append({
            "path": normalized,
            "operation": "update" if current is not None else "create",
            "before_sha256": current,
            "after_sha256": sha256_bytes(content.encode("utf-8")),
            "content": content,
        })
    for normalized, content in validated:
        credential = grants.issue_step_credential(
            project, grant["grant_id"],
            task_id=task_id, attempt=attempt, action_kind="local_write",
            targets=[normalized],
            action_digest=sha256_bytes(canonical_json({"type": "write_text", "path": normalized, "content": content})),
            input_digest=sha256_bytes(content.encode("utf-8")),
        )
        grants.check_step_credential(project, credential["credential_id"], expect_task_id=task_id, consume=True)
    # R01 伴随修复:计划根必须与事务层校验根同源(preview/apply 都经 _root 规范化)。
    # 此前用未解析的 str(project),工作区路径含 8.3 短名/未规范组件时
    # apply 端 _validate_plan 必报 "plan root mismatch",任何真实提交都无法完成。
    plan_core = {"schema_version": transactions.SCHEMA_VERSION,
                 "root": str(transactions._root(project)), "entries": entries}
    plan = dict(plan_core)
    plan["plan_digest"] = sha256_bytes(canonical_json(plan_core))
    # S02：取消/撤销复查随计划一起交给文件应用层，在每次落盘前实际执行。
    guard = _CommitGuard(project, grant["grant_id"], run_id)
    plan["commit_guard"] = guard
    input_digests = {entry["path"]: entry["after_sha256"] for entry in entries}
    binding = _record_commit_binding(project, run_id, task_id, attempt, grant["grant_id"], plan["plan_digest"], entries, input_digests)
    result = transactions.apply_changes(project, plan, approved_digest=plan["plan_digest"])
    transaction_id = result.get("transaction_id")
    if result["status"] != "applied":
        reasons = ";".join(str(item) for item in result.get("reason_codes", [])[:3])
        if "preimage drift" in reasons:
            _finalize_commit_binding(binding, state="failed", transaction_id=str(transaction_id) if transaction_id else None, note="preimage drift")
            raise AutorunError("preimage_drift", "事务层前像核对未通过，未覆盖既有内容：" + reasons)
        if str(transaction_id):
            _finalize_commit_binding(binding, state="partial_failure", transaction_id=str(transaction_id), note="apply " + str(result["status"]))
        # S02：部分写入必须有记录并能恢复，不留无自主恢复入口的残留。
        rollback = _rollback_transaction(project, str(transaction_id) if transaction_id else None)
        _record_pending_recovery(project, run_id, task_id, attempt, grant["grant_id"], str(transaction_id) if transaction_id else None, entries, result, rollback)
        _finalize_commit_binding(binding, state=str(rollback.get("status")), transaction_id=str(transaction_id) if transaction_id else None, note="apply " + str(result["status"]))
        blocked = guard.error
        if blocked is not None:
            # S02：提交点拒绝（取消/撤销）按真实原因上报，不被“应用未完成”掩盖。
            raise AutorunError(
                getattr(blocked, "code", "commit_blocked"),
                "受控提交点拒绝写入：" + sanitize_text(str(blocked)) + "；" + _rollback_phrase(rollback),
            )
        raise AutorunError(
            "transaction_apply_" + str(result["status"]),
            "受控事务应用未完成：" + (reasons or str(result["status"])) + "；" + _rollback_phrase(rollback),
        )
    fresh = grants.load_grant(project, grant["grant_id"])
    valid, reason = grants.grant_valid(fresh)
    cancelled = _cancel_requested(project, run_id)
    if not valid or cancelled:
        rollback = _rollback_transaction(project, str(transaction_id) if transaction_id else None)
        code = "commit_cancelled" if cancelled else reason
        phrase = _rollback_phrase(rollback)
        if str(rollback.get("status")) != "rolled_back":
            # S04：回滚失败不能报成“已全部恢复”，残留必须可定位。
            _record_pending_recovery(project, run_id, task_id, attempt, grant["grant_id"], str(transaction_id) if transaction_id else None, entries, result, rollback)
            _finalize_commit_binding(binding, state="needs_recovery", transaction_id=str(transaction_id) if transaction_id else None, note=code)
            raise AutorunError(code, "提交后" + ("收到取消" if cancelled else "批次授权失效") + "；" + phrase)
        _finalize_commit_binding(binding, state="rolled_back", transaction_id=str(transaction_id) if transaction_id else None, note=code)
        raise AutorunError(code, "提交后" + ("收到取消" if cancelled else "批次授权失效") + "；" + phrase)
    _record_recovery(project, run_id, task_id, attempt, grant["grant_id"], str(transaction_id), entries, result)
    _finalize_commit_binding(binding, state="committed", transaction_id=str(transaction_id) if transaction_id else None)
    return {entry["path"]: entry["after_sha256"] for entry in entries}


def rollback_task_files(root: str | Path, run_id: str, task_id: str | None = None) -> dict[str, Any]:
    """回滚一次运行（或指定任务）的受控写入：复用既有事务回滚（F09/A26）。

    保留用户后续改动与业务数据：后像已漂移的路径由事务层跳过不覆盖。
    完整恢复与部分恢复分别报告。
    """
    project = Path(root).resolve()
    directory = _recovery_dir(project, run_id)
    if not directory.exists():
        raise AutorunError("recovery_record_missing", "没有可回滚的受控写入记录")
    # S04：按真实提交顺序逆序恢复；连续事务覆盖同一文件时先恢复最后一次写入。
    candidates: list[tuple[int, str, Path, dict[str, Any]]] = []
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict) or record.get("state") not in {"committed", "needs_recovery"}:
            continue
        if record.get("kind") == "binding":
            continue
        if task_id is not None and record.get("task_id") != task_id:
            continue
        transaction_id = record.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            continue
        try:
            seq = int(record.get("seq", 0))
        except (TypeError, ValueError):
            seq = 0
        candidates.append((seq, str(path), path, record))
    if not candidates:
        raise AutorunError("recovery_record_missing", "没有匹配的已提交受控写入记录")
    candidates.sort(key=lambda item: (-item[0], item[1]))
    transactions_done: list[dict[str, Any]] = []
    restored_anywhere: set[str] = set()
    # C02：本次回滚中已由更晚事务恢复过的路径，其文件身份变化可归因于本批受控恢复链。
    explained_identity_changes: set[str] = set()
    for _seq, _name, path, record in candidates:
        transaction_id = str(record.get("transaction_id"))
        outcome = _rollback_transaction(project, transaction_id)
        status = str(outcome.get("status"))
        restored = list(outcome.get("changed_paths", []))
        explained_identity_changes.update(restored)
        residual = list(outcome.get("residual_paths", []))
        notes: list[str] = []
        by_path = {str(item.get("path")): item for item in record.get("files", []) if item.get("path")}
        # S04/Q10：整批回滚要回到运行前。逆序恢复会改变文件身份，事务层会保守地判为漂移，
        # 因此对本事务后像字节仍严格一致、且身份变化可归因（身份一致，或由本次回滚中
        # 更晚事务的恢复造成）的路径，按事务前像做有记录补偿恢复；
        # 内容已被改动或身份变化无法归因的路径一律跳过，不覆盖（C02）。
        for relative, item in by_path.items():
            if _path_hash(project, relative) == item.get("before_sha256"):
                if relative not in restored:
                    restored.append(relative)
                continue
            if _path_hash(project, relative) != item.get("after_sha256"):
                notes.append(relative + ":skipped:content_changed")
                continue
            ok, note = _restore_from_preimage(
                project,
                transaction_id,
                relative,
                item.get("after_sha256"),
                identity_change_explained=relative in explained_identity_changes,
            )
            if ok:
                restored.append(relative)
                explained_identity_changes.add(relative)
                notes.append(relative + ":compensated:" + note)
            else:
                notes.append(relative + ":unrestored:" + note)
        pending = [relative for relative, item in by_path.items() if _path_hash(project, relative) != item.get("before_sha256")]
        if by_path and not pending:
            status = "rolled_back"
            residual = []
        elif pending:
            residual = sorted(set(residual) | set(pending))
            status = "partial_failure" if restored else "blocked"
        restored_anywhere.update(restored)
        skipped = [
            relative for relative in by_path
            if relative not in set(restored) and relative not in restored_anywhere
        ]
        transactions_done.append({
            "task_id": record.get("task_id"),
            "attempt": record.get("attempt"),
            "transaction_id": transaction_id,
            "status": status,
            "restored_paths": restored,
            "skipped_paths": skipped,
            "residual_paths": residual,
            "error": outcome.get("error"),
            "notes": notes,
        })
        record["state"] = "rolled_back" if status == "rolled_back" else ("partial_failure" if restored else "needs_recovery")
        record["rolled_back_at"] = _now()
        record["rollback_status"] = status
        _write_json_atomic(path, record)
    rolled_back = sum(1 for item in transactions_done if item["status"] == "rolled_back")
    if rolled_back == len(transactions_done):
        overall = "rolled_back"
    elif restored_anywhere:
        # S04/Q21：只要发生了部分恢复就如实报部分恢复，不按“完整成功事务数”掩盖。
        overall = "partial_failure"
    else:
        overall = "blocked"
    return {
        "schema_version": AUTORUN_SCHEMA_VERSION,
        "run_id": run_id,
        "status": overall,
        "full_restored": rolled_back,
        "partial_or_blocked": len(transactions_done) - rolled_back,
        "restored_paths": sorted(restored_anywhere),
        "transactions": transactions_done,
    }


def _update_lineage(ledger: dict[str, Any], task_id: str, attempt: int, written: Mapping[str, str]) -> None:
    """记录每个文件最后一次受控写入的任务血缘（F06/R08）。"""
    lineage = ledger.setdefault("file_lineage", {})
    for relative, digest in written.items():
        lineage[relative] = {"task_id": task_id, "attempt": attempt, "sha256": digest, "at": _now()}


def _resume_verdict(project: Path, ledger: dict[str, Any], task: Mapping[str, Any], record: Mapping[str, Any]) -> str:
    """恢复判定：合法后继更新不算用户修改；残缺成功记录不自动通过（F06/R08/R12）。"""
    expected = record.get("file_hashes")
    outputs = list(task["outputs"])
    if (
        not isinstance(expected, dict)
        or set(expected) != set(outputs)
        or not all(isinstance(value, str) and len(value) == 64 for value in expected.values())
    ):
        return "receipt_invalid"
    lineage = ledger.get("file_lineage", {})
    current = _snapshot_files(project, outputs)
    successor = False
    for relative in outputs:
        if current.get(relative) == expected[relative]:
            continue
        writer = lineage.get(relative)
        if (
            isinstance(writer, dict)
            and writer.get("task_id") not in (None, task["task_id"])
            and writer.get("sha256") == current.get(relative)
        ):
            successor = True
            continue
        return "user_modified"
    return "successor" if successor else "consistent"


def _reverify_task(project: Path, grant: Mapping[str, Any], task: Mapping[str, Any], verifier: Verifying, run_id: str, record: dict[str, Any], trusted_fixture: str | None = None) -> bool:
    """残缺成功记录的待核实路径：重跑隔离验收，不请求 AI，不自动通过。"""
    ok, _evidence = _verify_copy(project, grant, task, run_id, 1, verifier, trusted_fixture=trusted_fixture)
    if ok:
        record["file_hashes"] = _snapshot_files(project, list(task["outputs"]))
        record.setdefault("attempts", []).append({
            "attempt": len(record.get("attempts", [])) + 1,
            "kind": "reverify",
            "verification_ok": True,
            "verification_evidence": [],
            "file_hashes": dict(record["file_hashes"]),
        })
    return ok


def existing_run_grant_id(root: str | Path, run_id: str) -> str | None:
    """返回该运行编号已绑定的批次授权；没有账本时返回 None（表示新批）。"""
    project = Path(root).resolve()
    path = _ledger_path(project, run_id)
    if not path.exists():
        return None
    ledger = _load_ledger(project, run_id)
    value = ledger.get("grant_id")
    return value if isinstance(value, str) and value.startswith("grant-") else None


def _request_implementation(
    adapter,
    task: Mapping[str, Any],
    run_id: str,
    attempt: int,
    *,
    prepared: Mapping[str, Any],
) -> dict[str, Any]:
    return adapter.complete(
        prepared["messages"],
        request_kind=prepared["request_kind"],
        run_id=run_id,
        task_id=task["task_id"],
        attempt=attempt,
        request_id=prepared["request_id"],
        nonce=prepared["nonce"],
        input_digest=prepared["input_digest"],
        allowed_outputs=prepared["allowed_outputs"],
    )


def _collect_request_sources(
    project: Path, scenario: Mapping[str, Any], task: Mapping[str, Any]
) -> dict[str, str]:
    task_map = {str(item["task_id"]): item for item in scenario["tasks"]}
    pending = list(task.get("depends_on", []))
    ancestors: set[str] = set()
    while pending:
        dependency = str(pending.pop())
        if dependency in ancestors:
            continue
        ancestors.add(dependency)
        parent = task_map.get(dependency)
        if isinstance(parent, Mapping):
            pending.extend(parent.get("depends_on", []))
    paths = set(str(item) for item in task["outputs"])
    for dependency in ancestors:
        parent = task_map.get(dependency)
        if isinstance(parent, Mapping):
            paths.update(str(item) for item in parent.get("outputs", []))
    sources: dict[str, str] = {}
    total_bytes = 0
    for relative in sorted(paths):
        target = project / relative
        if not target.exists():
            continue
        if target.is_symlink() or not target.is_file():
            raise AutorunError("request_source_untrusted", "请求上下文包含链接或非常规文件：" + relative)
        resolved = target.resolve(strict=True)
        if project not in resolved.parents:
            raise AutorunError("request_source_outside_root", "请求上下文越出项目根目录：" + relative)
        raw = target.read_bytes()
        total_bytes += len(raw)
        if total_bytes > 512 * 1024:
            raise AutorunError("request_context_oversized", "必要源码上下文超过 512 KiB 上限")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AutorunError("request_source_encoding_invalid", "必要源码不是 UTF-8：" + relative) from exc
        if inspect_sensitive(content)["sensitive"]:
            raise AutorunError("request_source_sensitive", "必要源码包含疑似凭据，拒绝纳入请求：" + relative)
        sources[relative] = content
    return sources


def _remaining_budget(grant: Mapping[str, Any]) -> dict[str, int]:
    budget = grant.get("budget", {})
    used = grant.get("budget_used", {})
    result: dict[str, int] = {}
    for name in ("max_ai_requests", "max_repair_rounds"):
        maximum = int(budget.get(name, 0))
        counter = "ai_requests" if name == "max_ai_requests" else "repair_rounds"
        consumed = int(used.get(counter, 0))
        result[counter] = max(0, maximum - consumed)
    return result


def _build_implementation_messages(
    scenario: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    run_id: str,
    request_kind: str,
    snapshot: Mapping[str, str | None],
    repair_evidence: list[str] | None,
    source_files: Mapping[str, str],
    remaining_budget: Mapping[str, int],
    request_id: str,
    nonce: str,
    attempt: int,
) -> list[dict[str, str]]:
    prompt_lines = [
        "OpenCoding 协议版本：" + ADAPTER_SCHEMA_VERSION,
        f"request_id：{request_id}",
        f"run_id：{run_id}",
        f"task_id：{task['task_id']}",
        f"attempt：{attempt}",
        f"request_kind：{request_kind}",
        f"nonce：{nonce}",
        "允许产物：" + ", ".join(sorted(str(item) for item in task["outputs"])),
        f"任务：{task['title']}（{task['task_id']}）",
        f"目标：{scenario['goal']}",
        f"要求：{task['objective']}",
        "固定验收条件：" + json.dumps(task["acceptance"], ensure_ascii=False),
        "剩余批次预算：" + json.dumps(dict(remaining_budget), ensure_ascii=False, sort_keys=True),
        task["prompt"],
        (
            "只输出 JSON 对象：nonce（回传给定值）、summary（一句话）、"
            + ("fix（说明修复）和 files（数组，元素为 path/content）。" if request_kind == "repair"
               else "files（数组，元素为 path/content）。")
        ),
    ]
    if snapshot:
        prompt_lines.append("请求时允许产物前像摘要：" + json.dumps(dict(snapshot), ensure_ascii=False, sort_keys=True))
    if source_files:
        prompt_lines.append("当前任务及依赖的授权源码：" + json.dumps(dict(source_files), ensure_ascii=False, sort_keys=True))
    if repair_evidence:
        prompt_lines.append("上次验收失败证据（请定位原因并局部修复）：")
        prompt_lines.extend("- " + item for item in repair_evidence[:6])
    return [
        {"role": "system", "content": "你是受控执行环境中的编码助手。只输出 JSON，不输出解释性散文。"},
        {"role": "user", "content": "\n".join(prompt_lines)},
    ]


def _prepare_implementation_request(
    project: Path,
    scenario: Mapping[str, Any],
    task: Mapping[str, Any],
    run_id: str,
    attempt: int,
    grant_id: str,
    *,
    request_kind: str,
    repair_evidence: list[str] | None,
) -> dict[str, Any]:
    request_id = "req-" + uuid.uuid4().hex
    nonce = "nonce-" + uuid.uuid4().hex
    allowed_outputs = list(task["outputs"])
    snapshot = _snapshot_bytes(project, allowed_outputs)
    source_files = _collect_request_sources(project, scenario, task)
    remaining_budget = _remaining_budget(grants.load_grant(project, grant_id))
    messages = _build_implementation_messages(
        scenario,
        task,
        run_id=run_id,
        request_kind=request_kind,
        snapshot=snapshot,
        repair_evidence=repair_evidence,
        source_files=source_files,
        remaining_budget=remaining_budget,
        request_id=request_id,
        nonce=nonce,
        attempt=attempt,
    )
    input_digest = sha256_bytes(canonical_json({
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "request_id": request_id,
        "run_id": run_id,
        "task_id": task["task_id"],
        "attempt": attempt,
        "request_kind": request_kind,
        "nonce": nonce,
        "allowed_outputs": allowed_outputs,
        "preimage_digests": snapshot,
        "messages": messages,
    }))
    return {
        "request_id": request_id,
        "nonce": nonce,
        "run_id": run_id,
        "task_id": task["task_id"],
        "attempt": attempt,
        "request_kind": request_kind,
        "allowed_outputs": allowed_outputs,
        "preimage_digests": snapshot,
        "messages": messages,
        "input_digest": input_digest,
        "remaining_budget": remaining_budget,
        "source_digests": {
            path: sha256_bytes(content.encode("utf-8")) for path, content in source_files.items()
        },
    }


def cancel_run(root: str | Path, run_id: str, *, reason: str) -> dict[str, Any]:
    """取消一个运行：持久化取消状态，后续派发立即停止且不自动复活。"""

    project = Path(root).resolve()
    ledger = _load_ledger(project, run_id)
    if not isinstance(reason, str) or not reason.strip():
        raise AutorunError("cancel_reason_invalid", "取消必须说明原因")
    ledger["cancel_requested"] = True
    ledger["cancel_reason"] = sanitize_text(reason.strip())
    ledger["cancelled_at"] = _now()
    _save_ledger(project, ledger)
    # F04/R07：取消状态另存独立受控存储；运行中账本旧对象写回不能覆盖取消。
    _persist_cancel_control(project, run_id, reason=reason.strip())
    uxtext.record_interruption(
        project,
        category="user_rejected",
        action=f"运行 {run_id}",
        reason="用户取消本批次：" + sanitize_text(reason.strip()),
        auto_path="已取消的任务不会自动恢复；如需继续请显式新开批次。",
    )
    return dict(ledger)


def _cancel_pending(project: Path, ledger: dict[str, Any], states: dict[str, str], tasks: list[Mapping[str, Any]], run_id: str, say) -> dict[str, Any]:
    for task in tasks:
        task_id = task["task_id"]
        if states[task_id] in {"pending", "running"}:
            states[task_id] = "cancelled"
            existing = ledger["tasks"].get(task_id)
            if isinstance(existing, dict) and existing.get("attempts"):
                # 保留已发生的尝试历史，只改状态；不覆盖原始失败记录（F06）。
                existing["state"] = "cancelled"
                existing["reason"] = "cancel_requested"
                ledger["tasks"][task_id] = existing
            else:
                ledger["tasks"][task_id] = {"state": "cancelled", "reason": "cancel_requested", "attempts": []}
    ledger["cancel_requested"] = True
    _save_ledger(project, ledger)
    say(uxtext.render_pause(
        "本批次已被取消。",
        "已取消的任务不会自动恢复；保留已验收的产物与账本。",
        "如需继续，显式新开批次。",
    ))
    return _finish(project, ledger, states, tasks, run_id)


def _bind_run(ledger: dict[str, Any], grant_id: str, scenario_digest: str) -> None:
    """运行编号与授权/任务图持久绑定；每次写账本前都重新核对（F05/R05）。"""
    if ledger.get("grant_id") and ledger["grant_id"] != grant_id:
        raise AutorunError("run_grant_mismatch", "该运行编号已绑定其他批次授权；接续必须使用原授权")
    if ledger.get("scenario_digest") and ledger["scenario_digest"] != scenario_digest:
        raise AutorunError("run_scenario_mismatch", "该运行编号已绑定其他任务图；接续必须使用相同任务图")
    ledger["grant_id"] = grant_id
    ledger["scenario_digest"] = scenario_digest


def _load_adopted_plan(
    project: Path, plan_digest: str | None, scenario: Mapping[str, Any]
) -> dict[str, Any] | None:
    """F02：定位并核验采用记录；返回通过核验的记录，找不到返回 None。

    核验范围包含计划节点→实际任务/依赖/验收/产物映射，以及采用时的有效输入
    快照；产物范围不符或输入快照不完整都视为未通过。
    """

    if plan_digest is None:
        return None
    if not isinstance(plan_digest, str) or len(plan_digest) != 64:
        raise AutorunError("adopted_plan_invalid", "采用计划摘要格式无效")
    adoption_dir = project / ".opencoding" / "adoptions"
    matches = []
    if adoption_dir.is_dir():
        for path in adoption_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("plan_digest") == plan_digest:
                matches.append(record)
    scenario_digest = sha256_bytes(canonical_json({
        "goal": scenario.get("goal", ""),
        "tasks": [dict(item) for item in scenario.get("tasks", [])],
    }))
    valid_records = []
    for record in matches:
        task_plan = record.get("task_plan")
        decision_record = record.get("decision_record")
        mapping = record.get("executor_mapping")
        input_snapshot = record.get("input_snapshot")
        plan_task_ids = {
            item.get("id") for item in task_plan.get("tasks", [])
            if isinstance(item, Mapping)
        } if isinstance(task_plan, Mapping) and isinstance(task_plan.get("tasks"), list) else set()
        task_bindings = mapping.get("task_bindings") if isinstance(mapping, Mapping) else None
        expected_task_ids = {item["task_id"] for item in scenario.get("tasks", [])}
        if (
            record.get("validated") is not True
            or not isinstance(input_snapshot, Mapping)
            or record.get("input_digest") != sha256_bytes(canonical_json(input_snapshot))
            or input_snapshot.get("plan_digest") != record.get("plan_digest")
            or input_snapshot.get("facts_digest") != record.get("facts_digest")
            or not isinstance(input_snapshot.get("session_id"), str)
            or not isinstance(task_plan, Mapping)
            or not isinstance(decision_record, Mapping)
            or record.get("plan_digest") != sha256_bytes(canonical_json(task_plan))
            or record.get("facts_digest") != sha256_bytes(
                canonical_json(decision_record.get("facts", []))
            )
            or record.get("decision_input_digest") != sha256_bytes(canonical_json({
                "recommendation": record.get("recommendation"),
                "decision_record": decision_record,
            }))
            or not isinstance(mapping, Mapping)
            or mapping.get("schema_version") != "1.0"
            or mapping.get("supported") is not True
            or mapping.get("executor_id") != "lendreg"
            or mapping.get("scenario_digest") != scenario_digest
            or set(mapping.get("plan_task_ids", [])) - plan_task_ids
            or not isinstance(task_bindings, Mapping)
            or set(task_bindings) != expected_task_ids
            or any(
                not isinstance(binding, Mapping)
                or binding.get("plan_task_id") not in plan_task_ids
                or binding.get("depends_on") != list(task["depends_on"])
                or binding.get("acceptance") != list(task["acceptance"])
                or binding.get("allowed_outputs") != list(task["outputs"])
                or not isinstance(task.get("outputs"), list)
                or not task["outputs"]
                or set(task["outputs"]) - set(binding.get("allowed_outputs") or [])
                for task in scenario.get("tasks", [])
                for binding in [task_bindings.get(task["task_id"])]
            )
        ):
            continue
        valid_records.append(record)
    return valid_records[0] if valid_records else None


def _validate_adopted_plan(
    project: Path, plan_digest: str | None, scenario: Mapping[str, Any]
) -> None:
    if plan_digest is None:
        return
    if _load_adopted_plan(project, plan_digest, scenario) is None:
        raise AutorunError("adopted_plan_missing", "指定采用计划不存在或未通过验证")


def _check_adoption_input_drift(project: Path, plan_digest: str | None, scenario: Mapping[str, Any]) -> None:
    """F02：运行前与接续边界核对采用时的有效输入是否仍然有效。

    发生相关输入漂移时暂停旧计划派发并说明变化与需要的动作；不静默覆盖旧
    计划，也不自动重写授权或预算。
    """

    if plan_digest is None:
        return
    record = _load_adopted_plan(project, plan_digest, scenario)
    if record is None:
        raise AutorunError("adopted_plan_missing", "指定采用计划不存在或未通过验证")
    session_id = record.get("session_id")
    input_snapshot = record.get("input_snapshot")
    if not isinstance(session_id, str) or not session_id or not isinstance(input_snapshot, Mapping):
        raise AutorunError("adopted_plan_missing", "采用记录缺少有效输入快照")
    from . import service as service_module

    try:
        status = service_module.adoption_input_status(project, session_id)
    except service_module.ServiceError as exc:
        raise AutorunError("adopted_plan_input_unavailable", sanitize_text(str(exc))) from exc
    if status.get("input_snapshot_missing") or status.get("drift"):
        changed = ", ".join(str(item) for item in status.get("drift") or []) or "采用记录缺少有效输入快照"
        raise AutorunError(
            "adopted_plan_input_drift",
            "采用时的有效输入已变化（" + changed + "）：暂停旧计划派发，需重新评估并明确采用后继续。",
        )


def _adoption_input_drift_reason(project: Path, ledger: Mapping[str, Any], scenario: Mapping[str, Any]) -> str | None:
    """F02/X03：批次中途核对采用输入是否仍然有效；返回漂移说明或 None。

    供"每次派发前、收结果后提交前"调用；不抛出异常，由调用方做结构化冻结，
    避免批次中途异常逃逸留下不可追溯的中间状态。
    """

    plan_digest = ledger.get("adopted_plan_digest")
    if not plan_digest:
        return None
    try:
        _check_adoption_input_drift(project, plan_digest, scenario)
    except AutorunError as exc:
        if exc.code in {"adopted_plan_input_drift", "adopted_plan_input_unavailable"}:
            return sanitize_text(str(exc))
        raise
    return None


def _persist_attempt(project: Path, ledger: dict[str, Any], task_id: str, record: dict[str, Any], repairs: int, no_progress_rounds: int, last_failure_signature: str) -> None:
    """尝试、失败签名与计数在下一个动作前落盘；崩溃后仍可追溯（S03/Q13）。"""

    record["repairs"] = int(repairs)
    record["no_progress_rounds"] = int(no_progress_rounds)
    record["last_failure_signature"] = str(last_failure_signature or "")
    ledger["tasks"][task_id] = record
    _save_ledger(project, ledger)


def _make_repair_context(
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    request_id: str,
    failure_signature: str,
    evidence: list[str],
    candidate_digests: Mapping[str, str],
    preimage_digests: Mapping[str, str],
) -> dict[str, Any]:
    body = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "attempt": attempt,
        "request_id": request_id,
        "failure_signature": failure_signature,
        "evidence": [sanitize_text(item) for item in evidence if isinstance(item, str)],
        "candidate_digests": dict(candidate_digests),
        "preimage_digests": dict(preimage_digests),
    }
    return {**body, "context_digest": sha256_bytes(canonical_json(body))}


def _load_repair_evidence(
    record: Mapping[str, Any], run_id: str, task_id: str
) -> tuple[list[str] | None, str | None]:
    context = record.get("repair_context")
    fields = {
        "schema_version", "run_id", "task_id", "attempt", "request_id",
        "failure_signature", "evidence", "candidate_digests",
        "preimage_digests", "context_digest",
    }
    if not isinstance(context, Mapping) or set(context) != fields:
        return None, "repair_context_missing_or_malformed"
    body = {key: context[key] for key in fields if key != "context_digest"}
    if (
        context.get("schema_version") != "1.0"
        or context.get("run_id") != run_id
        or context.get("task_id") != task_id
        or type(context.get("attempt")) is not int
        or context["attempt"] < 1
        or not isinstance(context.get("request_id"), str)
        or not context["request_id"]
        or not isinstance(context.get("failure_signature"), str)
        or not isinstance(context.get("evidence"), list)
        or not context["evidence"]
        or any(not isinstance(item, str) or not item.strip() for item in context["evidence"])
        or not isinstance(context.get("candidate_digests"), Mapping)
        or not isinstance(context.get("preimage_digests"), Mapping)
    ):
        return None, "repair_context_binding_invalid"
    digest = sha256_bytes(canonical_json(body))
    if context.get("context_digest") != digest:
        return None, "repair_context_digest_mismatch"
    source_request = next(
        (
            item for item in record.get("requests", [])
            if isinstance(item, Mapping) and item.get("request_id") == context["request_id"]
        ),
        None,
    )
    source_attempt = next(
        (
            item for item in record.get("attempts", [])
            if isinstance(item, Mapping)
            and item.get("request_id") == context["request_id"]
            and item.get("attempt") == context["attempt"]
        ),
        None,
    )
    if (
        not isinstance(source_request, Mapping)
        or source_request.get("task_id") != task_id
        or source_request.get("attempt") != context["attempt"]
        or source_request.get("stage") != "result_received"
        or not isinstance(source_attempt, Mapping)
        or source_attempt.get("verification_ok") is not False
        or source_attempt.get("failure_signature") != context["failure_signature"]
        or source_attempt.get("candidate_digests") != context["candidate_digests"]
        or hashlib.sha256("\n".join(context["evidence"]).encode("utf-8")).hexdigest()
        != context["failure_signature"]
    ):
        return None, "repair_context_source_mismatch"
    return list(context["evidence"]), None


def _mark_frozen(project: Path, ledger: dict[str, Any], states: dict[str, str], task: Mapping[str, Any], record: dict[str, Any], task_id: str, run_id: str, say, *, reason: str = "修复达到上限或无进展，冻结本任务与后继。") -> None:
    record["state"] = "frozen"
    states[task_id] = "frozen"
    ledger["tasks"][task_id] = record
    _save_ledger(project, ledger)
    uxtext.record_interruption(
        project,
        category="system_error",
        action=f"任务 {task_id}",
        reason=reason,
        auto_path="保留初次失败证据；可人工分析后重开新批次。",
    )
    say(uxtext.render_pause(
        f"任务「{task['title']}」已冻结。",
        "保留初次失败证据；独立任务继续。",
        "是否人工分析失败原因后重开新批次。",
    ))


def _find_transaction_receipt(project: Path, plan_digest: str) -> tuple[dict[str, Any], Path] | None:
    """按计划摘要定位既有事务回执；用于接续时核对已应用效果（S03/Q14）。"""

    base = project / ".opencoding" / "transactions"
    if not base.exists() or not plan_digest:
        return None
    for path in sorted(base.rglob("receipt.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("plan_digest") == plan_digest:
            return payload, path
    return None


def _adopt_interrupted_commit(
    project: Path,
    run_id: str,
    task: Mapping[str, Any],
    ledger: dict[str, Any],
    grant: Mapping[str, Any],
    verifier: Verifying,
    *,
    trusted_fixture: str | None = None,
) -> str | None:
    """接续时先核对中断的提交绑定：已应用的不重复请求，未落地的不冒认（S03/Q14）。"""

    directory = _binding_dir(project, run_id)
    task_id = task["task_id"]
    if not directory.exists():
        return None
    for path in sorted(directory.glob(f"{task_id}-*.binding.json")):
        try:
            binding = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(binding, dict) or binding.get("state") != "applying":
            continue
        attempt = int(binding.get("attempt", 1) or 1)
        files = [dict(item) for item in binding.get("files", []) if isinstance(item, dict)]
        found = _find_transaction_receipt(project, str(binding.get("plan_digest") or ""))
        existing = ledger["tasks"].get(task_id)
        record: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {"state": "running", "attempts": []}
        record["attempts"] = [item for item in record.get("attempts", []) if isinstance(item, dict)]
        if found is None:
            _finalize_commit_binding(path, state="abandoned", note="未找到事务回执，判定为未落盘")
            continue
        payload, receipt_path = found
        transaction_id = str(payload.get("transaction_id") or "")
        pseudo_result = {
            "status": str(payload.get("status")),
            "receipt_path": str(receipt_path),
            "changed_paths": list(payload.get("changed_paths", [])),
            "uncertain_paths": list(payload.get("uncertain_paths", [])),
        }
        if str(payload.get("status")) == "applied":
            _record_recovery(project, run_id, task_id, attempt, grant["grant_id"], transaction_id, files, pseudo_result)
            _finalize_commit_binding(path, state="committed", transaction_id=transaction_id, note="中断后按回执补登记")
            try:
                ok = _reverify_task(project, grant, task, verifier, run_id, record, trusted_fixture=trusted_fixture)
            except (AutorunError, grants.GrantError):
                ok = False
            if ok:
                record["state"] = "succeeded"
                record["file_hashes"] = _snapshot_files(project, list(task["outputs"]))
                record.setdefault("attempts", []).append({
                    "attempt": attempt,
                    "kind": "recovered",
                    "verification_ok": True,
                    "transaction_id": transaction_id,
                })
                _update_lineage(ledger, task_id, attempt, dict(record["file_hashes"]))
                ledger["tasks"][task_id] = record
                _save_ledger(project, ledger)
                return "succeeded"
            record["state"] = "frozen"
            record["reason"] = "recovered_commit_reverify_failed"
        else:
            rollback = _rollback_transaction(project, transaction_id or None)
            _record_pending_recovery(project, run_id, task_id, attempt, grant["grant_id"], transaction_id or None, files, pseudo_result, rollback)
            _finalize_commit_binding(path, state="needs_recovery", transaction_id=transaction_id or None, note="中断提交未完整应用")
            record["state"] = "frozen"
            record["reason"] = "interrupted_commit_partial"
        ledger["tasks"][task_id] = record
        _save_ledger(project, ledger)
        uxtext.record_interruption(
            project,
            category="system_error",
            action=f"任务 {task_id}",
            reason="接续时发现中断的提交，已按事务回执处理：状态 " + str(payload.get("status")),
            auto_path="账本保留事务编号与待核对路径；人工核对后重开本任务。",
        )
        return "frozen"
    return None


def run_binding_status(root: str | Path, run_id: str) -> dict[str, Any]:
    """区分“没有运行”与“运行已存在但损坏/缺绑定”；后者不得换发授权（S03/Q17）。"""

    project = Path(root).resolve()
    path = _ledger_path(project, run_id)
    if not path.exists():
        return {"exists": False, "grant_id": None, "state": "no_run", "reason": None}
    try:
        ledger = _load_ledger(project, run_id)
    except AutorunError as exc:
        return {"exists": True, "grant_id": None, "state": "corrupt", "reason": exc.code}
    value = ledger.get("grant_id")
    if isinstance(value, str) and value.startswith("grant-"):
        return {"exists": True, "grant_id": value, "state": "bound", "reason": None}
    return {"exists": True, "grant_id": None, "state": "unbound", "reason": "ledger_missing_grant_binding"}


def run_batch(
    root: str | Path,
    scenario: Mapping[str, Any],
    *,
    grant_id: str,
    adapter,
    run_id: str | None = None,
    progress: Callable[[str], None] | None = None,
    resume: bool = True,
    trusted_fixture: str | None = None,
    adopted_plan_digest: str | None = None,
) -> dict[str, Any]:
    """在有效批次授权内连续推进任务；每步有凭据、产物与独立验收证据。

    N01-a：`trusted_fixture` 仅由已审查测试代码显式传入并在 `REVIEWED_FIXTURES`
    中登记后，才允许在无实际受限执行环境时执行固定合成候选；普通 CLI 与真实
    模型路径不传该参数，永不因环境变量获得豁免。
    """

    validate_scenario(scenario)
    verifiers: Mapping[str, Verifying] = scenario["verifiers"]
    project = Path(root).resolve()
    _validate_adopted_plan(project, adopted_plan_digest, scenario)
    # F02：派发/接续前核对采用时的有效输入是否仍然有效，漂移即停止旧计划派发。
    _check_adoption_input_drift(project, adopted_plan_digest, scenario)
    grant = grants.load_grant(project, grant_id)
    valid, reason = grants.grant_valid(grant)
    if not valid:
        raise AutorunError(reason, "批次授权不可用：" + reason)
    run_id = run_id or ("run-" + uuid.uuid4().hex)
    tasks: list[Mapping[str, Any]] = scenario["tasks"]
    scenario_digest = sha256_bytes(canonical_json({"goal": scenario.get("goal", ""), "tasks": [dict(item) for item in tasks]}))
    states: dict[str, str] = {task["task_id"]: "pending" for task in tasks}
    ledger = _load_ledger(project, run_id) if resume else {"schema_version": AUTORUN_SCHEMA_VERSION, "run_id": run_id, "started_at": _now(), "tasks": {}}
    # F05：运行编号与批次授权、任务图持久绑定；接续必须同源，不重新授权。
    _bind_run(ledger, grant_id, scenario_digest)
    if ledger.get("adopted_plan_digest") and ledger["adopted_plan_digest"] != adopted_plan_digest:
        raise AutorunError("run_adopted_plan_mismatch", "该运行已绑定其他采用计划；接续必须使用原计划")
    ledger["adopted_plan_digest"] = adopted_plan_digest
    say = progress or (lambda _text: None)
    if ledger.get("cancel_requested") or _cancel_requested(project, run_id):
        return _cancel_pending(project, ledger, states, tasks, run_id, say)

    for task in tasks:
        task_id = task["task_id"]
        record = ledger["tasks"].get(task_id)
        if not record or not resume:
            continue
        state = record.get("state")
        if state == "succeeded":
            verdict = _resume_verdict(project, ledger, task, record)
            if verdict in {"consistent", "successor"}:
                states[task_id] = "succeeded"
                if verdict == "successor":
                    say(uxtext.render_progress(
                        f"任务「{task['title']}」此前已验收；其产物被本运行的合法后继任务更新，血缘一致，直接接续。",
                        "按文件血缘识别正常后继版本，不误判为用户修改。",
                        f"已完成 {sum(1 for s in states.values() if s == 'succeeded')}/{len(tasks)} 项。",
                        "检查下一项任务的依赖。",
                        "暂无",
                    ))
            elif verdict == "receipt_invalid":
                # F06/R12：残缺成功记录不自动通过；先重新核实（不请求 AI）。
                ok = _reverify_task(project, grant, task, verifiers[task_id], run_id, record, trusted_fixture=trusted_fixture)
                if ok:
                    states[task_id] = "succeeded"
                    ledger["tasks"][task_id] = record
                    _save_ledger(project, ledger)
                    say(uxtext.render_progress(
                        f"任务「{task['title']}」的账本记录缺少有效产物摘要，已重新核实通过。",
                        "补核实使用隔离工作副本，未请求 AI、未改写历史。",
                        f"已完成 {sum(1 for s in states.values() if s == 'succeeded')}/{len(tasks)} 项。",
                        "检查下一项任务的依赖。",
                        "暂无",
                    ))
                else:
                    states[task_id] = "frozen"
                    record["state"] = "frozen"
                    record["reason"] = "receipt_invalid"
                    ledger["tasks"][task_id] = record
                    _save_ledger(project, ledger)
                    uxtext.record_interruption(
                        project,
                        category="system_error",
                        action=f"任务 {task_id}",
                        reason="成功记录缺少有效产物摘要，重新核实未通过。",
                        auto_path="不自动通过；人工核对该任务产物后重开本任务。",
                    )
            else:
                states[task_id] = "frozen"
                record["state"] = "frozen"
                record["reason"] = "user_modification_detected"
                ledger["tasks"][task_id] = record
                _save_ledger(project, ledger)
                uxtext.record_interruption(
                    project,
                    category="system_error",
                    action=f"任务 {task_id}",
                    reason="已验收任务的文件被用户或其他进程修改，摘要不一致。",
                    auto_path="不自动覆盖；人工核对该文件后重开本任务。",
                )
        elif state in {"frozen", "cancelled"}:
            # F05/R06：冻结与取消不自动复活；尝试历史保留在账本中。
            states[task_id] = state

    pending = [task for task in tasks if states[task["task_id"]] == "pending"]
    for task in pending:
        ledger = _load_ledger(project, run_id)
        # 首次运行时磁盘账本可能尚不存在；重新核对并补齐绑定，防止写回丢失（F05）。
        _bind_run(ledger, grant_id, scenario_digest)
        ledger["adopted_plan_digest"] = adopted_plan_digest
        if ledger.get("cancel_requested") or _cancel_requested(project, run_id):
            return _cancel_pending(project, ledger, states, tasks, run_id, say)
        task_id = task["task_id"]
        if any(states[dep] != "succeeded" for dep in task["depends_on"]):
            states[task_id] = "frozen"
            existing = ledger["tasks"].get(task_id)
            if isinstance(existing, dict):
                existing["state"] = "frozen"
                existing["reason"] = "dependency_not_succeeded"
                ledger["tasks"][task_id] = existing
            else:
                ledger["tasks"][task_id] = {"state": "frozen", "reason": "dependency_not_succeeded", "attempts": []}
            _save_ledger(project, ledger)
            uxtext.record_interruption(
                project,
                category="system_error",
                action=f"任务 {task_id}",
                reason="依赖任务未成功，暂停本任务。",
                auto_path="依赖修复后可从账本接续。",
            )
            say(uxtext.render_pause(f"任务「{task['title']}」的依赖未成功。", "先完成不依赖它的独立任务。", "暂无"))
            continue

        # S03/Q14：接续先核对中断的提交绑定；已应用的效果不盲重放，未落地的不冒认。
        adopted = _adopt_interrupted_commit(
            project,
            run_id,
            task,
            ledger,
            grant,
            verifiers[task_id],
            trusted_fixture=trusted_fixture,
        )
        if adopted == "succeeded":
            states[task_id] = "succeeded"
            say(uxtext.render_progress(
                f"任务「{task['title']}」在中断前已实际落盘，已按事务回执补登记恢复入口并通过隔离复验。",
                "核对既有事务回执，不重新请求 AI、不重放写入。",
                f"已完成 {sum(1 for s in states.values() if s == 'succeeded')}/{len(tasks)} 项。",
                "检查下一项任务的依赖。",
                "暂无",
            ))
            continue
        if adopted == "frozen":
            states[task_id] = "frozen"
            say(uxtext.render_pause(
                f"任务「{task['title']}」存在中断的提交，已按事务回执处理并冻结。",
                "未自动重放写入；账本保留了事务编号与待核对路径。",
                "是否人工核对后重开本任务。",
            ))
            continue

        say(uxtext.render_progress(
            f"开始任务「{task['title']}」：请求 AI 产出候选实现。",
            "任务依赖已满足，批次授权有效。",
            f"已完成 {sum(1 for s in states.values() if s == 'succeeded')}/{len(tasks)} 项。",
            f"执行 {task_id}。",
            "暂无",
        ))
        existing = ledger["tasks"].get(task_id)
        record: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {"state": "running", "attempts": []}
        prior_attempts = [item for item in record.get("attempts", []) if isinstance(item, dict)]
        record["state"] = "running"
        record["attempts"] = list(prior_attempts)
        # F05/R06：接续保留尝试历史；尝试与修复计数按逻辑任务累计，不重置。
        # N02-b/P14：尝试号取完整请求与尝试历史的最大值递增，重启/改名不获得额外次数。
        prior_max = max([int(item.get("attempt", 0)) for item in prior_attempts if isinstance(item, dict)] + [0])
        request_max = max([int(item.get("attempt", 0)) for item in record.get("requests", []) if isinstance(item, dict)] + [0])
        attempt = max(prior_max, request_max) + 1
        repairs = int(record.get("repairs", sum(1 for item in prior_attempts if item.get("kind") == "repair")) or 0)
        no_progress_rounds = int(record.get("no_progress_rounds", 0) or 0)
        last_failure_signature = str(record.get("last_failure_signature") or (prior_attempts[-1].get("failure_signature", "") if prior_attempts else ""))
        record["repairs"] = repairs
        record["no_progress_rounds"] = no_progress_rounds
        record["last_failure_signature"] = last_failure_signature
        repair_evidence: list[str] | None = None
        if repairs:
            repair_evidence, repair_error = _load_repair_evidence(record, run_id, task_id)
            if repair_error:
                record["state"] = "frozen"
                record["reason"] = repair_error
                states[task_id] = "frozen"
                ledger["tasks"][task_id] = record
                _save_ledger(project, ledger)
                _mark_frozen(
                    project, ledger, states, task, record, task_id, run_id, say,
                    reason="修复上下文无法核实（" + repair_error + "）；不预留预算、不派发新请求。",
                )
                continue
        ledger["tasks"][task_id] = record
        _save_ledger(project, ledger)
        # S03/C04：先判断是否允许继续（取消、修复与无进展上限），再签发请求凭据。
        # 请求凭据签发即预扣 ai_requests 预算，因此达到上限或已取消的接续
        # 不得签发新凭据，避免预算虚耗与账本不一致。
        if _cancel_requested(project, run_id):
            record["state"] = "cancelled"
            record["reason"] = "cancel_requested"
            states[task_id] = "cancelled"
            ledger["tasks"][task_id] = record
            _save_ledger(project, ledger)
            return _cancel_pending(project, ledger, states, tasks, run_id, say)
        # N01：未知候选的执行能力准入。辅助拦截可用 ≠ 具备实际执行边界；
        # 没有实际受限执行环境时冻结本任务，不允许继续自动执行未知候选。
        capability = sandbox.execution_capability()
        ledger["execution_capability"] = capability
        record["execution_capability"] = capability
        trusted_fixture_ok = trusted_fixture in sandbox.REVIEWED_FIXTURES
        if not capability["available"] and not trusted_fixture_ok:
            record["state"] = "frozen"
            states[task_id] = "frozen"
            ledger["tasks"][task_id] = record
            _save_ledger(project, ledger)
            _mark_frozen(
                project, ledger, states, task, record, task_id, run_id, say,
                reason="未知候选的自动执行不可用（" + str(capability["kind"]) + "）：" + str(capability["reason"]),
            )
            continue
        # S03/Q13：接续时预算与修复次数按持久记录判定；已达上限不再发起新的 AI 请求。
        if repairs >= MAX_REPAIR_ROUNDS or no_progress_rounds >= 2:
            _mark_frozen(project, ledger, states, task, record, task_id, run_id, say, reason="接续时修复次数或无进展已达上限，冻结本任务与后继。")
            continue
        # N02：接续必须先处理"已派发但结果未知"的旧请求——
        # 能确认结果则采用；无法确认则保留未知并冻结，不默认为"没发过"、不静默重发。
        credential: dict[str, Any] | None = None
        adopted: dict[str, Any] | None = None
        open_request = _last_open_request(record)
        if open_request is not None:
            request_id_open = str(open_request.get("request_id") or "")
            resolved: dict[str, Any] | None = None
            resolution_note = ""
            resolution_error = ""
            if str(open_request.get("stage")) == "result_received":
                # N02-b：响应已可靠保存（含完整结果与摘要）→ 优先消费原结果，不新增请求。
                # 本地保存的响应绑定关系由本条目承载，合成绑定字段后走同一校验入口。
                try:
                    payload = json.loads(str(open_request.get("result_payload") or ""))
                    stored_digest = str(open_request.get("response_digest") or "")
                    actual_digest = sha256_bytes(canonical_json(payload))
                    if not stored_digest or actual_digest != stored_digest:
                        raise ValueError("saved_response_digest_mismatch")
                    resolved = payload
                    resolution_note = "from_saved_response"
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    resolved = None
                    resolution_error = str(exc) or "saved_response_invalid"
            if resolved is None:
                if resolution_error:
                    candidate = None
                else:
                    candidate = _try_resolve_open_request(adapter, open_request)
                    if candidate is not None:
                        resolved = candidate
                        resolution_note = "from_resume_query"
            if resolved is not None:
                ok, reason = _validate_recovered_result(project, run_id, task, open_request, resolved)
                if ok:
                    attempt = int(open_request["attempt"])
                    if resolution_note == "from_resume_query":
                        # 查询到的完整响应先写入账本，再宣称已收到，避免查询后崩溃丢失结果。
                        payload_bytes = canonical_json(resolved)
                        _mark_request_stage(
                            project,
                            ledger,
                            record,
                            task_id,
                            request_id_open,
                            "result_received",
                            response_digest=sha256_bytes(payload_bytes),
                            result_payload=payload_bytes.decode("utf-8"),
                            nonce=str(resolved.get("nonce") or ""),
                            resolved_by=resolution_note,
                        )
                    _mark_request_stage(
                        project, ledger, record, task_id, request_id_open,
                        "result_received",
                        **({} if resolution_note == "from_resume_query" else {"resolved_by": resolution_note}),
                    )
                    adopted = resolved
                    # 沿用旧请求的凭据编号记账（不重新签发、不重复消费、不新增预算）。
                    credential = {
                        "credential_id": str(open_request.get("credential_id") or ""),
                        "adopted_from_request": request_id_open,
                    }
                else:
                    if resolution_error:
                        reason = "saved_response_invalid:" + resolution_error
                    # N02-a：错运行/错任务/错尝试/错摘要或前像漂移 → 拒绝采用，保留原记录。
                    _mark_request_stage(
                        project, ledger, record, task_id, request_id_open,
                        "unknown", rejected_reason=reason,
                    )
                    _mark_frozen(
                        project, ledger, states, task, record, task_id, run_id, say,
                        reason="恢复结果未通过绑定核对（" + reason + "）：保留用户文件与原记录，不自动应用。",
                    )
                    continue
            elif resolution_error:
                reason = "saved_response_invalid:" + resolution_error
                _mark_request_stage(
                    project, ledger, record, task_id, request_id_open,
                    "unknown", rejected_reason=reason,
                )
                _mark_frozen(
                    project, ledger, states, task, record, task_id, run_id, say,
                    reason="恢复结果未通过完整性核对（" + reason + "）：保留用户文件与原记录，不自动应用。",
                )
                continue
            elif str(record.get("retry_policy") or "") == "allow_new_attempt":
                # 明确的重试策略覆盖：保留原记录、换新请求编号与尝试号，预算由签发环节核对。
                _mark_request_stage(
                    project, ledger, record, task_id, request_id_open,
                    "unknown_retried", note="按明确重试策略发起新请求，原记录保留",
                )
            else:
                _mark_request_stage(
                    project, ledger, record, task_id, request_id_open,
                    "unknown", note="已派发但结果未知，未确认前不重发、不释放预算",
                )
                _mark_frozen(
                    project, ledger, states, task, record, task_id, run_id, say,
                    reason="存在未核实的请求（已派发但结果未知）：保留未知状态，不自动重发，需人工确认或显式重试策略。",
                )
                continue
        if adopted is None:
            # F02/X03：每次派发前核对采用输入；漂移即结构化冻结本任务，
            # 不签发凭据、不预扣预算，后续任务在各自派发前同样核对并冻结。
            drift_reason = _adoption_input_drift_reason(project, ledger, scenario)
            if drift_reason:
                record["state"] = "frozen"
                record["reason"] = "adopted_plan_input_drift"
                states[task_id] = "frozen"
                ledger["tasks"][task_id] = record
                _save_ledger(project, ledger)
                _mark_frozen(
                    project, ledger, states, task, record, task_id, run_id, say,
                    reason="采用时的有效输入已变化，暂停旧计划派发：" + drift_reason,
                )
                continue
            protocol_kind = "repair" if repairs and getattr(adapter, "real", False) else "implement"
            try:
                prepared_request = _prepare_implementation_request(
                    project,
                    scenario,
                    task,
                    run_id,
                    attempt,
                    grant_id,
                    request_kind=protocol_kind,
                    repair_evidence=repair_evidence if repairs else None,
                )
            except (AutorunError, grants.GrantError) as exc:
                record["state"] = "frozen"
                record["error_code"] = getattr(exc, "code", "request_context_invalid")
                states[task_id] = "frozen"
                ledger["tasks"][task_id] = record
                _save_ledger(project, ledger)
                _mark_frozen(
                    project, ledger, states, task, record, task_id, run_id, say,
                    reason="请求准备未通过（" + sanitize_text(str(exc))[:200] + "）；未预留 AI 预算。",
                )
                continue
            credential = grants.issue_step_credential(
                project, grant_id,
                task_id=task_id, attempt=attempt, action_kind="ai_request",
                targets=list(task["outputs"]),
                action_digest=sha256_bytes(canonical_json({"task": task_id, "kind": "ai_request"})),
                input_digest=prepared_request["input_digest"],
            )
            # S03/C04：请求凭据即预算预留；记录阶段，不把预留展示为实际成功请求。
            _append_request(project, ledger, record, task_id, {
                "request_id": prepared_request["request_id"],
                "task_id": task_id,
                "attempt": attempt,
                "schema_version": ADAPTER_SCHEMA_VERSION,
                "nonce": prepared_request["nonce"],
                "allowed_outputs": prepared_request["allowed_outputs"],
                "kind": prepared_request["request_kind"],
                "credential_id": credential["credential_id"],
                "input_digest": prepared_request["input_digest"],
                "preimage_digests": prepared_request["preimage_digests"],
                "source_digests": prepared_request["source_digests"],
                "remaining_budget": prepared_request["remaining_budget"],
                "stage": "reserved",
                "at": _now(),
            })
        else:
            prepared_request = None
        while True:
            try:
                request_snapshot = (
                    _snapshot_bytes(project, list(task["outputs"]))
                    if adopted is not None
                    else dict(prepared_request["preimage_digests"])
                )
                if adopted is not None:
                    result = adopted
                    adopted = None
                else:
                    grants.check_step_credential(
                        project, credential["credential_id"],
                        expect_task_id=task_id, consume=True,
                    )
                    # N02：派发意图先落盘，再调用适配器（承认落盘与发送之间仍有不确定窗口）。
                    dispatch_id = str(record.get("current_request_id") or request_id)
                    _mark_request_stage(
                        project, ledger, record, task_id, dispatch_id, "dispatched",
                        dispatched_at=_now(), attempt=attempt,
                        preimage_digests=dict(request_snapshot),
                    )
                    result = _request_implementation(
                        adapter,
                        task,
                        run_id,
                        attempt,
                        prepared=prepared_request,
                    )
                    # 持久化完整请求身份；恢复时只接受本体中的绑定，不从当前状态补造。
                    result = {
                        **result,
                        "bound_request_id": dispatch_id,
                        "bound_run_id": str(run_id),
                        "bound_task_id": str(task_id),
                        "bound_attempt": attempt,
                        "bound_input_digest": str(record["requests"][-1].get("input_digest") or ""),
                    }
                    valid_response, response_reason = _validate_recovered_result(
                        project, run_id, task, record["requests"][-1], result
                    )
                    if not valid_response:
                        raise AIRequestError("response_contract_invalid", response_reason)
                    # N02-a：响应先完整落盘（载荷+摘要+随机标识），再进入验收/应用。
                    _mark_request_stage(
                        project, ledger, record, task_id, dispatch_id, "result_received",
                        response_digest=sha256_bytes(canonical_json(result)),
                        result_payload=canonical_json(result).decode("utf-8"),
                        nonce=str(result.get("nonce") or ""),
                    )
                # F04 检查点一：接收模型结果后立即重查取消与授权；已取消的结果不落地。
                if _cancel_requested(project, run_id):
                    record["state"] = "cancelled"
                    record["reason"] = "cancel_requested"
                    states[task_id] = "cancelled"
                    ledger["tasks"][task_id] = record
                    _save_ledger(project, ledger)
                    return _cancel_pending(project, ledger, states, tasks, run_id, say)
                _require_grant_active(project, grant_id)
                try:
                    ok, evidence, candidate_digests = _stage_and_verify(project, grant, task, result["structured"]["files"], run_id, attempt, verifiers[task_id], trusted_fixture=trusted_fixture)
                except (AutorunError, grants.GrantError):
                    record["attempts"].append({
                        "attempt": attempt,
                        "request_id": dispatch_id,
                        "kind": "repair" if repairs else "implement",
                        "nonce": result["nonce"],
                        "ai_credential_id": credential["credential_id"],
                        "file_hashes": {},
                        "verification_ok": False,
                        "verification_evidence": [sanitize_text(str(result.get("summary", "")))[:200]],
                        "response_sha256": result["response_sha256"],
                        "error_code": "rejected_before_verification",
                    })
                    # S03/Q13：失败尝试先落盘再进入下一动作；崩溃后仍可追溯。
                    _persist_attempt(project, ledger, task_id, record, repairs, no_progress_rounds, last_failure_signature)
                    raise
                record["attempts"].append({
                    "attempt": attempt,
                    "request_id": str(record.get("current_request_id") or ""),
                    "kind": "repair" if repairs else "implement",
                    "nonce": result["nonce"],
                    "ai_credential_id": credential["credential_id"],
                    "file_hashes": {},
                    "candidate_digests": candidate_digests,
                    "verification_ok": ok,
                    "verification_evidence": evidence,
                    "response_sha256": result["response_sha256"],
                })
                _persist_attempt(project, ledger, task_id, record, repairs, no_progress_rounds, last_failure_signature)
                if ok:
                    # F04 检查点二：应用候选前重查取消与授权。
                    if _cancel_requested(project, run_id):
                        record["state"] = "cancelled"
                        record["reason"] = "cancel_requested"
                        states[task_id] = "cancelled"
                        ledger["tasks"][task_id] = record
                        _save_ledger(project, ledger)
                        return _cancel_pending(project, ledger, states, tasks, run_id, say)
                    # F02/X03：收结果后、提交前再次核对采用输入；漂移即结果不落地，
                    # 本任务结构化冻结，后继任务在各自派发前同样核对并冻结。
                    drift_reason = _adoption_input_drift_reason(project, ledger, scenario)
                    if drift_reason:
                        record["attempts"][-1]["committed"] = False
                        record["attempts"][-1]["error_code"] = "adopted_plan_input_drift"
                        record["state"] = "frozen"
                        record["reason"] = "adopted_plan_input_drift"
                        states[task_id] = "frozen"
                        ledger["tasks"][task_id] = record
                        _save_ledger(project, ledger)
                        _mark_frozen(
                            project, ledger, states, task, record, task_id, run_id, say,
                            reason="收结果后核对发现采用输入已变化，候选不提交：" + drift_reason,
                        )
                        break
                    try:
                        written = _commit_candidate(project, grant, task, result["structured"]["files"], run_id, task_id, attempt, expected=request_snapshot)
                    except (AutorunError, grants.GrantError) as commit_error:
                        record["attempts"][-1]["committed"] = False
                        record["attempts"][-1]["error_code"] = getattr(commit_error, "code", "unknown")
                        raise
                    record["attempts"][-1]["file_hashes"] = written
                    record["attempts"][-1]["committed"] = True
                    record["state"] = "succeeded"
                    record["file_hashes"] = written
                    applied_request_id = str(result.get("request_id") or "")
                    for request_entry in record.get("requests", []):
                        if (
                            isinstance(request_entry, dict)
                            and request_entry.get("request_id") == applied_request_id
                        ):
                            # Keep result_received as the durable response fact. The
                            # application is a separate state and must not erase the
                            # zero-redelivery recovery path.
                            request_entry["response_applied"] = True
                            request_entry["applied_at"] = _now()
                    request_stage = record.get("request_stage")
                    if isinstance(request_stage, dict) and request_stage.get("request_id") == applied_request_id:
                        request_stage["response_applied"] = True
                        request_stage["applied_at"] = _now()
                    states[task_id] = "succeeded"
                    _update_lineage(ledger, task_id, attempt, written)
                    ledger["tasks"][task_id] = record
                    _save_ledger(project, ledger)
                    say(uxtext.render_progress(
                        f"任务「{task['title']}」已验收通过，候选经隔离验收后以受控事务提交。",
                        "独立验收器在隔离工作副本运行真实命令并核对持久化结果。",
                        f"已完成 {sum(1 for s in states.values() if s == 'succeeded')}/{len(tasks)} 项。",
                        "自动选择下一项依赖已满足的任务。",
                        "暂无",
                    ))
                    break
                signature = hashlib.sha256("\n".join(evidence).encode("utf-8")).hexdigest()
                record["attempts"][-1]["failure_signature"] = signature
                if signature == last_failure_signature:
                    no_progress_rounds += 1
                    grants.consume_budget_counter(project, grant_id, counter="no_progress_rounds")
                else:
                    no_progress_rounds = 0
                last_failure_signature = signature
                failed_attempt = record["attempts"][-1]
                record["repair_context"] = _make_repair_context(
                    run_id=run_id,
                    task_id=task_id,
                    attempt=attempt,
                    request_id=str(failed_attempt.get("request_id") or ""),
                    failure_signature=signature,
                    evidence=evidence,
                    candidate_digests=failed_attempt.get("candidate_digests", {}),
                    preimage_digests=request_snapshot,
                )
                repair_evidence = list(record["repair_context"]["evidence"])
                _persist_attempt(project, ledger, task_id, record, repairs, no_progress_rounds, last_failure_signature)
                if repairs >= MAX_REPAIR_ROUNDS or no_progress_rounds >= 2:
                    _mark_frozen(project, ledger, states, task, record, task_id, run_id, say)
                    break
                # F02/r8-Y04：修复计数与预算预留**之前**先核对采用输入；漂移即
                # 结构化冻结——不递增修复轮数、不消费修复预算、不派发新请求。
                # 旧实现先 repairs += 1 并 consume 再核对，未派发的修复被错误
                # 计入修复额度（审核 Y04）。
                drift_reason = _adoption_input_drift_reason(project, ledger, scenario)
                if drift_reason:
                    record["state"] = "frozen"
                    record["reason"] = "adopted_plan_input_drift"
                    states[task_id] = "frozen"
                    ledger["tasks"][task_id] = record
                    _save_ledger(project, ledger)
                    _mark_frozen(
                        project, ledger, states, task, record, task_id, run_id, say,
                        reason="修复准备前核对发现采用输入已变化：" + drift_reason,
                    )
                    break
                repairs += 1
                attempt += 1
                # S03：计数在下一次请求前落盘，重启不靠内存变量重建。
                _persist_attempt(project, ledger, task_id, record, repairs, no_progress_rounds, last_failure_signature)
                grants.consume_budget_counter(project, grant_id, counter="repair_rounds")
                protocol_kind = "repair" if getattr(adapter, "real", False) else "implement"
                prepared_request = _prepare_implementation_request(
                    project,
                    scenario,
                    task,
                    run_id,
                    attempt,
                    grant_id,
                    request_kind=protocol_kind,
                    repair_evidence=repair_evidence,
                )
                credential = grants.issue_step_credential(
                    project, grant_id,
                    task_id=task_id, attempt=attempt, action_kind="ai_request",
                    targets=list(task["outputs"]),
                    action_digest=sha256_bytes(canonical_json({"task": task_id, "kind": "ai_request", "repair": repairs})),
                    input_digest=prepared_request["input_digest"],
                )
                # N02：修复请求与实现请求复用同一套持久化流程（此处仅登记预留，
                # 真正的"已派发"在调用适配器前一刻标记，避免把预留写成已派发）。
                _append_request(project, ledger, record, task_id, {
                    "request_id": prepared_request["request_id"],
                    "task_id": task_id,
                    "attempt": attempt,
                    "schema_version": ADAPTER_SCHEMA_VERSION,
                    "nonce": prepared_request["nonce"],
                    "allowed_outputs": prepared_request["allowed_outputs"],
                    "kind": prepared_request["request_kind"],
                    "credential_id": credential["credential_id"],
                    "input_digest": prepared_request["input_digest"],
                    "preimage_digests": prepared_request["preimage_digests"],
                    "source_digests": prepared_request["source_digests"],
                    "remaining_budget": prepared_request["remaining_budget"],
                    "stage": "reserved",
                    "at": _now(),
                })
            except (AIRequestError, grants.GrantError, AutorunError) as exc:
                record["state"] = "frozen"
                record["error_code"] = getattr(exc, "code", "unknown")
                states[task_id] = "frozen"
                ledger["tasks"][task_id] = record
                _save_ledger(project, ledger)
                code = getattr(exc, "code", "unknown")
                if code == "step_budget_exhausted":
                    uxtext.record_interruption(
                        project,
                        category="new_authorization",
                        action=f"任务 {task_id}",
                        reason="本批次 AI 请求预算已用完。",
                        auto_path="暂停批次；扩大预算需用户重新确认。",
                        new_risk="继续请求将超出已确认预算。",
                    )
                    say(uxtext.render_pause("本批次 AI 请求预算已用完。", "暂停批次；完成已验收任务即可交付部分结果。", "是否确认扩大预算。"))
                    return _finish(project, ledger, states, tasks, run_id)
                uxtext.record_interruption(
                    project,
                    category="system_error",
                    action=f"任务 {task_id}",
                    reason="请求或校验失败：" + sanitize_text(str(exc))[:200],
                    reason_code=code,
                    auto_path="保留证据；其余独立任务可继续。",
                )
                say(uxtext.render_pause(f"任务「{task['title']}」异常：" + sanitize_text(str(exc))[:120], "继续执行不依赖它的任务。", "暂无"))
                break

    return _finish_with_overall_check(project, ledger, states, tasks, run_id, grant, verifiers, say, trusted_fixture=trusted_fixture)


def _finish(project: Path, ledger: dict[str, Any], states: dict[str, str], tasks: list[Mapping[str, Any]], run_id: str, *, final_verification: Mapping[str, str] | None = None, regressed: list[str] | None = None, final_states: Mapping[str, str] | None = None) -> dict[str, Any]:
    ledger["finished_at"] = _now()
    regressed = list(regressed or [])
    summary = {
        "schema_version": AUTORUN_SCHEMA_VERSION,
        "run_id": run_id,
        "succeeded": sum(1 for state in states.values() if state == "succeeded"),
        "frozen": sum(1 for state in states.values() if state == "frozen"),
        "cancelled": sum(1 for state in states.values() if state == "cancelled"),
        "total": len(tasks),
        "states": {task["task_id"]: states[task["task_id"]] for task in tasks},
        "ledger_path": str(_ledger_path(project, run_id)),
        # N01：能力状态必须可见——辅助拦截不等于实际执行边界，不得冒充业务成功。
        "execution_capability": sandbox.execution_capability(),
    }
    # 历史证据保留：曾经验收通过的任务单独统计，不因最终回归改写成“从未通过”。
    summary["execution_succeeded"] = sum(
        1
        for task in tasks
        if str(ledger.get("tasks", {}).get(task["task_id"], {}).get("state")) in {"succeeded", "regressed"}
    )
    if final_verification is not None:
        summary["final_verification"] = dict(final_verification)
        summary["regressed"] = regressed
        summary["final_states"] = {
            task["task_id"]: dict(final_states or {}).get(task["task_id"], states[task["task_id"]])
            for task in tasks
        }
    blockers: list[str] = []
    if summary["frozen"]:
        blockers.append("存在被冻结的任务：" + str(summary["frozen"]))
    if summary["cancelled"]:
        blockers.append("批次被取消：" + str(summary["cancelled"]))
    if regressed:
        blockers.append("最终验收发现回归：" + ",".join(regressed))
    summary["delivery_blockers"] = blockers
    # S05/Q11：可交付必须同时满足无冻结、无取消、无最终回归。
    summary["deliverable"] = not blockers
    ledger["summary"] = summary
    _save_ledger(project, ledger)
    return summary


def _append_request(project: Path, ledger: dict[str, Any], record: dict[str, Any], task_id: str, entry: dict[str, Any]) -> None:
    """N02：为一次真实请求追加一条持久记录（预留/派发/已收到/未知）。"""

    requests = [item for item in record.get("requests", []) if isinstance(item, dict)]
    requests.append(entry)
    record["requests"] = requests
    record["current_request_id"] = entry.get("request_id")
    ledger["tasks"][task_id] = record
    _save_ledger(project, ledger)


# N02-b：全部非终态阶段统一处理，不再只匹配一个字符串。
OPEN_REQUEST_STAGES = ("dispatched", "result_received", "unknown")


def _last_open_request(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """取最近一条非终态请求（已派发/已收到未应用/未知）；没有则 None。"""

    for item in reversed([x for x in record.get("requests", []) if isinstance(x, dict)]):
        if str(item.get("stage")) in OPEN_REQUEST_STAGES:
            return dict(item)
    return None


def _validate_recovered_result(
    project: Path,
    run_id: str,
    task: Mapping[str, Any],
    request_entry: Mapping[str, Any],
    result: Mapping[str, Any],
) -> tuple[bool, str]:
    """N02-a：恢复响应的完整绑定核对；只返回非空对象不构成核实。

    校验：结构完整、请求编号/运行/任务/尝试/输入摘要绑定一致、
    以及"请求时前像"与当前文件一致（中断期间的用户新修改不归因于旧请求）。
    """

    if not isinstance(result, Mapping):
        return False, "recovery_not_mapping"
    structured = result.get("structured")
    if not isinstance(structured, Mapping):
        return False, "recovery_missing_structured"
    request_id = str(request_entry.get("request_id") or "")
    expected_attempt = request_entry.get("attempt")
    try:
        structured = validate_structured_payload(
            str(request_entry.get("kind", "implement")),
            structured,
            nonce=str(request_entry.get("nonce") or ""),
            run_id=str(run_id),
            task_id=str(task["task_id"]),
            attempt=expected_attempt,
            request_id=str(request_entry.get("request_id") or ""),
            input_digest=str(request_entry.get("input_digest") or ""),
            allowed_outputs=request_entry.get("allowed_outputs"),
        )
    except AIRequestError as exc:
        return False, "recovery_" + exc.code
    if (
        request_entry.get("schema_version") != ADAPTER_SCHEMA_VERSION
        or result.get("schema_version") != request_entry.get("schema_version")
    ):
        return False, "recovery_schema_mismatch"
    if (
        result.get("request_id") != request_id
        or result.get("run_id") != run_id
        or result.get("task_id") != task["task_id"]
        or type(result.get("attempt")) is not int
        or type(expected_attempt) is not int
        or result.get("attempt") != expected_attempt
    ):
        return False, "recovery_request_id_mismatch"
    if result.get("request_kind") != request_entry.get("kind", "implement"):
        return False, "recovery_request_kind_mismatch"
    if result.get("nonce") != request_entry.get("nonce") or structured.get("nonce") != request_entry.get("nonce"):
        return False, "recovery_nonce_mismatch"
    expected_outputs = request_entry.get("allowed_outputs")
    if not isinstance(expected_outputs, list) or not expected_outputs:
        return False, "recovery_allowed_outputs_missing"
    if structured.get("allowed_outputs") != expected_outputs:
        return False, "recovery_allowed_outputs_mismatch"
    if (
        structured.get("schema_version") != request_entry.get("schema_version")
        or structured.get("request_id") != request_id
        or structured.get("request_kind") != request_entry.get("kind", "implement")
        or structured.get("run_id") != run_id
        or structured.get("task_id") != task["task_id"]
        or type(structured.get("attempt")) is not int
        or structured.get("attempt") != expected_attempt
        or structured.get("input_digest") != request_entry.get("input_digest")
    ):
        return False, "recovery_inner_binding_mismatch"
    input_digest = request_entry.get("input_digest")
    if result.get("input_sha256") != input_digest:
        return False, "recovery_input_digest_mismatch"
    response_digest = result.get("response_sha256")
    if (
        not isinstance(response_digest, str)
        or len(response_digest) != 64
        or response_digest != sha256_bytes(canonical_json(structured))
    ):
        return False, "recovery_response_digest_mismatch"
    source_digests = request_entry.get("source_digests")
    if not isinstance(source_digests, Mapping):
        return False, "recovery_source_digests_missing"
    candidate_digests: dict[str, str] = {}
    for entry in structured.get("files", []):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("content"), str):
            return False, "recovery_candidate_unreadable"
        candidate_digests[entry["path"]] = sha256_bytes(entry["content"].encode("utf-8"))
    output_paths = set(expected_outputs)
    for relative, digest in source_digests.items():
        if (
            not isinstance(relative, str)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            return False, "recovery_source_digest_invalid"
        try:
            target = transactions.safe_target(project, relative, allow_missing=True)
        except (OSError, ValueError):
            return False, "recovery_source_path_invalid"
        current = sha256_bytes(target.read_bytes()) if target.is_file() else None
        allowed_current = {digest}
        if relative in output_paths and relative in candidate_digests:
            allowed_current.add(candidate_digests[relative])
        if current not in allowed_current:
            return False, "recovery_source_drifted:" + relative
    bound_request = result.get("bound_request_id")
    if not isinstance(bound_request, str) or not bound_request:
        return False, "recovery_binding_missing:bound_request_id"
    if bound_request != request_id:
        return False, "recovery_request_mismatch"
    bound_run = result.get("bound_run_id")
    if not isinstance(bound_run, str) or not bound_run:
        return False, "recovery_binding_missing:bound_run_id"
    if bound_run != str(run_id):
        return False, "recovery_run_mismatch"
    bound_task = result.get("bound_task_id")
    if not isinstance(bound_task, str) or not bound_task:
        return False, "recovery_binding_missing:bound_task_id"
    if bound_task != task["task_id"]:
        return False, "recovery_task_mismatch"
    bound_attempt = result.get("bound_attempt")
    if isinstance(bound_attempt, bool) or not isinstance(bound_attempt, int) or bound_attempt < 1:
        return False, "recovery_binding_missing:bound_attempt"
    if bound_attempt != int(request_entry.get("attempt", 0)):
        return False, "recovery_attempt_mismatch"
    bound_input = result.get("bound_input_digest")
    if not isinstance(bound_input, str) or not bound_input:
        return False, "recovery_binding_missing:bound_input_digest"
    stored_input = str(request_entry.get("input_digest") or "")
    if not stored_input or bound_input != stored_input:
        return False, "recovery_input_mismatch"
    preimage = request_entry.get("preimage_digests")
    if isinstance(preimage, Mapping) and preimage:
        current: dict[str, str | None] = {}
        for relative in preimage:
            try:
                target = transactions.safe_target(project, relative, allow_missing=True)
            except (OSError, ValueError):
                return False, "recovery_preimage_path_invalid"
            current[relative] = sha256_bytes(target.read_bytes()) if target.is_file() else None
        # 本结果对应的候选后像摘要：当前内容已等于候选后像 = 本结果已被应用，属正常路径。
        candidate_digests: dict[str, str] = {}
        for entry in (structured.get("files") or []):
            if isinstance(entry, Mapping) and entry.get("path") is not None:
                try:
                    candidate_digests[str(entry["path"])] = sha256_bytes(str(entry["content"]).encode("utf-8"))
                except (TypeError, ValueError):
                    return False, "recovery_candidate_unreadable"
        drifted = [
            path for path, digest in preimage.items()
            if current.get(path) != digest and current.get(path) != candidate_digests.get(path)
        ]
        if drifted:
            return False, "preimage_drifted_user_modified:" + ",".join(sorted(drifted))
    return True, "recovery_valid"


def _mark_request_stage(project: Path, ledger: dict[str, Any], record: dict[str, Any], task_id: str, request_id: str, stage: str, **extra: Any) -> None:
    for item in record.get("requests", []):
        if isinstance(item, dict) and str(item.get("request_id")) == request_id:
            item["stage"] = stage
            item.update(extra)
    record["request_stage"] = {
        "task_id": task_id,
        "request_id": request_id,
        "stage": stage,
        **extra,
    }
    ledger["tasks"][task_id] = record
    _save_ledger(project, ledger)


def _try_resolve_open_request(adapter: Any, request: Mapping[str, Any]) -> dict[str, Any] | None:
    """N02：尝试确认"已派发但结果未知"的旧请求；不能确认则返回 None。

    适配器若提供 `get_result(request_id)` 则用其确认；否则视为无法确认，
    **不默认为"没发过"**。
    """

    resolver = getattr(adapter, "get_result", None)
    if not callable(resolver):
        return None
    try:
        resolved = resolver(str(request.get("request_id")))
    except Exception:
        return None
    if isinstance(resolved, Mapping) and resolved:
        return dict(resolved)
    return None


def _finish_with_overall_check(project: Path, ledger: dict[str, Any], states: dict[str, str], tasks: list[Mapping[str, Any]], run_id: str, grant: Mapping[str, Any], verifiers: Mapping[str, Verifying], say, trusted_fixture: str | None = None) -> dict[str, Any]:
    """批次结束总体检查（F06/R16）：产物被后继修改过的已验收任务重跑隔离验收。"""
    final_verification: dict[str, str] = {}
    regressed: list[str] = []
    final_states: dict[str, str] = {}
    lineage = ledger.get("file_lineage", {})
    for task in tasks:
        task_id = task["task_id"]
        if states.get(task_id) == "succeeded":
            final_states[task_id] = "succeeded"
        if states.get(task_id) != "succeeded":
            continue
        outputs = list(task["outputs"])
        affected = any(
            lineage.get(relative, {}).get("task_id") not in (None, task_id)
            for relative in outputs
        )
        if not affected:
            final_verification[task_id] = "not_affected"
            continue
        try:
            ok, _evidence = _verify_copy(project, grant, task, run_id, 1, verifiers[task_id], trusted_fixture=trusted_fixture)
        except (AutorunError, grants.GrantError):
            ok = False
        final_verification[task_id] = "rechecked_ok" if ok else "rechecked_failed"
        if not ok:
            regressed.append(task_id)
            final_states[task_id] = "regressed"
            # S05：最终验收失败要更新任务状态，但不能抹掉“曾经通过”的历史。
            existing = ledger["tasks"].get(task_id)
            record = dict(existing) if isinstance(existing, dict) else {}
            record["previously_succeeded"] = True
            record["state"] = "regressed"
            record["final_verification"] = "rechecked_failed"
            record["attempts"] = list(record.get("attempts", []))
            ledger["tasks"][task_id] = record
            states[task_id] = "regressed"
    # 依赖已回归任务的下游任务同样不可直接交付（保留其执行成功历史）。
    if regressed:
        blocked = set(regressed)
        changed = True
        while changed:
            changed = False
            for task in tasks:
                task_id = task["task_id"]
                if task_id in blocked:
                    continue
                if any(dep in blocked for dep in task["depends_on"]):
                    blocked.add(task_id)
                    changed = True
        for task_id in sorted(blocked - set(regressed)):
            final_states[task_id] = "regressed_dependent"
            existing = ledger["tasks"].get(task_id)
            if isinstance(existing, dict):
                existing["blocked_by_regression"] = True
                ledger["tasks"][task_id] = existing
    if regressed:
        uxtext.record_interruption(
            project,
            category="system_error",
            action="批次总体检查",
            reason="总体检查发现先前验收的产物被后续修改破坏：" + ",".join(regressed),
            auto_path="保留现场与账本；人工核对后重开受影响任务，不要直接交付。",
        )
        say(uxtext.render_pause(
            "批次总体检查发现回归：" + "、".join(regressed) + "。",
            "相关任务此前验收通过，但最终产物不再满足其验收器；本批不可直接交付。",
            "是否人工核对后重开受影响任务。",
        ))
    return _finish(project, ledger, states, tasks, run_id, final_verification=final_verification, regressed=regressed, final_states=final_states)


LENDREG_SCENARIO["verifiers"] = LENDREG_VERIFIERS


__all__ = [
    "AUTORUN_SCHEMA_VERSION",
    "LENDREG_SCENARIO",
    "MAX_REPAIR_ROUNDS",
    "AutorunError",
    "cancel_run",
    "existing_run_grant_id",
    "rollback_task_files",
    "run_batch",
    "run_binding_status",
    "validate_scenario",
]
