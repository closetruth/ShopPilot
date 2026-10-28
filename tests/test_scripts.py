"""脚本冒烟测试。

这些脚本不进主流程，但跑一下就崩的代码不该存在。之前删除
`rebuild_from_store` 的返回字段后，`scripts/reindex.py` 立刻 KeyError，
而 81 个测试全绿——因为没人测脚本。
"""

import importlib
import subprocess
import sys

import pytest


SCRIPTS = [
    "scripts.seed",
    "scripts.import_faq",
    "scripts.reindex",
    "scripts.migrate_sqlite_to_mysql",
]


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_imports_cleanly(name):
    """导入即执行的定义错误（拼错的常量、缺 import）会在这里暴露。"""
    module = importlib.import_module(name)
    assert hasattr(module, "main") or hasattr(module, "seed") or hasattr(module, "migrate")


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_compiles(name):
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", name.replace(".", "/") + ".py"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_reindex_reads_fields_returned_by_rebuild(monkeypatch):
    """重建脚本读取的字段必须与 rebuild_from_store 的返回键一致。

    这是上一轮改动引入的真实回归：脚本还在读 products / work_orders，
    而函数已经不再返回它们。
    """
    from app import vector_store
    from scripts import reindex

    fake_result = {"faq": 0, "total": 0, "written": 0}
    monkeypatch.setattr(
        vector_store, "rebuild_from_store", lambda drop=False: fake_result
    )
    monkeypatch.setattr(reindex.store, "init_db", lambda: None)

    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))

    reindex.main(drop=True)

    assert printed
    assert any("0 条" in line for line in printed)


def test_import_faq_builds_documents():
    from scripts.import_faq import build_documents

    docs = build_documents()
    assert docs
    assert all(d["content"] for d in docs)
