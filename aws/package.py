"""Build Lambda and explicitly allowlisted delivery archives without network access."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

EXCLUDED_SUFFIXES = (".tfstate", ".tfstate.backup", ".tfplan", ".tfvars", ".tfvars.json", ".pyc", ".zip", ".pem")


def eligible(path, root):
    parts = path.relative_to(root).parts
    return (path.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in parts)
            and not path.name.endswith(EXCLUDED_SUFFIXES) and ".tfstate" not in path.name)


def package(root, output):
    root, output = Path(root), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output / "lambda.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in ("orderflow", "aws"):
            for path in sorted((root / folder).rglob("*.py")):
                if eligible(path, root):
                    archive.write(path, path.relative_to(root).as_posix())
    with zipfile.ZipFile(output / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in ("orderflow", "aws", "frontend", "tests", "infra", "docs"):
            for path in sorted((root / folder).rglob("*")):
                if eligible(path, root):
                    archive.write(path, path.relative_to(root).as_posix())
        for name in ("Dockerfile", ".dockerignore", "buildspec.yml", "pyproject.toml", "README.md", "CONTRIBUTING.md"):
            path = root / name
            if path.exists():
                archive.write(path, name)
        lock = root / "infra/.terraform.lock.hcl"
        if lock.is_file():
            archive.write(lock, "infra/.terraform.lock.hcl")
    manifest = {name: {"sha256": hashlib.sha256((output / name).read_bytes()).hexdigest(),
                       "bytes": (output / name).stat().st_size} for name in ("lambda.zip", "source.zip")}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return output / "lambda.zip", output / "source.zip"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="build")
    args = parser.parse_args()
    for path in package(Path(__file__).resolve().parents[1], args.output):
        print(path)


if __name__ == "__main__":
    main()
