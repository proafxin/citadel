import shutil
import subprocess
import tempfile
from pathlib import Path

import filetype
from markdownify import markdownify

OFFICE_EXTS = {"doc", "docx", "ppt", "pptx", "odt", "odp", "rtf"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"}
HTML_EXTS = {"html", "htm"}


def _detect_ext(data: bytes, filename: str) -> str:
    guess = filetype.guess(data)
    if guess is not None:
        return guess.extension
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def normalize_file(data: bytes, filename: str, profile_dir: str) -> tuple[str, bytes]:
    ext = _detect_ext(data, filename)
    if ext in OFFICE_EXTS:
        soffice = shutil.which("soffice")
        if soffice is None:
            raise RuntimeError("libreoffice 'soffice' not found on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / f"input.{ext}"
            src.write_bytes(data)
            subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation=file://{profile_dir}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    tmp,
                    str(src),
                ],
                check=True,
                capture_output=True,
            )
            return "office-pdf", (Path(tmp) / "input.pdf").read_bytes()
    if ext == "pdf":
        return "pdf", data
    if ext in IMAGE_EXTS:
        return f"image:{ext}", data
    if ext in HTML_EXTS:
        return "text", markdownify(data.decode("utf-8")).encode()
    return "text", data.decode("utf-8", errors="replace").encode()
