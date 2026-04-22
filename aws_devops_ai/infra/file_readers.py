"""File readers for various log formats — CSV, RPT, XEL, DOCX, and plain text."""

from __future__ import annotations

import csv
import io
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)

# Supported file extensions (lowercase, with dot)
SUPPORTED_EXTENSIONS = {".log", ".csv", ".rpt", ".xel", ".docx", ".pdf", ".txt", ".json", ".xml"}


def read_file_content(path: Path) -> str:
    """Read any supported file format and return its content as text.

    Dispatches to the appropriate reader based on file extension.
    Returns empty string if the file cannot be read.
    """
    ext = path.suffix.lower()
    try:
        if ext == ".csv":
            return _read_csv(path)
        elif ext == ".rpt":
            return _read_rpt(path)
        elif ext == ".xel":
            return _read_xel(path)
        elif ext == ".docx":
            return _read_docx(path)
        elif ext == ".pdf":
            return _read_pdf_ocr(path)
        else:
            # Plain text fallback (.log, .txt, .json, .xml, etc.)
            return path.read_text(errors="replace")
    except Exception as e:
        logger.warning("Failed to read %s: %s", path, e)
        return ""


def is_supported_file(path: Path) -> bool:
    """Check if a file has a supported extension."""
    return path.suffix.lower() in SUPPORTED_EXTENSIONS



def _read_csv(path: Path) -> str:
    """Read a CSV file and return a formatted text representation."""
    try:
        raw = path.read_text(errors="replace")
    except UnicodeDecodeError:
        raw = path.read_bytes().decode("utf-8", errors="replace")

    reader = csv.reader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        return ""

    # Format: header line + data rows for readability
    lines = []
    header = rows[0] if rows else []
    lines.append(" | ".join(header))
    lines.append("-" * len(lines[0]))
    for row in rows[1:]:
        if header:
            # Key=Value format for easier AI parsing
            pairs = [f"{header[i]}={row[i]}" if i < len(header) else row[i] for i in range(len(row))]
            lines.append(" | ".join(pairs))
        else:
            lines.append(" | ".join(row))
    return "\n".join(lines)


def _read_rpt(path: Path) -> str:
    """Read an RPT (report/text) file as plain text."""
    try:
        return path.read_text(errors="replace")
    except Exception:
        return path.read_bytes().decode("utf-8", errors="replace")


def _read_xel(path: Path) -> str:
    """Read a SQL Server Extended Events (.xel) file.

    XEL files are binary. We attempt two strategies:
    1. Parse as XML if the file contains XML event data
    2. Extract readable strings from the binary content
    """
    data = path.read_bytes()

    # Strategy 1: Try to find XML fragments in the binary data
    text_content = _extract_xel_xml_events(data)
    if text_content:
        return text_content

    # Strategy 2: Extract printable strings (like the `strings` command)
    return _extract_printable_strings(data)


def _extract_xel_xml_events(data: bytes) -> str:
    """Try to extract XML event fragments from XEL binary data."""
    events = []
    # XEL files often contain XML event nodes embedded in binary
    # Look for <event> tags or other XML-like structures
    text = data.decode("utf-8", errors="replace")

    # Try to find XML event blocks
    import re
    # Match <event ...>...</event> blocks
    event_pattern = re.compile(r"<event\b[^>]*>.*?</event>", re.DOTALL)
    matches = event_pattern.findall(text)
    if matches:
        for i, match in enumerate(matches):
            try:
                root = ET.fromstring(match)
                events.append(_format_xml_element(root, i + 1))
            except ET.ParseError:
                events.append(f"[Event {i + 1}] {match[:500]}")
        return "\n\n".join(events)

    # Try parsing entire content as XML
    try:
        root = ET.fromstring(text.strip())
        return _format_xml_tree(root)
    except ET.ParseError:
        pass

    return ""


