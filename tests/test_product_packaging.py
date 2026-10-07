"""Offline distribution checks for the bounded OpenCoding package."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import tomllib
import unicodedata
import unittest
import urllib.parse
import zipfile

from opencoding.intake import QUESTION_DEFINITIONS
from opencoding.safety import safe_target


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_FILES = tuple(sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "opencoding").glob("*.py")))
FIXTURE_FILES = ("pyproject.toml", "MANIFEST.in", "docs/product/OFFLINE_INSTALLATION.md", *PACKAGE_FILES)
DIST_INFO_FILES = {"METADATA", "RECORD", "WHEEL", "entry_points.txt", "top_level.txt"}
SDIST_EGG_INFO_FILES = {"PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt", "top_level.txt"}
PRIVATE_FILENAMES = ("AUTHORS_PRIVATE.txt", "AUTHORS", "COPYING", "LICENSE", "LICENSE.txt", "NOTICE", "NOTICE.txt")
SENTINEL_DIRECTORIES = ("apps", "artifacts", "docs", "scripts", "services", "tests", ".governance")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _retained_workspace(label: str) -> Path:
    workspace = Path(tempfile.mkdtemp(prefix=f"opencoding-packaging-{label}-"))
    (workspace / "logs").mkdir()
    print(f"retained packaging workspace: {workspace}", file=sys.stderr)
    return workspace


def _environment(workspace: Path) -> dict[str, str]:
    temporary = workspace / "temp"
    cargo_target = workspace / "cargo-target"
    temporary.mkdir(exist_ok=True)
    cargo_target.mkdir(exist_ok=True)
    return dict(
        os.environ,
        CARGO_NET_OFFLINE="true",
        CARGO_TARGET_DIR=str(cargo_target),
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_NO_CACHE_DIR="1",
        PIP_NO_INDEX="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
        PYTHONNOUSERSITE="1",
        LOCALAPPDATA=str(workspace / "user-config"),
        TEMP=str(temporary),
        TMP=str(temporary),
    )


def _run(
    command: list[str],
    *,
    cwd: Path,
    workspace: Path,
    label: str,
    text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=_environment(workspace),
        input=text,
        text=True,
        capture_output=True,
        check=False,
        creationflags=CREATE_NO_WINDOW,
        timeout=120,
    )
    log = workspace / "logs" / label
    log.with_suffix(".command.json").write_text(json.dumps(command, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    log.with_suffix(".stdout.log").write_text(result.stdout, encoding="utf-8")
    log.with_suffix(".stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        raise AssertionError(
            "command failed:\n"
            + " ".join(command)
            + "\nstdout:\n"
            + result.stdout
            + "\nstderr:\n"
            + result.stderr
        )
    return result


def _copy_fixture(source_root: Path, destination_root: Path) -> None:
    """Copy only declared regular inputs after validating the whole copy plan."""

    sources = []
    for relative in FIXTURE_FILES:
        source = safe_target(source_root, relative, allow_missing=False)
        destination = safe_target(destination_root, relative, allow_missing=True)
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"fixture output already exists: {relative}")
        sources.append((source, destination))
    for source, _ in sources:
        source.read_bytes()
    for source, destination in sources:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())


def _inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _portable_key(parts: tuple[str, ...]) -> str:
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in parts)


def _archive_relative(name: str, prefix: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name or ":" in name:
        raise AssertionError(f"unsafe archive member: {name!r}")
    raw_parts = name.split("/")
    if name.startswith("/") or any(part in ("", ".", "..") for part in raw_parts):
        raise AssertionError(f"unsafe archive member: {name}")
    candidate = PurePosixPath(name)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AssertionError(f"unsafe archive member: {name}")
    try:
        relative = candidate.relative_to(prefix)
    except ValueError as error:
        raise AssertionError(f"archive member outside root: {name}") from error
    if not relative.parts:
        raise AssertionError(f"archive root cannot be extracted as a file: {name}")
    return relative


def _validate_sdist_members(archive_path: Path, destination: Path) -> tuple[Path, list[tuple[tarfile.TarInfo, Path]]]:
    """Return an extraction plan only after validating all names, kinds, and targets."""

    prefix = archive_path.name.removesuffix(".tar.gz")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
    roots = [member for member in members if member.name == prefix]
    if len(roots) != 1 or not roots[0].isdir():
        raise AssertionError("sdist must contain exactly one regular root directory")
    try:
        output_root = safe_target(destination, prefix + "/.fixture-parent", allow_missing=True).parent
    except ValueError as error:
        raise AssertionError("sdist output root is unsafe") from error
    if output_root.exists() or output_root.is_symlink():
        raise AssertionError("sdist output root already exists")

    plan: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
    kinds: dict[str, str] = {}
    spellings: dict[str, tuple[str, ...]] = {}
    for member in members:
        if member.name == prefix:
            continue
        if not (member.isdir() or member.isfile()) or member.issym() or member.islnk():
            raise AssertionError(f"sdist contains unsupported member: {member.name}")
        relative = _archive_relative(member.name, prefix)
        # Implicit parents need the same alias checks as explicit tar members.
        for depth in range(1, len(relative.parts) + 1):
            parts = relative.parts[:depth]
            parent_key = _portable_key(parts)
            if spellings.setdefault(parent_key, parts) != parts:
                raise AssertionError(f"sdist contains a parent alias: {member.name}")
        key = _portable_key(relative.parts)
        if key in kinds:
            raise AssertionError(f"sdist contains an alias or duplicate member: {member.name}")
        kinds[key] = "directory" if member.isdir() else "file"
        plan.append((member, relative))

    for key, kind in kinds.items():
        parts = tuple(key.split("/"))
        for index in range(1, len(parts)):
            if kinds.get("/".join(parts[:index])) == "file":
                raise AssertionError("sdist contains a file/directory collision")
        if kind == "file" and any(other.startswith(key + "/") for other in kinds):
            raise AssertionError("sdist contains a file/directory collision")

    validated: list[tuple[tarfile.TarInfo, Path]] = []
    for member, relative in plan:
        output_relative = "/".join((prefix, *relative.parts))
        try:
            output = safe_target(destination, output_relative, allow_missing=True)
            if len(relative.parts) > 1:
                safe_target(destination, "/".join((prefix, *relative.parts[:-1], ".fixture-parent")), allow_missing=True)
        except ValueError as error:
            raise AssertionError(f"unsafe sdist output: {relative.as_posix()}") from error
        if output.exists() or output.is_symlink():
            raise AssertionError(f"sdist output already exists: {relative.as_posix()}")
        validated.append((member, output))
    return output_root, validated


def _extract_sdist(archive_path: Path, destination: Path) -> Path:
    output_root, plan = _validate_sdist_members(archive_path, destination)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member, target in sorted(plan, key=lambda item: (len(item[1].parts), item[1].as_posix())):
            if member.isdir():
                target.mkdir(parents=True, exist_ok=False)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise AssertionError(f"sdist member is unreadable: {member.name}")
            target.write_bytes(source.read())
    return output_root


def _write_fixture_archive(archive_path: Path, entries: list[tuple[str, str, bytes | str]]) -> None:
    prefix = archive_path.name.removesuffix(".tar.gz")
    with tarfile.open(archive_path, "w:gz") as archive:
        root = tarfile.TarInfo(prefix)
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, kind, value in entries:
            member = tarfile.TarInfo(prefix + "/" + name)
            if kind == "directory":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            elif kind == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = str(value)
                archive.addfile(member)
            else:
                content = bytes(value)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))


class ProductPackagingTests(unittest.TestCase):
    def _check_installed_workbench(self, python: Path, outside: Path) -> None:
        """Exercise the installed module over loopback without user AI settings."""
        projects = self.workspace / "workbench-projects"
        for restarting in (False, True):
            process = subprocess.Popen(
                [str(python), "-I", "-B", "-u", "-X", "utf8", "-m",
                 "opencoding.workbench", "--workspace", str(projects),
                 "--port", "0", "--no-browser"],
                cwd=outside,
                env=_environment(self.workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                creationflags=CREATE_NO_WINDOW,
            )
            lines = queue.Queue()
            reader = threading.Thread(
                target=lambda: lines.put(process.stdout.readline()), daemon=True,
            )
            reader.start()
            connection = None
            try:
                try:
                    startup = lines.get(timeout=15)
                except queue.Empty:
                    self.fail("installed workbench did not announce its loopback address")
                match = re.search(r"http://127\.0\.0\.1:\d+/\?t=[A-Za-z0-9_-]+", startup)
                # Never include the startup token in assertion output or retained logs.
                self.assertIsNotNone(match, "installed workbench failed to start")
                url = urllib.parse.urlsplit(match.group(0))
                connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)

                def request(method, path, *, cookie=None, body=None):
                    headers = {"Content-Type": "application/json"}
                    if cookie:
                        headers["Cookie"] = cookie
                    connection.request(method, path, body=body, headers=headers)
                    response = connection.getresponse()
                    payload = response.read()
                    return response, payload

                response, _ = request("GET", "/api/bootstrap")
                self.assertEqual(response.status, 401)
                before = _inventory(projects)
                response, page = request("GET", url.path + "?" + url.query)
                self.assertEqual(response.status, 200)
                self.assertIn(b"OpenCoding", page)
                cookie_header = response.getheader("Set-Cookie") or ""
                self.assertTrue("HttpOnly" in cookie_header and "SameSite=Strict" in cookie_header)
                cookie = cookie_header.split(";", 1)[0]
                response, payload = request("GET", "/api/bootstrap", cookie=cookie)
                self.assertEqual(response.status, 200)
                self.assertEqual(_inventory(projects), before)
                names = [item["name"] for item in json.loads(payload)["projects"]]
                self.assertEqual(names, ["安装验证"] if restarting else [])
                if not restarting:
                    response, payload = request(
                        "POST", "/api/project", cookie=cookie,
                        body=json.dumps({"name": "安装验证"}).encode("utf-8"),
                    )
                    self.assertEqual(response.status, 200)
                    self.assertTrue(json.loads(payload)["created"])
                    self.assertTrue((projects / "安装验证/.opencoding/project.json").is_file())
            finally:
                if connection is not None:
                    connection.close()
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                reader.join(timeout=5)
                process.stdout.close()
        self.assertFalse((self.workspace / "user-config/OpenCoding/ai_provider.json").exists())

    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace = _retained_workspace("source")
        cls.scratch = cls.workspace / "scratch"
        cls.scratch.mkdir()
        _copy_fixture(ROOT, cls.scratch)
        for directory in SENTINEL_DIRECTORIES:
            sentinel = cls.scratch / directory
            sentinel.mkdir(parents=True, exist_ok=True)
            (sentinel / "private-sentinel.txt").write_text("must-not-package", encoding="utf-8")
        for filename in PRIVATE_FILENAMES:
            (cls.scratch / filename).write_text("must-not-package", encoding="utf-8")
        (cls.scratch / "README.md").write_text("must-not-package", encoding="utf-8")
        cls.dist = cls.workspace / "dist"
        cls.dist.mkdir()
        build = (
            "import pathlib,sys,setuptools.build_meta as backend;"
            "sdist_out=str(pathlib.Path(sys.argv[1]).resolve());"
            "wheel_out=str(pathlib.Path(sys.argv[2]).resolve());"
            "print(backend.build_sdist(sdist_out));"
            "print(backend.build_wheel(wheel_out))"
        )
        _run(
            [sys.executable, "-I", "-B", "-X", "utf8", "-c", build, str(cls.dist), str(cls.dist)],
            cwd=cls.scratch,
            workspace=cls.workspace,
            label="source-build",
        )
        cls.wheel = next(cls.dist.glob("*.whl"))
        cls.sdist = next(cls.dist.glob("*.tar.gz"))

    def test_build_contract_and_archives_have_exact_allowlists(self):
        configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(configuration["build-system"], {"requires": ["setuptools>=83"], "build-backend": "setuptools.build_meta"})
        self.assertEqual(configuration["project"]["scripts"], {"opencoding": "opencoding.cli:main"})
        self.assertEqual(
            configuration["project"]["readme"],
            {"file": "docs/product/OFFLINE_INSTALLATION.md", "content-type": "text/markdown"},
        )
        self.assertEqual(configuration["project"]["license-files"], [])
        self.assertEqual(configuration["tool"]["setuptools"], {"packages": ["opencoding"], "include-package-data": False})

        with zipfile.ZipFile(self.wheel) as archive:
            members = set(archive.namelist())
            dist_info = next(member.rsplit("/", 1)[0] for member in members if member.endswith(".dist-info/METADATA"))
            expected = set(PACKAGE_FILES) | {f"{dist_info}/{name}" for name in DIST_INFO_FILES}
            self.assertEqual(members, expected)
            self.assertEqual(
                archive.read(f"{dist_info}/entry_points.txt").decode("utf-8"),
                "[console_scripts]\nopencoding = opencoding.cli:main\n",
            )

        with tarfile.open(self.sdist, "r:gz") as archive:
            files = {member.name for member in archive.getmembers() if member.isfile()}
        prefix = self.sdist.name.removesuffix(".tar.gz")
        expected = {
            f"{prefix}/MANIFEST.in",
            f"{prefix}/PKG-INFO",
            f"{prefix}/pyproject.toml",
            f"{prefix}/setup.cfg",
            f"{prefix}/docs/product/OFFLINE_INSTALLATION.md",
        }
        expected.update(f"{prefix}/{name}" for name in PACKAGE_FILES)
        expected.update(f"{prefix}/opencoding_local_entry.egg-info/{name}" for name in SDIST_EGG_INFO_FILES)
        self.assertEqual(files, expected)
        prohibited = (*PRIVATE_FILENAMES, "private-sentinel", "tests/", "apps/", "artifacts/", "scripts/", "services/", ".governance/", "README")
        self.assertFalse(any(fragment in member for member in files | members for fragment in prohibited))

    def test_fixture_rejects_leaf_hardlink_before_any_copy(self):
        workspace = _retained_workspace("hardlink")
        source = workspace / "source"
        destination = workspace / "destination"
        source.mkdir()
        destination.mkdir()
        _copy_fixture(ROOT, source)
        os.link(source / "opencoding" / "cli.py", source / "opencoding" / "cli-alias.py")
        with self.assertRaisesRegex(ValueError, "hardlink"):
            _copy_fixture(source, destination)
        self.assertEqual(_inventory(destination), {})

    def test_fixture_rejects_root_parent_and_destination_links_before_any_copy(self):
        workspace = _retained_workspace("links")
        real_source = workspace / "real-source"
        real_source.mkdir()
        _copy_fixture(ROOT, real_source)

        linked_root = workspace / "linked-root"
        os.symlink(real_source, linked_root, target_is_directory=True)
        root_destination = workspace / "root-destination"
        root_destination.mkdir()
        with self.assertRaisesRegex(ValueError, "link or reparse point"):
            _copy_fixture(linked_root, root_destination)
        self.assertEqual(_inventory(root_destination), {})

        parent_source = workspace / "parent-source"
        parent_source.mkdir()
        _copy_fixture(ROOT, parent_source)
        package_target = workspace / "package-target"
        (parent_source / "opencoding").replace(package_target)
        os.symlink(package_target, parent_source / "opencoding", target_is_directory=True)
        parent_destination = workspace / "parent-destination"
        parent_destination.mkdir()
        with self.assertRaisesRegex(ValueError, "link or reparse point"):
            _copy_fixture(parent_source, parent_destination)
        self.assertEqual(_inventory(parent_destination), {})

        destination = workspace / "destination-link"
        destination.mkdir()
        outside = workspace / "outside"
        outside.mkdir()
        os.symlink(outside, destination / "opencoding", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "link or reparse point"):
            _copy_fixture(real_source, destination)
        self.assertEqual(_inventory(destination), {})

    def test_sdist_extraction_rejects_aliases_links_and_collisions_before_writes(self):
        workspace = _retained_workspace("archive-boundaries")
        invalid_cases = {
            "backslash": [("valid.txt", "file", b"valid"), (r"..\escaped.txt", "file", b"escape")],
            "drive": [("valid.txt", "file", b"valid"), ("C:/escaped.txt", "file", b"escape")],
            "ads": [("valid.txt", "file", b"valid"), ("note:stream", "file", b"escape")],
            "trailing": [("valid.txt", "file", b"valid"), ("name. ", "file", b"escape")],
            "device": [("valid.txt", "file", b"valid"), ("CON.txt", "file", b"escape")],
            "case": [("valid.txt", "file", b"valid"), ("dup.txt", "file", b"one"), ("DUP.txt", "file", b"two")],
            "unicode": [("valid.txt", "file", b"valid"), ("e\u0301.txt", "file", b"one"), ("\u00e9.txt", "file", b"two")],
            "implicit-case": [("valid.txt", "file", b"valid"), ("Folder/one.txt", "file", b"one"), ("folder/two.txt", "file", b"two")],
            "implicit-unicode": [("valid.txt", "file", b"valid"), ("e\u0301/one.txt", "file", b"one"), ("\u00e9/two.txt", "file", b"two")],
            "duplicate": [("valid.txt", "file", b"valid"), ("same.txt", "file", b"one"), ("same.txt", "file", b"two")],
            "collision": [("valid.txt", "file", b"valid"), ("node", "file", b"one"), ("node/child.txt", "file", b"two")],
            "link": [("valid.txt", "file", b"valid"), ("linked", "link", "outside")],
        }
        for label, entries in invalid_cases.items():
            with self.subTest(label=label):
                archive = workspace / f"{label}.tar.gz"
                destination = workspace / f"{label}-destination"
                destination.mkdir()
                _write_fixture_archive(archive, entries)
                before = _inventory(destination)
                with self.assertRaises(AssertionError):
                    _extract_sdist(archive, destination)
                self.assertEqual(_inventory(destination), before)
                self.assertFalse((workspace / "escaped.txt").exists())

        linked_archive = workspace / "linked-output.tar.gz"
        linked_destination = workspace / "linked-output-destination"
        linked_destination.mkdir()
        outside = workspace / "linked-output-outside"
        outside.mkdir()
        os.symlink(outside, linked_destination / "linked-output", target_is_directory=True)
        _write_fixture_archive(linked_archive, [("valid.txt", "file", b"valid")])
        with self.assertRaisesRegex(AssertionError, "output root is unsafe"):
            _extract_sdist(linked_archive, linked_destination)
        self.assertEqual(_inventory(outside), {})

    def test_sdist_extraction_uses_validated_canonical_targets(self):
        workspace = _retained_workspace("canonical-target")
        archive = workspace / "canonical.tar.gz"
        destination = workspace / "destination"
        destination.mkdir()
        _write_fixture_archive(archive, [("e\u0301/one.txt", "file", b"canonical")])
        extracted = _extract_sdist(archive, destination)
        self.assertEqual((extracted / "\u00e9/one.txt").read_bytes(), b"canonical")
        self.assertEqual({path.name for path in extracted.iterdir()}, {"\u00e9"})

    def test_sdist_rebuild_and_clean_install_use_installed_entrypoints(self):
        rebuild_root = self.workspace / "sdist-rebuild"
        rebuild_root.mkdir()
        extracted = _extract_sdist(self.sdist, rebuild_root)
        rebuilt_dist = self.workspace / "rebuilt-dist"
        rebuilt_dist.mkdir()
        wheel_output = str(rebuilt_dist.resolve())
        rebuild = (
            "import pathlib,sys,setuptools.build_meta as backend;"
            "wheel_out=str(pathlib.Path(sys.argv[1]).resolve());"
            "print(backend.build_wheel(wheel_out))"
        )
        _run(
            [sys.executable, "-I", "-B", "-X", "utf8", "-c", rebuild, wheel_output],
            cwd=extracted,
            workspace=self.workspace,
            label="sdist-rebuild",
        )
        rebuilt_wheel = next(rebuilt_dist.glob("*.whl"))
        venv = self.workspace / "venv"
        outside = self.workspace / "outside-repository"
        project = self.workspace / "synthetic-project"
        outside.mkdir()
        project.mkdir()
        _run(
            [sys.executable, "-I", "-B", "-X", "utf8", "-m", "venv", str(venv)],
            cwd=outside,
            workspace=self.workspace,
            label="create-venv",
        )
        python = venv / "Scripts" / "python.exe"
        console = venv / "Scripts" / "opencoding.exe"
        if not python.exists():
            python = venv / "bin" / "python"
            console = venv / "bin" / "opencoding"
        _run(
            [str(python), "-I", "-B", "-X", "utf8", "-m", "pip", "install", "--no-index", "--no-deps", "--no-cache-dir", "--no-compile", str(rebuilt_wheel)],
            cwd=outside,
            workspace=self.workspace,
            label="install-rebuilt-wheel",
        )
        origin = _run(
            [str(python), "-I", "-B", "-X", "utf8", "-c", "import opencoding,pathlib,sys; p=pathlib.Path(opencoding.__file__).resolve(); assert p.is_relative_to(pathlib.Path(sys.prefix).resolve()); print(p)"],
            cwd=outside,
            workspace=self.workspace,
            label="installed-origin",
        )
        self.assertNotIn(str((ROOT / "opencoding").resolve()).lower(), origin.stdout.lower())

        before = _inventory(project)
        module_status = _run(
            [str(python), "-I", "-B", "-X", "utf8", "-m", "opencoding", "--root", str(project), "--status", "--json"],
            cwd=outside,
            workspace=self.workspace,
            label="module-status",
        )
        console_status = _run(
            [str(console), "--root", str(project), "--status", "--json"],
            cwd=outside,
            workspace=self.workspace,
            label="console-status",
        )
        self.assertEqual(json.loads(module_status.stdout)["status"], "not_initialized")
        self.assertEqual(json.loads(console_status.stdout)["status"], "not_initialized")
        self.assertEqual(_inventory(project), before)

        answers = ["offline packaging test", "users", "web", "local documents"]
        answers.extend(["\u4e0d\u9700\u8981"] * (len(QUESTION_DEFINITIONS) - 3))
        answers.append("yes")
        applied = _run(
            [str(console), "--root", str(project)],
            cwd=outside,
            workspace=self.workspace,
            label="apply-document-transaction",
            text="\n".join(answers) + "\n",
        )
        transaction = re.search(r"tx-[^\s\uFF1B]+", applied.stdout)
        self.assertIsNotNone(transaction, applied.stdout)
        self.assertTrue((project / "AGENTS.md").exists())
        rolled_back = _run(
            [str(console), "--root", str(project), "--rollback", transaction.group(0)],
            cwd=outside,
            workspace=self.workspace,
            label="rollback-document-transaction",
        )
        self.assertIn("rolled_back", rolled_back.stdout)
        self.assertFalse((project / "AGENTS.md").exists())
        self._check_installed_workbench(python, outside)


if __name__ == "__main__":
    unittest.main()
