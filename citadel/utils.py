import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import filetype
import xmltodict
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin


def _render_math(content: str, options: dict) -> str:
    if options.get("display_mode"):
        return f"<math>{html.escape(content)}</math>"
    return f"\\({html.escape(content)}\\)"


_MARKDOWN = MarkdownIt().enable("table").use(dollarmath_plugin, renderer=_render_math)

PRESENTATION_NATIVE_EXTS = {"pptx"}
PRESENTATION_CONVERT_EXTS = {"ppt", "odp"}
DOC_CONVERT_EXTS = {"doc", "rtf"}
DOC_HTML_EXTS = {"docx", "odt"}
MARKDOWN_EXTS = {"md", "markdown"}
HTML_EXTS = {"html", "htm", "xhtml"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"}
XML_EXTS = {"xml"}
SVG_EXTS = {"svg"}

_KNOWN_EXTS = (
    PRESENTATION_NATIVE_EXTS
    | PRESENTATION_CONVERT_EXTS
    | DOC_CONVERT_EXTS
    | DOC_HTML_EXTS
    | MARKDOWN_EXTS
    | HTML_EXTS
    | IMAGE_EXTS
    | XML_EXTS
    | SVG_EXTS
    | {"csv", "tsv", "tab", "json", "pdf", "epub"}
)
_SNIFF_BYTES = 65536
_SNIFF_LINES = 20
_MIN_DELIMITED_LINES = 2
_PASSTHROUGH = {"csv": "csv", "tsv": "tsv", "tab": "tsv", "json": "json", "pdf": "pdf"}
_MARKUP_RE = re.compile(r"^\s*(<!doctype html|<html|<\?xml|<[a-z]+[\s>/])", re.IGNORECASE)


def _detect_ext(data: bytes, filename: str) -> str:
    guess = filetype.guess(data)
    if guess is not None:
        return guess.extension
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _delimited_ext(lines: list[str]) -> str | None:
    for separator, ext in ((",", "csv"), ("\t", "tsv")):
        counts = {line.count(separator) for line in lines}
        if len(counts) == 1 and counts.pop() > 0:
            return ext
    return None


def _sniff_text_ext(data: bytes) -> str | None:
    text = data[:_SNIFF_BYTES].decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if text[0] in "{[":
        return "json"
    if _MARKUP_RE.match(text):
        return "html"
    lines = [line for line in text.splitlines()[:_SNIFF_LINES] if line.strip()]
    return _delimited_ext(lines) if len(lines) >= _MIN_DELIMITED_LINES else None


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


def _pandoc_to_html(data: bytes, src_ext: str) -> tuple[bytes, dict[str, bytes]]:
    pandoc = shutil.which("pandoc")
    if pandoc is None:
        raise RuntimeError("pandoc not found on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"input.{src_ext}"
        src.write_bytes(data)
        media_dir = Path(tmp) / "media_out"
        html_bytes = subprocess.run(
            [pandoc, str(src), "-t", "html", f"--extract-media={media_dir}"],
            check=True,
            capture_output=True,
        ).stdout
        media = {path.name: path.read_bytes() for path in media_dir.rglob("*") if path.is_file()}
        return html_bytes, media


def _xml_kind(data: bytes, ext: str) -> tuple[str, bytes, dict[str, bytes]] | None:
    if ext not in XML_EXTS:
        return None
    return "json", json.dumps(xmltodict.parse(data)).encode(), {}


def _svg_kind(data: bytes, ext: str, profile_dir: str) -> tuple[str, bytes, dict[str, bytes]] | None:
    if ext not in SVG_EXTS:
        return None
    return "image:png", _soffice_convert(data, ext, "png", profile_dir), {}


def _markup_kind(data: bytes, ext: str, profile_dir: str) -> tuple[str, bytes, dict[str, bytes]] | None:
    if ext in HTML_EXTS:
        return "html", data, {}
    if ext in MARKDOWN_EXTS:
        return "html", _MARKDOWN.render(data.decode("utf-8")).encode(), {}
    if ext in DOC_CONVERT_EXTS:
        html_bytes, media = _pandoc_to_html(_soffice_convert(data, ext, "docx", profile_dir), "docx")
        return "html_pandoc", html_bytes, media
    if ext in DOC_HTML_EXTS or ext == "epub":
        html_bytes, media = _pandoc_to_html(data, ext)
        return "html_pandoc", html_bytes, media
    return None


def _presentation_kind(data: bytes, ext: str, profile_dir: str) -> tuple[str, bytes, dict[str, bytes]] | None:
    if ext in PRESENTATION_NATIVE_EXTS:
        return "pptx", data, {}
    if ext in PRESENTATION_CONVERT_EXTS:
        return "pptx", _soffice_convert(data, ext, "pptx", profile_dir), {}
    return None


def _converted_kind(data: bytes, ext: str, profile_dir: str) -> tuple[str, bytes, dict[str, bytes]] | None:
    xml = _xml_kind(data, ext)
    if xml is not None:
        return xml
    svg = _svg_kind(data, ext, profile_dir)
    if svg is not None:
        return svg
    markup = _markup_kind(data, ext, profile_dir)
    if markup is not None:
        return markup
    return _presentation_kind(data, ext, profile_dir)


def normalize_file(data: bytes, filename: str, profile_dir: str) -> tuple[str, bytes, dict[str, bytes]]:
    ext = _detect_ext(data, filename)
    if ext not in _KNOWN_EXTS:
        ext = _sniff_text_ext(data) or ext
    passthrough = _PASSTHROUGH.get(ext)
    if passthrough is not None:
        return passthrough, data, {}
    if ext in IMAGE_EXTS:
        return f"image:{ext}", data, {}
    converted = _converted_kind(data, ext, profile_dir)
    if converted is not None:
        return converted
    return "text", data.decode("utf-8", errors="replace").encode(), {}
