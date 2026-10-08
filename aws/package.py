"""Build dependency-light Lambda and CodePipeline source archives offline."""
import argparse
import zipfile
from pathlib import Path


def package(root, output):
    root, output = Path(root), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output / "lambda.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in ["retail_forecast", "aws"]:
            for path in sorted((root / folder).rglob("*.py")):
                if "__pycache__" not in path.parts:
                    archive.write(path, path.relative_to(root))
    with zipfile.ZipFile(output / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in ["retail_forecast", "aws", "frontend", "tests", "infra"]:
            for path in sorted((root / folder).rglob("*")):
                if (path.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts)
                        and ".tfstate" not in path.name and not path.name.endswith((".tfplan", ".tfvars", ".tfvars.json"))):
                    archive.write(path, path.relative_to(root))
        for filename in ["Dockerfile", ".dockerignore", "buildspec.yml", "pyproject.toml"]:
            if (root / filename).exists():
                archive.write(root / filename, filename)
    return output / "lambda.zip", output / "source.zip"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="build")
    args = parser.parse_args()
    for path in package(Path(__file__).resolve().parents[1], args.output):
        print(path)


if __name__ == "__main__":
    main()
