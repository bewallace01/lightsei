"""Phase 5.5: SDK CLI deploy command (zip builder + arg handling)."""
import io
import os
import zipfile
from pathlib import Path

import pytest

from lightsei._cli import _build_zip, deploy


def test_build_zip_includes_python_files(tmp_path: Path):
    (tmp_path / "bot.py").write_text("print('hi')\n")
    (tmp_path / "requirements.txt").write_text("openai\n")
    (tmp_path / "helpers").mkdir()
    (tmp_path / "helpers" / "util.py").write_text("X = 1\n")

    data = _build_zip(tmp_path)

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = sorted(z.namelist())
    assert names == ["bot.py", "helpers/util.py", "requirements.txt"]


def test_build_zip_excludes_dev_junk(tmp_path: Path):
    (tmp_path / "bot.py").write_text("x")
    # Each of these should be skipped.
    for d in ("__pycache__", ".venv", ".git", "node_modules", ".pytest_cache",
              ".lightsei-runtime", "dist", "build", ".mypy_cache"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "trash").write_text("x")
    (tmp_path / "main.pyc").write_text("x")
    (tmp_path / ".DS_Store").write_text("x")

    data = _build_zip(tmp_path)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()

    assert "bot.py" in names
    for excluded in (
        "__pycache__/trash", ".venv/trash", ".git/trash",
        "node_modules/trash", ".pytest_cache/trash",
        ".lightsei-runtime/trash", "dist/trash", "build/trash",
        ".mypy_cache/trash", "main.pyc", ".DS_Store",
    ):
        assert excluded not in names, f"should have skipped {excluded}"


def test_build_zip_is_deterministic(tmp_path: Path):
    """Same input → same bytes (so a re-deploy with no changes hashes
    identically). Walk order matters here."""
    (tmp_path / "a.py").write_text("a")
    (tmp_path / "b.py").write_text("b")
    (tmp_path / "z.py").write_text("z")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("c")

    first = _build_zip(tmp_path)
    second = _build_zip(tmp_path)
    assert first == second


def test_build_zip_rejects_non_directory(tmp_path: Path):
    f = tmp_path / "not-a-dir.txt"
    f.write_text("x")
    with pytest.raises(ValueError):
        _build_zip(f)


def test_deploy_no_directory_returns_2(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("LIGHTSEI_API_KEY", "bk_dummy")
    rc = deploy([str(tmp_path / "does-not-exist")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not a directory" in err


def test_deploy_missing_bot_py_returns_2(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("LIGHTSEI_API_KEY", "bk_dummy")
    rc = deploy([str(tmp_path)])  # tmp_path is a dir but has no bot.py
    assert rc == 2
    err = capsys.readouterr().err
    assert "no bot.py" in err


def test_deploy_missing_api_key_exits(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("LIGHTSEI_API_KEY", raising=False)
    (tmp_path / "bot.py").write_text("x")
    with pytest.raises(SystemExit) as exc:
        deploy([str(tmp_path)])
    assert "LIGHTSEI_API_KEY" in str(exc.value)
