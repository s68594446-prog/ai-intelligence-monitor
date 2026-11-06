import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

BACKEND = Path(__file__).resolve().parents[1]


def pins(filename):
    text = (BACKEND / filename).read_text()
    result = {}
    for block in re.split(r"\n(?=[a-zA-Z])", text.strip()):
        first = block.splitlines()[0].rstrip(" \\")
        requirement = Requirement(first)
        assert re.fullmatch(r"==[0-9][^,]*", str(requirement.specifier))
        assert re.search(r"--hash=sha256:[a-f0-9]{64}", block)
        assert requirement.url is None
        result[canonicalize_name(requirement.name)] = Version(str(requirement.specifier)[2:])
    return result


def test_locks_cover_declared_dependencies_and_share_runtime_versions():
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    runtime, dev, build = (pins(name) for name in ("requirements.lock", "requirements-dev.lock", "requirements-build.lock"))
    for dependencies, locked in (
        (project["project"]["dependencies"], runtime),
        (project["project"]["optional-dependencies"]["dev"], dev),
        (project["build-system"]["requires"], build),
    ):
        for text in dependencies:
            requirement = Requirement(text)
            assert locked[canonicalize_name(requirement.name)] in requirement.specifier
    assert all(dev[name] == version for name, version in runtime.items())
    assert all(dev[name] == version for name, version in build.items() if name in dev)


def test_api_and_package_have_one_version_source():
    from app.main import app
    from app.version import __version__
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    assert app.version == __version__
    assert project["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "app.version.__version__"
