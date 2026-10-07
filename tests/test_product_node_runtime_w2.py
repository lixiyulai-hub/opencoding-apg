# -*- coding: utf-8 -*-
"""W2(2026-09-30):第二技术上下文(Node/JavaScript)的执行/验证适配受控验证。

验证的是**适配层本身**:契约接受 node 运行时、冻结检查器是 Node 版本、
main 与 restart 在两个独立 node 进程中执行、内存实现在 restart 必然失败。

边界声明:
- 这里用真实 node 可执行文件跑真实冻结检查器(本机的 Node 已存在);
- 这不是"资格化的未知候选执行"(那需要实际受限执行后端,见 W3/REAL-14);
- 候选源码是合成夹具,用于验证适配,不代表用户产品动态生成已完成。
"""
import json
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from opencoding import generic_run

NODE_MAIN = r'''
// 合成夹具:一个极小的"借还登记"持久化模块(仅 Node 内置模块)。
const fs = require('fs');
const path = require('path');

const DATA_DIR = path.join(__dirname, 'data');
const STORE = path.join(DATA_DIR, 'store.json');

function _ensure() { fs.mkdirSync(DATA_DIR, { recursive: true }); }
function _read() {
  _ensure();
  if (!fs.existsSync(STORE)) { return []; }
  try { return JSON.parse(fs.readFileSync(STORE, 'utf8')); } catch (e) { return []; }
}
function _write(rows) {
  _ensure();
  fs.writeFileSync(STORE, JSON.stringify(rows, null, 1));
}
function add_item(name) {
  const rows = _read();
  const id = 'item-' + String(rows.length + 1).padStart(3, '0');
  rows.push({ id: id, name: name, state: '在库' });
  _write(rows);
  return id;
}
function borrow(id, who) {
  const rows = _read();
  const hit = rows.find(function (r) { return r.id === id; });
  if (!hit) { throw new Error('未找到物品:' + id); }
  hit.state = '已借出';
  hit.borrower = who;
  _write(rows);
  return hit;
}
function list_all() { return _read(); }

module.exports = { add_item: add_item, borrow: borrow, list_all: list_all };
'''

NODE_MAIN_MEMORY_ONLY = r'''
// 反向对照:只存在内存里,重启必然丢失。
const rows = [];
function add_item(name) {
  const id = 'item-' + String(rows.length + 1).padStart(3, '0');
  rows.push({ id: id, name: name, state: '在库' });
  return id;
}
function list_all() { return rows; }
module.exports = { add_item: add_item, list_all: list_all };
'''


def _node_contract():
    return {
        "runtime": "node",
        "entry_module": "app.main",
        "data_dir": "app/data",
        "files": [{"path": "app/main.js"}, {"path": "app/selftest.js"}],
        "functions": {
            "add_item": "登记一件物品,返回记录编号",
            "borrow": "借出:把记录标记为已借出并记下借用人",
            "list_all": "列出全部记录",
        },
        "identity_fields": ["id"],
        "steps": [
            {"op": "call", "phase": "main", "function": "add_item",
             "args": ["投影仪"], "save_as": "created_id"},
            {"op": "call", "phase": "main", "function": "borrow",
             "args": ["$created_id", "张三"], "save_as": "borrowed"},
            {"op": "assert", "phase": "main", "saved": "borrowed",
             "contains": [{"state": "已借出", "borrower": "张三"}]},
            {"op": "call", "phase": "restart", "function": "list_all",
             "save_as": "after_restart"},
            {"op": "assert", "phase": "restart", "saved": "after_restart",
             "contains": [{"state": "已借出", "borrower": "张三"}]},
        ],
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "app").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run_checker(self, scratch: Path, phase: str, checker_rel: str):
        node = generic_run._node_executable()
        self.assertIsNotNone(node, "本机需要真实 node 可执行文件")
        return subprocess.run([node, checker_rel, phase], cwd=str(scratch),
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=120)


