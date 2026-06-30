import shutil
import subprocess
import tempfile
from pathlib import Path

import filetype

OFFICE_EXTS = {"doc", "docx", "ppt", "pptx", "odt", "odp", "rtf"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"}
SPREADSHEET_NATIVE_EXTS = {"xlsx", "xlsm"}
SPREADSHEET_CONVERT_EXTS = {"xls", "xlsb", "ods", "fods"}


def _detect_ext(data: bytes, filename: str) -> str:
    guess = filetype.guess(data)
    if guess is not None:
        return guess.extension
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _soffice_convert(data: bytes, src_ext: str, target: str, profile_dir: str) -> bytes:
    soffice = shutil.which("soffice")
    if soffice is None:
        raise RuntimeError("libreoffice 'soffice' not found on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"input.{src_ext}"
        src.write_bytes(data)
        subprocess.run(
            [
                soffice,
                f"-env:UserInstallation=file://{profile_dir}",
                "--headless",
                "--convert-to",
                target,
                "--outdir",
                tmp,
                str(src),
            ],
            check=True,
            capture_output=True,
        )
        return (Path(tmp) / f"input.{target}").read_bytes()


def normalize_file(data: bytes, filename: str, profile_dir: str) -> tuple[str, bytes]:
    ext = _detect_ext(data, filename)
    if ext in SPREADSHEET_NATIVE_EXTS:
        return "xlsx", data
    if ext in SPREADSHEET_CONVERT_EXTS:
        return "xlsx", _soffice_convert(data, ext, "xlsx", profile_dir)
    if ext in OFFICE_EXTS:
        return "office-pdf", _soffice_convert(data, ext, "pdf", profile_dir)
    if ext == "pdf":
        return "pdf", data
    if ext in IMAGE_EXTS:
        return f"image:{ext}", data
    return "text", data.decode("utf-8", errors="replace").encode()
