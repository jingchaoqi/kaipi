"""Default local document converters, private to the attachment parser."""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from typing import TYPE_CHECKING
from zipfile import ZipFile, is_zipfile

from kaipi.i18n import tr

if TYPE_CHECKING:
    from markitdown import MarkItDown
from openpyxl import load_workbook


@lru_cache(maxsize=1)
def converter() -> MarkItDown:
    import onnxruntime  # type: ignore[import-untyped]
    from markitdown import MarkItDown
    from markitdown.converters import (
        CsvConverter,
        DocxConverter,
        EpubConverter,
        HtmlConverter,
        IpynbConverter,
        PlainTextConverter,
        PptxConverter,
        XlsConverter,
    )

    onnxruntime.disable_telemetry_events()
    # Restrict to local document converters: no cloud clients, plugins, URLs,
    # recursive ZIP extraction, audio transcription, or image-to-text fallback.
    result = MarkItDown(enable_builtins=False, enable_plugins=False)
    for cls in (
        PlainTextConverter,
        HtmlConverter,
        CsvConverter,
        IpynbConverter,
        EpubConverter,
        DocxConverter,
        XlsConverter,
        PptxConverter,
    ):
        result.register_converter(cls())
    return result


def spreadsheet(raw: bytes) -> str:
    """Keep formula source too; generic spreadsheet conversion loses uncached formulas."""
    book = load_workbook(BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    try:
        lines = []
        cells = 0
        for sheet in book:
            if (sheet.max_row or 0) * (sheet.max_column or 0) > 50000:
                raise ValueError(tr("Excel 工作表过大；请选取需要的数据或导出 CSV"))
            lines.append(f"[工作表: {sheet.title}]")
            for index, values in enumerate(sheet.iter_rows(values_only=True), 1):
                cells += len(values)
                if cells > 50000 or index > 10000:
                    raise ValueError(tr("Excel 数据过多；请选取需要的数据或导出 CSV"))
                if any(value is not None for value in values):
                    lines.append("\t".join("" if value is None else str(value) for value in values))
        return "\n".join(lines)
    finally:
        book.close()


def text(name: str, raw: bytes) -> str:
    try:
        if is_zipfile(BytesIO(raw)):
            with ZipFile(BytesIO(raw)) as archive:
                if sum(i.file_size for i in archive.infolist()) > 20 * 1024 * 1024:
                    raise ValueError(tr("文档解压后过大（最多 20 MiB）"))
                # Inspect container contents, not the supplied filename.
                if "xl/workbook.xml" in archive.namelist():
                    return spreadsheet(raw)
        # Exact UTF-8 text, including code, must not be reformatted as Markdown.
        # Non-UTF8 documents use the converter's content/charset detection.
        try:
            plain = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            plain = None
        if plain is not None and "\0" not in plain:
            return plain
        from markitdown import StreamInfo

        result = converter().convert_stream(BytesIO(raw), stream_info=StreamInfo(filename=name))
        return result.text_content
    except ValueError:
        raise
    except Exception as e:
        # Converter failures are user file errors; retain the draft and avoid a traceback.
        raise ValueError(
            tr("无法解析附件：{name}；文件可能损坏、加密或缺少对应的解码能力", name=name)
        ) from e
