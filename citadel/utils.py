import html
import shutil
import subprocess
import tempfile
from pathlib import Path

import filetype
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin


def _render_math(content: str, options: dict) -> str:
    if options.get("display_mode"):
        return f"<math>{html.escape(content)}</math>"
    return f"\\({html.escape(content)}\\)"


_MARKDOWN = MarkdownIt().use(dollarmath_plugin, renderer=_render_math)

OFFICE_PDF_EXTS = {"doc", "ppt", "pptx", "odp"}
DOC_HTML_EXTS = {"docx", "odt", "rtf"}
MARKDOWN_EXTS = {"md", "markdown"}
HTML_EXTS = {"html", "htm", "xhtml"}
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


def _pandoc_to_html(data: bytes, src_ext: str) -> bytes:
    pandoc = shutil.which("pandoc")
    if pandoc is None:
        raise RuntimeError("pandoc not found on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"input.{src_ext}"
        src.write_bytes(data)
        return subprocess.run([pandoc, str(src), "-t", "html"], check=True, capture_output=True).stdout


def normalize_file(data: bytes, filename: str, profile_dir: str) -> tuple[str, bytes]:
    ext = _detect_ext(data, filename)
    if ext in SPREADSHEET_NATIVE_EXTS:
        return "xlsx", data
    if ext in SPREADSHEET_CONVERT_EXTS:
        return "xlsx", _soffice_convert(data, ext, "xlsx", profile_dir)
    if ext == "csv":
        return "csv", data
    if ext in {"tsv", "tab"}:
        return "tsv", data
    if ext == "json":
        return "json", data
    if ext in HTML_EXTS:
        return "html", data
    if ext in MARKDOWN_EXTS:
        return "html", _MARKDOWN.render(data.decode("utf-8")).encode()
    if ext in DOC_HTML_EXTS or ext == "epub":
        return "html", _pandoc_to_html(data, ext)
    if ext in OFFICE_PDF_EXTS:
        return "office-pdf", _soffice_convert(data, ext, "pdf", profile_dir)
    if ext == "pdf":
        return "pdf", data
    if ext in IMAGE_EXTS:
        return f"image:{ext}", data
    return "text", data.decode("utf-8", errors="replace").encode()
