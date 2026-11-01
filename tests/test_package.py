import hashlib
import shutil
from zipfile import ZipFile

import pytest

from scripts import build_package


@pytest.fixture
def package_root(monkeypatch, tmp_path):
    source = build_package.ROOT
    for relative in build_package.FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    monkeypatch.setattr(build_package, "ROOT", tmp_path)
    return tmp_path


def test_release_is_complete_reproducible_and_excludes_local_state(package_root):
    (package_root / ".env").write_text("secret", encoding="utf-8")
    output = build_package.build()
    content = output.read_bytes()
    assert output.name == "astrbot_plugin_nikke-v1.1.0.zip"
    assert len(content) < 16 * 1024 * 1024
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {
            "astrbot_plugin_nikke/" + name for name in build_package.FILES
        }
        assert archive.read("astrbot_plugin_nikke/docs/images/qq-preview.jpg")
        assert archive.read("astrbot_plugin_nikke/LICENSE")
    assert output.with_suffix(".zip.sha256").read_text(encoding="utf-8").startswith(
        hashlib.sha256(content).hexdigest()
    )
    assert build_package.build().read_bytes() == content


def test_package_rejects_unlisted_runtime_module(package_root):
    (package_root / "nikke/new_module.py").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="Unlisted runtime modules"):
        build_package.build()
