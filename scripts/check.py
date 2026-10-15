#!/usr/bin/env python3
"""Enforce the mechanical parts of AGENTS.md. Standard library only; exits 1 on any violation."""
import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
LOCAL_MODULES = {"dayuan", "workbuddy_auth"}
# Each runtime module talks to exactly one host, through exactly one function.
RUNTIME = {"dayuan.py": ("qyapi.weixin.qq.com", "request"), "workbuddy_auth.py": ("www.workbuddy.cn", "http")}
# Modules that would bypass the allowlisted HTTP function or the no-subprocess rule.
FORBIDDEN_IMPORTS = {"http", "socket", "ssl", "subprocess", "multiprocessing", "ftplib", "smtplib", "xmlrpc", "ctypes", "pickle", "asyncio"}
HTTP_CALLS = {"urlopen", "build_opener", "OpenerDirector"}
READ_OPERATIONS = ["search_groups", "read_chat", "search_messages"]
TEST_COUNT = re.compile(r"\d+\s*(?:项|个)(?:离线)?(?:测试|用例)")

problems = []


def report(path, line, message):
    problems.append("%s:%s: %s" % (path.relative_to(ROOT), line, message))


def python_files():
    return sorted(p for p in ROOT.rglob("*.py") if not any(part.startswith(".") or part in ("build", "dist", "__pycache__") for part in p.relative_to(ROOT).parts))


def enclosing_functions(tree):
    owner = {}
    # ast.walk is breadth-first: outer functions are visited first, so inner ones overwrite and win.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is not node:
                    owner[child] = node.name
    return owner


def check_python(path, stdlib):
    source = path.read_text()
    try:
        tree = ast.parse(source, str(path), feature_version=(3, 9))
    except SyntaxError as exc:
        report(path, exc.lineno, "not valid Python 3.9 syntax: %s" % exc.msg)
        return
    runtime = RUNTIME.get(path.name) if path.parent == ROOT else None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            names = []
        for name in names:
            top = name.split(".")[0]
            if stdlib is not None and top not in stdlib and top not in LOCAL_MODULES:
                report(path, node.lineno, "third-party import '%s'; the project is standard library only" % name)
            if runtime and top in FORBIDDEN_IMPORTS:
                report(path, node.lineno, "import '%s' bypasses the single allowlisted HTTP function or spawns processes" % name)
        if isinstance(node, ast.ExceptHandler) and runtime:
            caught = node.type
            if caught is None or (isinstance(caught, ast.Name) and caught.id in ("Exception", "BaseException")):
                report(path, node.lineno, "bare or broad except; catch specific exceptions and map them to an error code")
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if name in ("breakpoint", "set_trace"):
                report(path, node.lineno, "debugger call left in code")
    if runtime:
        host, http_function = runtime
        owner = enclosing_functions(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
                if name in HTTP_CALLS and owner.get(node) != http_function:
                    report(path, node.lineno, "HTTP call outside %s(); route every request through the allowlisted function" % http_function)
        for number, line in enumerate(source.splitlines(), 1):
            for found in re.findall(r"https?://([^/\"'\s]+)", line):
                if found != host:
                    report(path, number, "host '%s' is not allowed in %s (only %s)" % (found, path.name, host))
    for number, line in enumerate(source.splitlines(), 1):
        if re.search(r"#\s*(TODO|FIXME|XXX)\b", line) and "github.com" not in line:
            report(path, number, "TODO without an issue link")


def check_contract():
    sys.path.insert(0, str(ROOT))
    try:
        import dayuan
    except ImportError as exc:
        report(ROOT / "dayuan.py", 1, "cannot import with the standard library alone: %s" % exc)
        return
    operations = dayuan.QUERY_SCHEMA["properties"]["operation"]["enum"]
    if operations != READ_OPERATIONS:
        report(ROOT / "dayuan.py", 1, "query operations changed to %s; only read operations %s are allowed" % (operations, READ_OPERATIONS))
    if not re.fullmatch(r"\d+\.\d+\.\d+", getattr(dayuan, "__version__", "")):
        report(ROOT / "dayuan.py", 1, "__version__ must be defined once as X.Y.Z")
    pyproject = (ROOT / "pyproject.toml").read_text()
    if re.search(r"(?m)^version\s*=\s*\"", pyproject) or 'version = {attr = "dayuan.__version__"}' not in pyproject:
        report(ROOT / "pyproject.toml", 1, "version must be dynamic from dayuan.__version__, not a second literal")
    for path in [ROOT / "dayuan.py", ROOT / "workbuddy_auth.py"]:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"dayuan-lite/\d|\"version\":\s*\"\d", line):
                report(path, number, "hard-coded project version; use dayuan.__version__")


def check_docs():
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if TEST_COUNT.search(line):
                report(path, number, "exact test count in docs goes stale on every change; describe coverage instead")


def main():
    # sys.stdlib_module_names exists from Python 3.10; CI runs this check on a newer interpreter.
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is None:
        print("check: Python < 3.10, skipping the standard-library import check", file=sys.stderr)
    for path in python_files():
        check_python(path, stdlib)
    check_contract()
    check_docs()
    for line in problems:
        print(line)
    if problems:
        print("\n%d problem(s). See AGENTS.md." % len(problems))
        sys.exit(1)
    print("check: ok")


if __name__ == "__main__":
    main()
