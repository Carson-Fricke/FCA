"""Stage only current wheel inputs so unrelated source edits retain image caches.

Package selection and non-Python data come from pyproject.toml, the same contract
used by setuptools. The resulting source tree builds the complete distribution;
this is cache input selection, not a second product-specific wheel definition.
"""

import argparse
import fnmatch
import shutil
import tomllib
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    configuration = tomllib.loads(
        (args.root / "pyproject.toml").read_text(encoding="utf-8")
    )["tool"]["setuptools"]
    discovery = configuration["packages"]["find"]
    if discovery["where"] != ["src"] or not discovery["namespaces"]:
        raise ValueError("Image staging requires the current src namespace layout")
    if configuration["include-package-data"]:
        raise ValueError("Image staging requires explicit package-data inputs")
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.root / "src"
    selected: set[Path] = set()
    for directory in source.rglob("*"):
        if not directory.is_dir():
            continue
        package = ".".join(directory.relative_to(source).parts)
        included = any(fnmatch.fnmatchcase(package, p) for p in discovery["include"])
        excluded = any(fnmatch.fnmatchcase(package, p) for p in discovery["exclude"])
        if included and not excluded:
            selected.update(directory.glob("*.py"))
    for package, patterns in configuration["package-data"].items():
        directory = source.joinpath(*package.split("."))
        for pattern in patterns:
            selected.update(path for path in directory.glob(pattern) if path.is_file())
    for path in sorted(selected):
        destination = args.output / "src" / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    print(f"Staged {len(selected)} current wheel inputs")


if __name__ == "__main__":
    main()