def _format_xml_element(elem: ET.Element, index: int) -> str:
    """Format a single XML event element into readable text."""
    lines = [f"[Event {index}] {elem.tag} {dict(elem.attrib)}"]
    for child in elem:
        if child.text and child.text.strip():
            lines.append(f"  {child.tag}: {child.text.strip()}")
        if child.attrib:
            lines.append(f"  {child.tag} attrs: {dict(child.attrib)}")
        for sub in child:
            if sub.text and sub.text.strip():
                lines.append(f"    {sub.tag}: {sub.text.strip()}")
    return "\n".join(lines)


def _format_xml_tree(root: ET.Element) -> str:
    """Format an XML tree into readable text."""
    lines = []

    def _walk(elem, depth=0):
        indent = "  " * depth
        attrs = f" {dict(elem.attrib)}" if elem.attrib else ""
        text = f": {elem.text.strip()}" if elem.text and elem.text.strip() else ""
        lines.append(f"{indent}{elem.tag}{attrs}{text}")
        for child in elem:
            _walk(child, depth + 1)

    _walk(root)
    return "\n".join(lines)


def _extract_printable_strings(data: bytes, min_length: int = 8) -> str:
    """Extract printable ASCII/UTF-8 strings from binary data.

    Similar to the Unix `strings` command — pulls out sequences of
    printable characters that are at least min_length long.
    """
    strings = []
    current = []

    for byte in data:
        if 32 <= byte < 127 or byte in (9, 10, 13):  # printable ASCII + whitespace
            current.append(chr(byte))
        else:
            if len(current) >= min_length:
                strings.append("".join(current))
            current = []

    if len(current) >= min_length:
        strings.append("".join(current))

    return "\n".join(strings)


def _read_docx(path: Path) -> str:
    """Read a DOCX file by converting to PDF pages then extracting text.

    Pipeline: DOCX → page images (pymupdf) → AWS Textract / Bedrock vision / tesseract OCR.
    Falls back to raw XML extraction from the DOCX ZIP if pymupdf is not available.
    """
    try:
        images = _render_pages_to_images(path)
        if images:
            return _ocr_images(images, source_name=path.name)
    except ImportError:
        pass  # pymupdf not installed — fall through silently to XML extraction
    except Exception as e:
        logger.debug("Page rendering failed for %s: %s, trying fallback", path, e)
    return _read_docx_fallback(path)


def _read_docx_fallback(path: Path) -> str:
    """Extract text from DOCX by reading the raw XML inside the ZIP archive."""
    import zipfile

    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "word/document.xml" not in zf.namelist():
                return ""
            xml_content = zf.read("word/document.xml")
            root = ET.fromstring(xml_content)

            texts = []
            for elem in root.iter():
                if elem.text and elem.text.strip():
                    texts.append(elem.text.strip())
                if elem.tail and elem.tail.strip():
                    texts.append(elem.tail.strip())
            return "\n".join(texts)
    except (zipfile.BadZipFile, ET.ParseError) as e:
        logger.warning("Failed to read DOCX fallback for %s: %s", path, e)
        return ""


def _read_pdf_ocr(path: Path) -> str:
    """Read a PDF file by rendering pages to images then extracting text.

    Pipeline: PDF → page images (pymupdf) → AWS Textract / Bedrock vision / tesseract OCR.
    """
    try:
        images = _render_pages_to_images(path)
        if images:
            return _ocr_images(images, source_name=path.name)
    except ImportError as e:
        logger.warning("pymupdf/Pillow not installed, cannot process PDF %s: %s", path, e)
    except Exception as e:
        logger.warning("Failed to process PDF %s: %s", path, e)
    return ""


# ---------------------------------------------------------------------------
# Shared rendering: DOCX/PDF → page images via pymupdf
# ---------------------------------------------------------------------------

