"""Build a portable installer from this running distribution's public code."""
from __future__ import annotations

import io
import json
import tarfile
import tomllib
from importlib import metadata
from pathlib import Path


def _manifest(package):
    source = package.parent / "pyproject.toml"
    if source.is_file():
        content = source.read_text(encoding="utf-8")
        return tomllib.loads(content)["project"]["version"], content
    info = metadata.metadata("agent-observatory")
    version = info["Version"]
    extras = info.get_all("Provides-Extra") or []
    content = ('[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n'
        '[project]\nname = "agent-observatory"\nversion = ' + json.dumps(version) + '\n'
        'requires-python = ' + json.dumps(info.get("Requires-Python", ">=3.11")) + '\n'
        'dependencies = ' + json.dumps(info.get_all("Requires-Dist") or []) + '\n'
        '[project.optional-dependencies]\n' + ''.join(json.dumps(extra) + ' = []\n' for extra in extras) +
        '[project.scripts]\nagent-observatory = "observatory.__main__:main"\n'
        '[tool.setuptools.packages.find]\ninclude = ["observatory*"]\n'
        '[tool.setuptools.package-data]\nobservatory = ["index.html", "setup.js", "setup.css", "activity.js", "activity.css"]\n')
    return version, content


def installer_info():
    package = Path(__file__).resolve().parent
    version, _ = _manifest(package)
    filename = f"agent-observatory-{version}.tar.gz"
    return {"package_filename": filename,
            "install_command": f'python -m pip install "./{filename}[auto]"'}


def package_archive():
    package = Path(__file__).resolve().parent
    version, manifest = _manifest(package)
    root = f"agent-observatory-{version}"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        def add(name, data):
            info = tarfile.TarInfo(root + "/" + name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
        add("pyproject.toml", manifest.encode("utf-8"))
        for path in sorted(package.rglob("*")):
            if (path.is_file() and path.suffix in {".py", ".html", ".css", ".js"}
                    and path.resolve().is_relative_to(package)):
                add("observatory/" + path.relative_to(package).as_posix(), path.read_bytes())
    return output.getvalue(), root + ".tar.gz"
