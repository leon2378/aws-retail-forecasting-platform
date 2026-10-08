import csv
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import stat
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch
import zipfile

from retail_forecast.download import COMPETITION, DownloadError, M5_FILES, _load_api, download_m5


def fixture_csv(filename):
    headers = {
        "sales_train_evaluation.csv": [
            "id", "item_id", "dept_id", "cat_id", "store_id", "state_id", "d_1", "d_1941"
        ],
        "calendar.csv": ["date", "wm_yr_wk", "d"],
        "sell_prices.csv": ["store_id", "item_id", "wm_yr_wk", "sell_price"],
    }
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(headers[filename])
    writer.writerow(["1"] * len(headers[filename]))
    return stream.getvalue()


class FakeApi:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def competition_download_file(self, competition, filename, path, force, quiet):
        self.calls.append((competition, filename, force, quiet))
        if self.callback:
            self.callback(Path(path), filename)
        elif filename == "calendar.csv":
            (Path(path) / filename).write_text(fixture_csv(filename), encoding="utf-8")
        else:
            with zipfile.ZipFile(Path(path) / (filename + ".zip"), "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(filename, fixture_csv(filename))


class DownloadTests(unittest.TestCase):
    def test_missing_client_explains_optional_dependency_without_leaking_exception(self):
        with patch("builtins.__import__", side_effect=ModuleNotFoundError("private-client-detail")):
            with self.assertRaises(DownloadError) as raised:
                _load_api()
        self.assertIn(".[data]", str(raised.exception))
        self.assertNotIn("private-client-detail", str(raised.exception))

    def test_authentication_failure_and_system_exit_do_not_print_client_secrets(self):
        for failure in (RuntimeError("private-client-detail"), SystemExit(1)):
            class UnauthenticatedApi:
                def authenticate(self):
                    print("private-client-detail")
                    raise failure
            client_module = ModuleType("kaggle.api.kaggle_api_extended")
            client_module.KaggleApi = UnauthenticatedApi
            modules = {
                "kaggle": ModuleType("kaggle"),
                "kaggle.api": ModuleType("kaggle.api"),
                "kaggle.api.kaggle_api_extended": client_module,
            }
            output, errors = io.StringIO(), io.StringIO()
            with self.subTest(failure=type(failure).__name__), patch.dict("sys.modules", modules):
                with redirect_stdout(output), redirect_stderr(errors):
                    with self.assertRaises(DownloadError) as raised:
                        _load_api()
            self.assertIn("auth login", str(raised.exception))
            self.assertNotIn("private-client-detail", str(raised.exception))
            self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_plain_and_zip_downloads_only_three_source_files(self):
        api = FakeApi()
        with tempfile.TemporaryDirectory() as temporary, patch("retail_forecast.download._load_api", return_value=api):
            folder = Path(temporary) / "m5"
            result = download_m5(folder)
            self.assertEqual(result["downloaded"], list(M5_FILES))
            self.assertEqual(result["skipped"], [])
            self.assertEqual([call[1] for call in api.calls], list(M5_FILES))
            self.assertTrue(all(call[0] == COMPETITION and call[2:] == (True, True) for call in api.calls))
            self.assertEqual({path.name for path in folder.iterdir()}, set(M5_FILES))
            self.assertTrue(all(Path(path).is_file() for path in result["files"].values()))

    def test_valid_existing_files_need_neither_credentials_nor_network(self):
        with tempfile.TemporaryDirectory() as temporary, patch("retail_forecast.download._load_api") as loader:
            folder = Path(temporary)
            for name in M5_FILES:
                (folder / name).write_text(fixture_csv(name), encoding="utf-8")
            result = download_m5(folder)
            self.assertEqual(result["downloaded"], [])
            self.assertEqual(result["skipped"], list(M5_FILES))
            loader.assert_not_called()

    def test_invalid_existing_csv_is_redownloaded(self):
        api = FakeApi()
        with tempfile.TemporaryDirectory() as temporary, patch("retail_forecast.download._load_api", return_value=api):
            folder = Path(temporary)
            for name in M5_FILES:
                (folder / name).write_text(fixture_csv(name), encoding="utf-8")
            (folder / "calendar.csv").write_text("<html>Access denied</html>")
            result = download_m5(folder)
            self.assertEqual(result["downloaded"], ["calendar.csv"])
            self.assertEqual(len(api.calls), 1)

    def test_archive_traversal_extra_entries_and_symlinks_are_rejected(self):
        for variant in ("../sales_train_evaluation.csv", "nested/sales_train_evaluation.csv", "extra", "symlink"):
            def malicious(folder, filename):
                with zipfile.ZipFile(folder / "download.zip", "w") as archive:
                    if variant == "extra":
                        archive.writestr(filename, fixture_csv(filename))
                        archive.writestr("extra.csv", "unexpected")
                    elif variant == "symlink":
                        member = zipfile.ZipInfo(filename)
                        member.create_system = 3
                        member.external_attr = (stat.S_IFLNK | 0o777) << 16
                        archive.writestr(member, fixture_csv(filename))
                    else:
                        archive.writestr(variant, fixture_csv(filename))
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary) / "m5"
                with patch("retail_forecast.download._load_api", return_value=FakeApi(malicious)):
                    with self.assertRaises(DownloadError):
                        download_m5(folder)
                self.assertEqual(list(folder.iterdir()), [])
                self.assertFalse((Path(temporary) / M5_FILES[0]).exists())

    def test_extraction_limit_rejects_bomb_before_writing(self):
        def compressed(folder, filename):
            with zipfile.ZipFile(folder / "download.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(filename, "x" * 8192)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch("retail_forecast.download._load_api", return_value=FakeApi(compressed)):
                with patch("retail_forecast.download.MAX_FILE_BYTES", 4096):
                    with self.assertRaisesRegex(DownloadError, "limits"):
                        download_m5(folder)
            self.assertEqual(list(folder.iterdir()), [])

    def test_failed_forced_refresh_preserves_old_csv_and_cleans_partial(self):
        def partial(folder, filename):
            (folder / filename).write_text("partial")
            raise RuntimeError("secret-token-never-disclose")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            target = folder / M5_FILES[0]
            original = fixture_csv(M5_FILES[0])
            target.write_text(original, encoding="utf-8")
            with patch("retail_forecast.download._load_api", return_value=FakeApi(partial)):
                with self.assertRaises(DownloadError) as raised:
                    download_m5(folder, force=True)
            self.assertNotIn("secret-token", str(raised.exception))
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual(list(folder.iterdir()), [target])

    def test_bad_new_schema_preserves_old_csv(self):
        def wrong_csv(folder, filename):
            (folder / filename).write_text("error,message\n403,denied\n")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            target = folder / M5_FILES[0]
            target.write_text(fixture_csv(M5_FILES[0]), encoding="utf-8")
            old_bytes = target.read_bytes()
            with patch("retail_forecast.download._load_api", return_value=FakeApi(wrong_csv)):
                with self.assertRaisesRegex(DownloadError, "schema"):
                    download_m5(folder, force=True)
            self.assertEqual(target.read_bytes(), old_bytes)
            self.assertEqual(list(folder.iterdir()), [target])

    def test_401_and_403_guidance_sanitizes_sensitive_client_errors(self):
        for status, guidance in ((401, "auth login"), (403, "/rules")):
            def denied(folder, filename):
                error = RuntimeError("secret-token-never-disclose")
                error.response = type("Response", (), {"status_code": status})()
                raise error
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                with patch("retail_forecast.download._load_api", return_value=FakeApi(denied)):
                    with self.assertRaises(DownloadError) as raised:
                        download_m5(temporary)
                self.assertIn(guidance, str(raised.exception))
                self.assertNotIn("secret-token", str(raised.exception))
                self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_interrupted_client_system_exit_becomes_actionable_error(self):
        def exited(folder, filename):
            raise SystemExit(1)
        with tempfile.TemporaryDirectory() as temporary:
            with patch("retail_forecast.download._load_api", return_value=FakeApi(exited)):
                with self.assertRaisesRegex(DownloadError, "Could not download"):
                    download_m5(temporary)
            self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
