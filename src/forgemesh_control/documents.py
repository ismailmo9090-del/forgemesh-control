"""Document intake and extraction.

Supported: TXT, Markdown, CSV, PDF (``pdftotext``), DOCX (OOXML, stdlib) and
images (tesseract when installed). Archives are extracted with traversal,
size and entry-count limits. Missing fields are left empty and reported as
missing: the extractor never invents data.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from forgemesh_control.config import ControlSettings
from forgemesh_control.security import SecurityError, ensure_within, safe_filename

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".rst", ".log", ".json"}
PDF_SUFFIXES = {".pdf"}
DOCX_SUFFIXES = {".docx"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
ARCHIVE_SUFFIXES = {".zip"}
SUPPORTED_SUFFIXES = (
    TEXT_SUFFIXES | PDF_SUFFIXES | DOCX_SUFFIXES | IMAGE_SUFFIXES | ARCHIVE_SUFFIXES
)

FIELD_PATTERNS: dict[str, tuple[str, ...]] = {
    "problem_id": (r"problem\s*(?:id|no|number|code)", r"statement\s*id"),
    "title": (r"problem\s*title", r"title", r"theme"),
    "description": (r"description", r"problem\s*statement", r"background", r"overview"),
    "target_users": (r"target\s*users?", r"users?", r"beneficiaries", r"audience"),
    "challenges": (
        r"existing\s*challenges?",
        r"current\s*challenges?",
        r"pain\s*points?",
        r"problem\s*context",
    ),
    "outcomes": (
        r"expected\s*outcomes?",
        r"expected\s*results?",
        r"desired\s*outcomes?",
        r"goals?",
    ),
    "constraints": (r"constraints?", r"limitations?", r"restrictions?"),
    "required_technology": (
        r"required\s*(?:technology|technologies|tech\s*stack|stack)",
        r"technology\s*requirements?",
        r"preferred\s*stack",
    ),
    "evaluation_criteria": (
        r"evaluation\s*criteria",
        r"judging\s*criteria",
        r"assessment\s*criteria",
        r"scoring",
    ),
    "deliverables": (r"deliverables?", r"submission\s*requirements?", r"outputs?"),
    "conditions": (
        r"important\s*conditions?",
        r"eligibility",
        r"rules?",
        r"terms",
        r"additional\s*information",
    ),
}

_ALLOWED_ARCHIVE_SUFFIXES = TEXT_SUFFIXES | PDF_SUFFIXES | DOCX_SUFFIXES | IMAGE_SUFFIXES
_MAX_FIELD_CHARS = 4000


class DocumentError(RuntimeError):
    def __init__(self, message: str, *, code: str = "document_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class DocumentExtractor:
    def __init__(self, settings: ControlSettings) -> None:
        self.settings = settings
        self.uploads = settings.uploads_dir
        self.uploads.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- validation
    def validate(self, filename: str, size: int) -> str:
        name = safe_filename(filename)
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            allowed = ", ".join(sorted(SUPPORTED_SUFFIXES))
            raise DocumentError(f"Unsupported file type {suffix or '(none)'}. Allowed: {allowed}")
        limit = (
            self.settings.max_archive_bytes
            if suffix in ARCHIVE_SUFFIXES
            else self.settings.max_upload_bytes
        )
        if size <= 0:
            raise DocumentError("The uploaded file is empty.")
        if size > limit:
            raise DocumentError(
                f"File is larger than the {limit // (1024 * 1024)} MiB limit."
            )
        return name

    def save(self, filename: str, data: bytes) -> tuple[Path, str, int]:
        name = self.validate(filename, len(data))
        digest = hashlib.sha256(data).hexdigest()
        directory = self.uploads / digest[:2]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{digest[:12]}-{name}"
        target = ensure_within(self.uploads, target)
        if not target.exists():
            target.write_bytes(data)
        return target, digest, len(data)

    # --------------------------------------------------------------- extraction
    def extract(self, path: Path) -> dict[str, Any]:
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            return {"text": self._read_text(path), "kind": "text", "pages": None}
        if suffix in PDF_SUFFIXES:
            return self._extract_pdf(path)
        if suffix in DOCX_SUFFIXES:
            return self._extract_docx(path)
        if suffix in IMAGE_SUFFIXES:
            return self._extract_image(path)
        if suffix in ARCHIVE_SUFFIXES:
            entries = self.extract_archive(path)
            text = "\n\n".join(
                f"# {entry['filename']}\n{entry['text']}" for entry in entries
            )
            return {
                "text": text,
                "kind": "archive",
                "pages": None,
                "entries": len(entries),
            }
        raise DocumentError(f"Unsupported extraction type: {suffix}")

    @staticmethod
    def _read_text(path: Path) -> str:
        raw = path.read_bytes()
        for encoding in ("utf-8", "utf-16", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _extract_pdf(self, path: Path) -> dict[str, Any]:
        binary = shutil.which("pdftotext")
        if binary is None:
            raise DocumentError(
                "PDF extraction needs the poppler utility `pdftotext`. Install it with: "
                "sudo apt install poppler-utils"
            )
        result = subprocess.run(
            [binary, "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise DocumentError(
                "pdftotext could not read this PDF: "
                + result.stderr.decode(errors="replace")[:300]
            )
        text = result.stdout.decode("utf-8", errors="replace")
        if not text.strip():
            raise DocumentError(
                "The PDF contains no selectable text (it is probably scanned). "
                "Install tesseract and re-upload it as images for OCR."
            )
        return {"text": text, "kind": "pdf", "pages": text.count("\f") + 1}

    def _extract_docx(self, path: Path) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo("word/document.xml")
                # OOXML is compressed; a small upload can inflate enormously.
                if info.file_size > self.settings.max_upload_bytes:
                    raise DocumentError(
                        "This .docx expands beyond the document size limit."
                    )
                xml = archive.read(info).decode("utf-8", errors="replace")
        except (KeyError, zipfile.BadZipFile) as exc:
            raise DocumentError("This .docx file is not a valid OOXML document.") from exc
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        text = (
            text.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
        )
        return {"text": text, "kind": "docx", "pages": None}

    def _extract_image(self, path: Path) -> dict[str, Any]:
        binary = shutil.which("tesseract")
        if binary is None:
            raise DocumentError(
                "Image OCR needs `tesseract`. Install it with: "
                "sudo apt install tesseract-ocr",
                code="ocr_unavailable",
            )
        result = subprocess.run(
            [binary, str(path), "stdout", "-l", "eng"],
            capture_output=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            raise DocumentError(
                "tesseract could not read this image: "
                + result.stderr.decode(errors="replace")[:300]
            )
        return {
            "text": result.stdout.decode("utf-8", errors="replace"),
            "kind": "image-ocr",
            "pages": 1,
        }

    # --------------------------------------------------------------- archives
    def extract_archive(self, path: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        total = 0
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > self.settings.max_archive_entries:
                    raise DocumentError(
                        f"Archive has {len(infos)} entries; the limit is "
                        f"{self.settings.max_archive_entries}."
                    )
                for info in infos:
                    if info.is_dir():
                        continue
                    member = Path(info.filename)
                    if member.is_absolute() or ".." in member.parts:
                        raise DocumentError(
                            f"Archive entry escapes the extraction directory: {info.filename}",
                            code="unsafe_archive",
                        )
                    suffix = member.suffix.lower()
                    if suffix not in _ALLOWED_ARCHIVE_SUFFIXES:
                        continue
                    total += info.file_size
                    if total > self.settings.max_archive_bytes:
                        raise DocumentError("Extracted archive exceeds the size limit.")
                    if info.file_size > self.settings.max_upload_bytes:
                        raise DocumentError(
                            f"Archive entry too large: {info.filename}"
                        )
                    data = archive.read(info)
                    target, _, _ = self.save(member.name, data)
                    extracted = self.extract(target)
                    entries.append(
                        {
                            "filename": member.name,
                            "path": str(target),
                            "kind": extracted["kind"],
                            "text": extracted["text"],
                        }
                    )
        except zipfile.BadZipFile as exc:
            raise DocumentError("This .zip file is not a valid ZIP archive.") from exc
        if not entries:
            raise DocumentError("The archive contains no supported documents.")
        return entries


# --------------------------------------------------------------------- problem
def _clean_excerpt(value: str) -> str:
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()[:_MAX_FIELD_CHARS]


def _section_after_label(lines: Iterable[str], pattern: str, index: int) -> str:
    """Return the text that follows a label, stopping at the next known label."""
    collected: list[str] = []
    for line in list(lines)[index + 1 :]:
        stripped = line.strip()
        if not stripped:
            if collected:
                break
            continue
        if any(
            re.match(rf"^(?:{inner})\s*[:\-–]", stripped, re.IGNORECASE)
            for patterns in FIELD_PATTERNS.values()
            for inner in patterns
        ):
            break
        collected.append(stripped)
        if len(" ".join(collected)) > _MAX_FIELD_CHARS:
            break
    return _clean_excerpt(" ".join(collected))


def extract_problem_fields(text: str) -> dict[str, Any]:
    """Heuristic, evidence-preserving extraction of hackathon statement fields."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    found: dict[str, Any] = {}
    evidence: dict[str, str] = {}

    for field, patterns in FIELD_PATTERNS.items():
        for index, line in enumerate(lines):
            if len(line) > 300:
                continue
            for pattern in patterns:
                match = re.match(rf"^\s*(?:#+\s*)?(?:{pattern})\s*[:\-–]\s*(.*)$", line, re.I)
                if not match:
                    continue
                inline = _clean_excerpt(match.group(1))
                body = inline or _section_after_label(lines, pattern, index)
                if body:
                    found[field] = body
                    evidence[field] = line.strip()[:300]
                break
            if field in found:
                break

    if "title" not in found:
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") and len(stripped.strip("# ")) > 3:
                found["title"] = stripped.strip("# ").strip()
                evidence["title"] = stripped[:300]
                break
    if "title" not in found:
        for line in lines:
            stripped = line.strip()
            if 5 <= len(stripped) <= 120 and stripped.isupper():
                found["title"] = stripped.title()
                evidence["title"] = stripped
                break

    missing = [field for field in FIELD_PATTERNS if field not in found and field != "problem_id"]
    return {
        "fields": found,
        "evidence": evidence,
        "missing": missing,
        "chars": len(normalized),
        "lines": len(lines),
    }


def split_problem_statements(text: str) -> list[dict[str, Any]]:
    """Split a combined document into statements when headings allow it.

    A document with a single statement returns one entry. The splitter only
    uses explicit markers such as ``Problem Statement 3`` or a Markdown ``##``
    heading; it never guesses boundaries from prose.
    """
    marker = re.compile(
        r"^\s*(?:#{1,3}\s*)?(?:problem\s*(?:statement|title)?\s*#?\s*(\d{1,3}))"
        r"\s*[:\-–.]?\s*(.*)$",
        re.IGNORECASE,
    )
    matches = list(marker.finditer(text))
    if len(matches) <= 1:
        return [{"index": 1, "title": "", "text": text}]
    chunks: list[dict[str, Any]] = []
    for position, match in enumerate(matches):
        start = match.start()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        chunks.append(
            {
                "index": int(match.group(1) or position + 1),
                "title": match.group(2).strip(),
                "text": body,
            }
        )
    return chunks


def assert_safe_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise SecurityError(
                        f"Archive entry escapes the extraction directory: {info.filename}"
                    )
    except zipfile.BadZipFile as exc:
        raise DocumentError("This .zip file is not a valid ZIP archive.") from exc
