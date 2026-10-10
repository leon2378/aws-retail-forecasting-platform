"""Verify release boundaries offline; Terraform's provider tests cover resources."""
import importlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from aws.package import package

ROOT = Path(__file__).resolve().parents[1]
POSIX_SHELL = shutil.which("sh")


class ReleaseBoundaryTests(unittest.TestCase):
    def test_lambda_archive_contains_every_handler_without_local_data(self):
        parent = ROOT / "build"
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as folder:
            archive_path, _ = package(ROOT, folder)
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                for handler in ("api", "worker", "outbox", "drill"):
                    self.assertIn(f"aws/handlers/{handler}.py", names)
                self.assertIn("aws/repository.py", names)
                self.assertIn("orderflow/engine.py", names)
                self.assertFalse(any(name.startswith(("data/", "build/", "infra/", "frontend/")) for name in names))
                self.assertFalse(any(name.endswith((".tfvars", ".tfstate", ".db", ".sqlite3")) for name in names))

    def test_importing_lambda_handlers_never_creates_services_or_seeds_inventory(self):
        with patch("boto3.client", side_effect=AssertionError("Cloud calls are forbidden during import")), patch("boto3.resource", side_effect=AssertionError("Cloud calls are forbidden during import")):
            for name in ("api", "worker", "outbox", "drill"):
                importlib.reload(importlib.import_module("aws.handlers." + name))

    def test_delivery_validates_before_packaging_and_deployment_is_separate(self):
        build = (ROOT / "buildspec.yml").read_text()
        deploy = (ROOT / "aws/buildspec-deploy.yml").read_text()
        self.assertIn("python -m unittest discover", build)
        self.assertIn("node --test", build)
        self.assertIn("terraform -chdir=infra test", build)
        self.assertLess(build.index("python -m unittest discover"), build.index("python -m aws.package"))
        self.assertNotIn("update-function-code", build)
        self.assertNotIn("terraform apply", build)
        self.assertIn("for component in api worker outbox drill", deploy)
        self.assertIn("sha256", deploy)
        self.assertIn("aws s3 sync frontend/", deploy)
        self.assertNotIn("aws dynamodb", deploy)
        self.assertNotIn("terraform apply", deploy)


@unittest.skipUnless(POSIX_SHELL, "A POSIX shell is required for deployment failure tests.")
class DeploymentFailureTests(unittest.TestCase):
    def run_deployment(self, failure="", valid_manifest=True):
        """Run the actual release commands with an in-process fake AWS function.

        No AWS executable is invoked. The shell function records every attempted
        operation and injects failure before any infrastructure can be contacted.
        """
        parent = ROOT / "build"
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as folder:
            working = Path(folder)
            (working / "build").mkdir()
            archive = b"offline release bytes"
            (working / "build/lambda.zip").write_bytes(archive)
            digest = hashlib.sha256(archive).hexdigest() if valid_manifest else "0" * 64
            (working / "build/manifest.json").write_text(json.dumps({"lambda.zip": {"sha256": digest}}))
            lines = (ROOT / "aws/buildspec-deploy.yml").read_text().splitlines()
            commands, block = [], False
            for line in lines:
                if line == "      - |":
                    block = True
                elif block and line.startswith("        "):
                    commands.append(line[8:])
                elif line.startswith("      - "):
                    block = False
                    commands.append(line[8:])
            fake_tools = '''
python() { "$PYTHON_EXE" "$@"; }
aws() {
  printf '%s\\n' "$*" >> "$CALL_LOG"
  case "$*" in
    "lambda update-function-code --function-name orderflow-api "*)
      if [ "$FAIL_POINT" = "api_update" ]; then return 42; fi ;;
    "lambda wait function-updated --function-name orderflow-api")
      if [ "$FAIL_POINT" = "api_wait" ]; then return 43; fi ;;
  esac
  return 0
}
'''
            environment = {**os.environ, "PYTHON_EXE": Path(sys.executable).as_posix(),
                           "CALL_LOG": (working / "calls.txt").as_posix(),
                           "FAIL_POINT": failure, "PROJECT_NAME": "orderflow",
                           "FRONTEND_BUCKET": "offline-example", "DISTRIBUTION_ID": "OFFLINE"}
            result = subprocess.run([POSIX_SHELL, "-c", fake_tools + "\n".join(commands)],
                                    cwd=working, env=environment, capture_output=True, text=True, timeout=20)
            calls = (working / "calls.txt").read_text().splitlines() if (working / "calls.txt").exists() else []
            return result, calls

    def test_manifest_mismatch_stops_before_any_code_update(self):
        result, calls = self.run_deployment(valid_manifest=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, [])

    def test_failed_code_update_stops_before_wait_or_other_components(self):
        result, calls = self.run_deployment(failure="api_update")
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("lambda update-function-code --function-name orderflow-api "))

    def test_failed_wait_stops_before_other_components_or_publication(self):
        result, calls = self.run_deployment(failure="api_wait")
        self.assertEqual(result.returncode, 43, result.stdout + result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], "lambda wait function-updated --function-name orderflow-api")

    def test_successful_release_waits_for_every_function_before_frontend(self):
        result, calls = self.run_deployment()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(calls), 10)
        for offset, component in enumerate(("api", "worker", "outbox", "drill")):
            self.assertTrue(calls[offset * 2].startswith(f"lambda update-function-code --function-name orderflow-{component} "))
            self.assertEqual(calls[offset * 2 + 1], f"lambda wait function-updated --function-name orderflow-{component}")
        self.assertTrue(calls[8].startswith("s3 sync frontend/ "))
        self.assertTrue(calls[9].startswith("cloudfront create-invalidation "))


if __name__ == "__main__":
    unittest.main()
