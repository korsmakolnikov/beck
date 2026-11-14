"""A greenfield project's layout: beck's Python templates, mix new for Elixir; never over a file."""

import shutil
import subprocess

import pytest

from beck.scaffold import ScaffoldError, scaffold
from beck.toolchain import load


def test_a_python_project_is_laid_with_its_toolchain_wired(tmp_path):
    laid = scaffold(tmp_path, "python", "shop-cart")
    assert "src/shop_cart/__init__.py" in laid and "Makefile" in laid and ".github/workflows/ci.yml" in laid
    assert 'name = "shop-cart"' in (tmp_path / "pyproject.toml").read_text()
    assert "pytest-cov" in (tmp_path / "pyproject.toml").read_text()
    toolchain = load(tmp_path)
    assert toolchain.language == "python" and toolchain.coverage.format == "coverage.py"


def test_the_scaffold_never_overwrites(tmp_path):
    (tmp_path / "README.md").write_text("mine\n")
    with pytest.raises(ScaffoldError, match="overwrite README.md"):
        scaffold(tmp_path, "python", "x")
    assert (tmp_path / "README.md").read_text() == "mine\n"
    with pytest.raises(ScaffoldError, match="not 'rust'"):
        scaffold(tmp_path, "rust", "x")


@pytest.mark.skipif(shutil.which("mix") is None, reason="Elixir is not installed")
def test_an_elixir_project_is_laid_by_mix_new(tmp_path):
    (tmp_path / "notes.md").write_text("the shopping list\n")
    laid = scaffold(tmp_path, "elixir", "shop-cart")
    assert "mix.exs" in laid and "lib/shop_cart.ex" in laid
    assert "app: :shop_cart" in (tmp_path / "mix.exs").read_text()
    assert subprocess.run(["mix", "compile"], cwd=tmp_path, capture_output=True).returncode == 0
