# -*- coding: utf-8 -*-
"""W2(2026-09-30 集成批次 01):新建父目录的事务身份与受控恢复。

背景:Linus/POSIX 上一个空目录的 ``st_nlink`` 恒为 2(自身 + 父目录条目),
子目录再各加 1;而"硬链接(nlink>1)即身份不安全"这条规则只对普通文件成立。
原实现把两者混用,导致从**缺少父目录**开始的新手流程在 Linux 上被整体挡住
(``created parent identity is unavailable``)。

本文件在本机(Windows)上用可控方式复现 POSIX 的目录链接计数语义,验证:
- 目录身份与文件身份分开后,新父目录可以正常创建、写入、回滚;
- 符号链接/重解析点的父目录仍然被拒(不删除任何链接防护);
- 回滚只移除"本事务创建且身份未变的空目录",用户后改目录不被覆盖。
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opencoding import transactions
from opencoding.transactions import apply_changes, preview_changes, rollback_changes


class _StatWithNlink:
    """同一份 stat 结果、只改 st_nlink 的透明代理。

    必须保留平台附加属性(Windows 的 st_file_attributes / st_reparse_tag)与
    取下标/迭代行为,否则会破坏链接/重解析点检测,反而让测试失去意义。
    """

    __slots__ = ("_info", "_nlink")

    def __init__(self, info, nlink):
        object.__setattr__(self, "_info", info)
        object.__setattr__(self, "_nlink", nlink)

    def __getattr__(self, name):
        if name == "st_nlink":
            return self._nlink
        return getattr(self._info, name)

    def __getitem__(self, index):
        value = self._info[index]
        return self._nlink if index == 3 else value

    def __len__(self):
        return len(self._info)

    def __iter__(self):
        for index, value in enumerate(self._info):
            yield self._nlink if index == 3 else value


def _with_nlink(info, nlink):
    return _StatWithNlink(info, nlink)


def _posix_like_stat(original_stat):
    """把目录的 st_nlink 改成 POSIX 语义(空目录=2,子目录+1),其他不变。"""

    def patched(path, **kwargs):
        info = original_stat(path, **kwargs)
        try:
            is_dir = stat.S_ISDIR(int(getattr(info, "st_mode", 0)))
        except (TypeError, ValueError):
            is_dir = False
        if not is_dir or getattr(info, "st_nlink", 1) > 1:
            return info
        link_count = 2
        try:
            for child in os.scandir(str(path)):
                if child.is_dir(follow_symlinks=False):
                    link_count += 1
        except OSError:
            link_count = 2
        return _with_nlink(info, link_count)

    return patched


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class ParentIdentityTests(_Base):
    def test_identity_rules_split_for_file_and_dir(self):
        real_dir = self.root / "real"
        real_dir.mkdir()
        self.assertIsNotNone(transactions._dir_identity(real_dir),
                             "目录身份不应受链接计数影响")
        target = self.root / "file.txt"
        target.write_text("x", encoding="utf-8")
        self.assertIsNotNone(transactions._identity(target))
        with mock.patch("os.stat", _posix_like_stat(os.stat)):
            self.assertIsNotNone(transactions._dir_identity(real_dir),
                                 "POSIX 目录链接计数下仍必须得到目录身份")
            self.assertIsNone(transactions._identity(real_dir),
                              "文件身份规则仍应拒绝目录(保持原语义不变)")

    def test_new_parent_created_under_posix_link_counts(self):
        """POSIX 目录链接计数下,从缺父目录开始的新建必须成功。"""
        with mock.patch("os.stat", _posix_like_stat(os.stat)):
            plan = preview_changes(self.root, {"app/ui/main.py": "print('hi')\n"})
            result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        self.assertEqual(result["status"], "applied", result.get("failure"))
        created = self.root / "app" / "ui" / "main.py"
        self.assertTrue(created.is_file(), "新父目录链必须真实创建")
        self.assertEqual(created.read_text(encoding="utf-8"), "print('hi')\n")

    def test_symlinked_parent_still_refused(self):
        """新父目录放行不等于放行链接:符号链接父目录仍然拒绝。"""
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir(exist_ok=True)
        try:
            link = self.root / "app"
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前环境无法创建符号链接")
        try:
            with mock.patch("os.stat", _posix_like_stat(os.stat)):
                # 链接父目录在预览阶段就被拒绝(失败关闭),根本不会进入写入
                with self.assertRaises(ValueError) as ctx:
                    preview_changes(self.root, {"app/main.py": "x"})
            self.assertIn("link or reparse point", str(ctx.exception),
                          "链接父目录必须拒绝,不得放行")
        finally:
            if link.is_symlink():
                link.unlink()

    def test_rollback_removes_only_owned_empty_parents(self):
        """失败回滚只移除本事务创建且身份未变的空目录。"""
        keep = self.root / "app"
        keep.mkdir()
        (keep / "keep.txt").write_text("user file", encoding="utf-8")
        with mock.patch("os.stat", _posix_like_stat(os.stat)):
            plan = preview_changes(self.root, {"app/deep/new.txt": "x"})
            # 先让写入前的预像漂移,触发失败路径
            (keep / "deep").mkdir()
            (keep / "deep" / "new.txt").write_text("changed by user", encoding="utf-8")
            result = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
        self.assertEqual(result["status"], "blocked")
        self.assertTrue((keep / "keep.txt").is_file(), "用户原有文件不得被移除")
        self.assertTrue((keep / "deep").is_dir(), "用户后改的目录不得被回滚删除")
        self.assertEqual((keep / "deep" / "new.txt").read_text(encoding="utf-8"),
                         "changed by user", "用户后改内容不得被覆盖")

    def test_rollback_after_real_commit_restores_preimage(self):
        with mock.patch("os.stat", _posix_like_stat(os.stat)):
            plan = preview_changes(self.root, {"app/ui/main.py": "v1\n"})
            first = apply_changes(self.root, plan, approved_digest=plan["plan_digest"])
            self.assertEqual(first["status"], "applied")
            rolled = rollback_changes(self.root, first["transaction_id"])
        self.assertEqual(rolled["status"], "rolled_back", rolled.get("failure"))
        self.assertFalse((self.root / "app" / "ui" / "main.py").exists(),
                         "回滚必须移除新建文件")
        # 本事务创建且身份未变的空目录被移除;项目根保留
        self.assertTrue(self.root.is_dir())


if __name__ == "__main__":
    unittest.main()