class NodeRuntimeAdapterTests(_Base):
    def test_contract_accepts_node_runtime(self):
        contract = generic_run._validate_contract(_node_contract())
        self.assertEqual(contract["runtime"], "node")
        self.assertIn("app/main.js", contract["files"])
        self.assertTrue(any("Node.js" in item for item in
                            generic_run._runtime_requirements(contract)))

    def test_unknown_runtime_still_refused(self):
        with self.assertRaises(generic_run.GenericRunError) as ctx:
            generic_run._validate_contract({**_node_contract(), "runtime": "html-string"})
        self.assertEqual(ctx.exception.code, "runtime_unsupported",
                         "仅出现 HTML 字符串不得计为第二技术上下文")

    def test_frozen_checker_is_node_version(self):
        contract = generic_run._validate_contract(_node_contract())
        record = generic_run._write_frozen_checks(self.root, "gen-0000c6d00001",
                                                  "借还登记", contract)
        self.assertEqual(record["runtime"], "node")
        self.assertEqual(record["checker_name"], "app_contract_node.js")
        written = (self.root / ".opencoding" / "frozen_checks" / "gen-0000c6d00001"
                   / "app_contract_node.js")
        self.assertTrue(written.is_file())
        self.assertIn("require('fs')", written.read_text(encoding="utf-8"))

    def test_node_app_passes_main_and_restart(self):
        """真实 node 跑真实冻结检查器:落盘实现两个进程都通过。"""
        contract = generic_run._validate_contract(_node_contract())
        record = generic_run._write_frozen_checks(self.root, "gen-0000c6d00002",
                                                  "借还登记", contract)
        (self.root / "app" / "main.js").write_text(NODE_MAIN, encoding="utf-8")
        frozen = self.root / ".opencoding" / "frozen_checks" / "gen-0000c6d00002"
        scratch = self.root
        (scratch / "frozen_checks").mkdir(exist_ok=True)
        for name in ("spec.json", "app_contract_node.js"):
            (scratch / "frozen_checks" / name).write_bytes(
                (frozen / name).read_bytes())
        self.assertEqual(record["checker_name"], "app_contract_node.js")
        main = self._run_checker(scratch, "main", "frozen_checks/app_contract_node.js")
        self.assertEqual(main.returncode, 0, main.stdout + main.stderr)
        restart = self._run_checker(scratch, "restart", "frozen_checks/app_contract_node.js")
        self.assertEqual(restart.returncode, 0, restart.stdout + restart.stderr)
        self.assertIn("PASS", restart.stdout)
        self.assertTrue((self.root / "app" / "data" / "store.json").is_file(),
                        "数据必须真实落盘")

    def test_memory_only_implementation_fails_restart(self):
        """反向对照:只放内存的实现在 restart 阶段必然失败(独立新进程)。"""
        contract = generic_run._validate_contract(_node_contract())
        generic_run._write_frozen_checks(self.root, "gen-0000c6d00003",
                                         "借还登记", contract)
        (self.root / "app" / "main.js").write_text(NODE_MAIN_MEMORY_ONLY,
                                                   encoding="utf-8")
        frozen = self.root / ".opencoding" / "frozen_checks" / "gen-0000c6d00003"
        (self.root / "frozen_checks").mkdir(exist_ok=True)
        for name in ("spec.json", "app_contract_node.js"):
            (self.root / "frozen_checks" / name).write_bytes(
                (frozen / name).read_bytes())
        restart = self._run_checker(self.root, "restart",
                                    "frozen_checks/app_contract_node.js")
        self.assertNotEqual(restart.returncode, 0,
                            "内存实现不得通过重启持久化断言")
        self.assertIn("FAIL", restart.stdout)

    def test_runtime_command_reports_missing_node(self):
        with unittest.mock.patch.object(generic_run, "_node_executable",
                                        return_value=None):
            with self.assertRaises(generic_run.GenericRunError) as ctx:
                generic_run._runtime_command("node", "frozen_checks/app_contract_node.js",
                                             "main")
        self.assertEqual(ctx.exception.code, "runtime_unavailable",
                         "缺少真实 node 时必须明确拒绝,不伪装可用")


if __name__ == "__main__":
    unittest.main()