def _render_pages_to_images(path: Path) -> list[bytes]:
    """Render each page of a DOCX or PDF to PNG bytes using pymupdf."""
    import pymupdf

    doc = pymupdf.open(str(path))
    png_pages: list[bytes] = []
    try:
        for page_num in range(len(doc)):
            page = doc[page_num]
            # 300 DPI ≈ 4x scale
            mat = pymupdf.Matrix(4, 4)
            pix = page.get_pixmap(matrix=mat)
            png_pages.append(pix.tobytes("png"))
    finally:
        doc.close()
    return png_pages


# ---------------------------------------------------------------------------
# OCR strategy chain: Textract → Bedrock vision → local tesseract
# ---------------------------------------------------------------------------

def _ocr_images(png_pages: list[bytes], source_name: str = "") -> str:
    """Extract text from page images using the best available OCR method.

    Tries in order:
    1. AWS Textract (document OCR service)
    2. AWS Bedrock multimodal LLM (Claude vision)
    3. Local tesseract (pytesseract)
    """
    # Strategy 1: AWS Textract
    try:
        result = _ocr_via_textract(png_pages)
        if result.strip():
            logger.info("OCR via Textract succeeded for %s", source_name)
            return result
    except Exception as e:
        logger.info("Textract unavailable or failed for %s: %s", source_name, e)

    # Strategy 2: AWS Bedrock multimodal (Claude vision)
    try:
        result = _ocr_via_bedrock_vision(png_pages)
        if result.strip():
            logger.info("OCR via Bedrock vision succeeded for %s", source_name)
            return result
    except Exception as e:
        logger.info("Bedrock vision unavailable or failed for %s: %s", source_name, e)

    # Strategy 3: Local tesseract
    try:
        result = _ocr_via_tesseract(png_pages)
        if result.strip():
            logger.info("OCR via local tesseract succeeded for %s", source_name)
            return result
    except Exception as e:
        logger.info("Local tesseract unavailable or failed for %s: %s", source_name, e)

    logger.warning("All OCR strategies failed for %s", source_name)
    return ""


def _ocr_via_textract(png_pages: list[bytes]) -> str:
    """Use AWS Textract DetectDocumentText to OCR each page image."""
    import boto3

    client = boto3.client("textract")
    all_text: list[str] = []

    for i, png_data in enumerate(png_pages):
        response = client.detect_document_text(
            Document={"Bytes": png_data}
        )
        lines = []
        for block in response.get("Blocks", []):
            if block["BlockType"] == "LINE":
                lines.append(block["Text"])
        if lines:
            all_text.append(f"[Page {i + 1}]\n" + "\n".join(lines))

    return "\n\n".join(all_text)


def _ocr_via_bedrock_vision(png_pages: list[bytes]) -> str:
    """Use AWS Bedrock Claude with vision to extract text from page images."""
    import base64
    import json
    import boto3

    client = boto3.client("bedrock-runtime")
    all_text: list[str] = []

    for i, png_data in enumerate(png_pages):
        b64_image = base64.b64encode(png_data).decode("utf-8")

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 8192,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64_image,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Extract ALL text from this document image exactly as it appears. "
                                "Preserve the original layout, tables, and formatting as much as possible. "
                                "Return ONLY the extracted text, nothing else."
                            ),
                        },
                    ],
                }
            ],
        })

        response = client.invoke_model(
            modelId="anthropic.claude-sonnet-4-20250514",
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        result = json.loads(response["body"].read())
        page_text = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                page_text += block["text"]

        if page_text.strip():
            all_text.append(f"[Page {i + 1}]\n{page_text.strip()}")

    return "\n\n".join(all_text)


def _ocr_via_tesseract(png_pages: list[bytes]) -> str:
    """Use local tesseract (via pytesseract + Pillow) to OCR page images."""
    import pytesseract
    from PIL import Image

    all_text: list[str] = []
    for i, png_data in enumerate(png_pages):
        img = Image.open(io.BytesIO(png_data))
        page_text = pytesseract.image_to_string(img)
        if page_text.strip():
            all_text.append(f"[Page {i + 1}]\n{page_text.strip()}")

    return "\n\n".join(all_text)
