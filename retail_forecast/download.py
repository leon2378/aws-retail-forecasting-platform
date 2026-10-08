"""Download the three M5 source files through the official Kaggle client."""

import csv
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import stat
import tempfile
import zipfile


COMPETITION = "m5-forecasting-accuracy"
M5_FILES = ("sales_train_evaluation.csv", "calendar.csv", "sell_prices.csv")
RULES_URL = f"https://www.kaggle.com/competitions/{COMPETITION}/rules"
# Each official M5 CSV is well below this limit; bound extraction before writing.
MAX_FILE_BYTES = 2 * 1024**3
MAX_COMPRESSION_RATIO = 1000
MAX_CSV_LINE_BYTES = 1024**2
REQUIRED_HEADERS = {
    "sales_train_evaluation.csv": {
        "id", "item_id", "dept_id", "cat_id", "store_id", "state_id", "d_1", "d_1941"
    },
    "calendar.csv": {"date", "wm_yr_wk", "d"},
    "sell_prices.csv": {"store_id", "item_id", "wm_yr_wk", "sell_price"},
}


class DownloadError(ValueError):
    """A safe, actionable download error that never includes client credentials."""


def _load_api():
    # Some client versions authenticate during import and print diagnostics.
    # The application supplies its own safe guidance instead of forwarding it.
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            from kaggle.api.kaggle_api_extended import KaggleApi
    except ModuleNotFoundError:
        raise DownloadError(
            'Install the Kaggle client with python -m pip install ".[data]" before downloading M5.'
        ) from None
    except (Exception, SystemExit):
        raise DownloadError("Unable to load the Kaggle client. Reinstall the data extra and try again.") from None
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            api = KaggleApi()
            api.authenticate()
    except (Exception, SystemExit):
        raise DownloadError(
            "Kaggle authentication is not configured or has expired. Run kaggle auth login "
            "and complete the browser sign-in, then retry. Keep API tokens outside this repository."
        ) from None
    return api


def _valid_csv(path, filename):
    """Check a bounded header and first record, without loading the large CSVs."""
    try:
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE_BYTES:
            return False
        with path.open("rb") as stream:
            lines = [stream.readline(MAX_CSV_LINE_BYTES + 1) for _ in range(2)]
        if any(not line or len(line) > MAX_CSV_LINE_BYTES for line in lines):
            return False
        rows = list(csv.reader(line.decode("utf-8-sig") for line in lines))
        header, first = rows
        return (
            len(header) == len(set(header))
            and REQUIRED_HEADERS[filename].issubset(header)
            and len(first) == len(header)
            and any(first)
        )
    except (OSError, UnicodeError, csv.Error, ValueError):
        return False


def _unpack_download(directory, filename):
    entries = list(directory.iterdir())
    if len(entries) != 1 or not entries[0].is_file() or entries[0].is_symlink():
        raise DownloadError(f"Kaggle did not return exactly one complete download for {filename}. Retry the download.")
    downloaded = entries[0]
    if downloaded.stat().st_size > MAX_FILE_BYTES:
        raise DownloadError(f"The download for {filename} exceeds the M5 file size limit.")
    if zipfile.is_zipfile(downloaded):
        output = directory / "extracted.csv"
        try:
            with zipfile.ZipFile(downloaded) as archive:
                members = archive.infolist()
                if len(members) != 1 or members[0].filename != filename:
                    raise DownloadError(f"The archive must contain only {filename} at its root.")
                member = members[0]
                kind = stat.S_IFMT(member.external_attr >> 16)
                if member.is_dir() or kind not in (0, stat.S_IFREG) or member.flag_bits & 1:
                    raise DownloadError(f"The archive entry for {filename} must be a regular, unencrypted file.")
                if (
                    not 0 < member.file_size <= MAX_FILE_BYTES
                    or member.compress_size <= 0
                    or member.file_size > member.compress_size * MAX_COMPRESSION_RATIO
                ):
                    raise DownloadError(f"The archive for {filename} exceeds the M5 extraction limits.")
                written = 0
                with archive.open(member) as source, output.open("xb") as target:
                    while chunk := source.read(1024**2):
                        written += len(chunk)
                        if written > member.file_size or written > MAX_FILE_BYTES:
                            raise DownloadError(f"The archive for {filename} exceeds the M5 extraction limits.")
                        target.write(chunk)
                if written != member.file_size:
                    raise DownloadError(f"The archive for {filename} is incomplete. Retry the download.")
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise DownloadError(f"The archive for {filename} is invalid or incomplete. Retry the download.") from None
        downloaded = output
    elif downloaded.name != filename:
        raise DownloadError(f"Kaggle returned an unexpected file instead of {filename}. Retry the download.")
    if not _valid_csv(downloaded, filename):
        raise DownloadError(f"The downloaded {filename} is empty or has an unexpected CSV schema. Retry the download.")
    return downloaded


def _download_error(exc, filename):
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if str(status) == "401":
        return DownloadError("Kaggle rejected the credentials (401). Run kaggle auth login again, then retry.")
    if str(status) == "403":
        return DownloadError(
            "Kaggle denied access to M5 (403). Sign in to the same Kaggle account and "
            f"accept the competition rules at {RULES_URL}, then retry."
        )
    return DownloadError(
        f"Could not download {filename} from Kaggle. Check your connection and account access, then retry. "
        "Completed files are retained; temporary files are removed."
    )


def download_m5(folder="data/m5", force=False):
    """Download and validate M5 CSVs; retain completed files for safe retries.

    Authentication uses the official client's credential locations and environment.
    No credentials are copied into the project. Validation here checks the file
    envelope; the M5 importer performs full data validation before use.
    """
    if not isinstance(force, bool):
        raise DownloadError("force must be true or false.")
    folder = Path(folder).expanduser().resolve()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise DownloadError("Unable to prepare the M5 download folder. Choose a writable directory.") from None
    skipped = [name for name in M5_FILES if not force and _valid_csv(folder / name, name)]
    needed = [name for name in M5_FILES if name not in skipped]
    api = _load_api() if needed else None
    downloaded = []
    for filename in needed:
        try:
            # One isolated directory per file makes plain and ZIP results unambiguous.
            # os.replace keeps an earlier complete CSV intact if a forced refresh fails.
            with tempfile.TemporaryDirectory(prefix=".m5-download-", dir=folder) as temporary:
                directory = Path(temporary)
                try:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        api.competition_download_file(
                            COMPETITION, filename, path=str(directory), force=True, quiet=True
                        )
                except (Exception, SystemExit) as exc:
                    raise _download_error(exc, filename) from None
                ready = _unpack_download(directory, filename)
                os.replace(ready, folder / filename)
            downloaded.append(filename)
        except OSError:
            raise DownloadError(
                f"Unable to save {filename}. Check the download folder permissions and available disk space."
            ) from None
    return {
        "folder": str(folder),
        "files": {name: str(folder / name) for name in M5_FILES},
        "downloaded": downloaded,
        "skipped": skipped,
    }
