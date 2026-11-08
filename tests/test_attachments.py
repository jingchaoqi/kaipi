from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from kaipi import agent, attachments, context, core, providers, tui
from kaipi.attachments import Attachment, Input
from kaipi.model import Message, Usage
from kaipi.providers import Reply, gemini, openai_compat, openai_responses
from kaipi.providers.anthropic import AnthropicProvider
from kaipi.store import Log

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/b8AAAAASUVORK5CYII="
)


def image() -> Attachment:
    return Attachment(name="截图.png", data=base64.b64encode(PNG).decode())


def test_parser_can_be_replaced_without_touching_context_or_existing_drafts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def custom(name: str, raw: bytes) -> tuple[list[dict[str, Any]], str]:
        calls.append((name, raw))
        return [
            {"type": "text", "text": f'{attachments.TAG}name="{name}">custom</kaipi:attachment>'}
        ], "custom parser"

    monkeypatch.setattr(attachments, "parse", custom)
    path = tmp_path / "custom.binary"
    path.write_bytes(b"\x00\xff")
    item = Attachment.read(path)
    draft = Input("检查 [file1]", [item])
    assert calls == [(path.name, b"\x00\xff")]
    assert item.describe() == {"name": path.name, "summary": "custom parser", "kind": "file"}

    def unavailable(*args: Any) -> Any:
        raise AssertionError("the agent must only consume the already prepared snapshot")

    monkeypatch.setattr(attachments, "parse", unavailable)
    monkeypatch.setattr(Attachment, "blocks", unavailable)
    first = context.input_message(draft)
    expected = json.loads(json.dumps(first))
    first["content"][1]["text"] = "changed outside the draft"
    assert context.input_message(draft) == expected
    path.unlink()
    assert context.input_message(draft) == expected


def test_plain_agent_import_does_not_load_file_decoders() -> None:
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from kaipi import context
context.input_message('hello')
decoders = ('PIL', 'pillow_heif', 'markitdown', 'openpyxl', 'onnxruntime')
assert not any(name in sys.modules for name in decoders)
""",
        ],
        check=True,
    )


def test_attachment_snapshot_is_frozen_and_history_survives_deleted_source(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text('print("before")\n')
    items = [Attachment.read(source), image()]
    question = Input("检查这个问题 [file1] [image1]", items)
    source.write_text('print("after")\n')
    session = core.Session(tmp_path)
    requests: list[list[Message]] = []

    class Inspect:
        model = "fake"

        def complete(self, system: str, messages: list[Message], points: list[int]) -> Reply:
            requests.append(json.loads(json.dumps(messages)))
            return Reply(
                content=[{"type": "text", "text": "done"}], stop_reason="end_turn", usage=Usage()
            )

        def count_tokens(self, text: str) -> int:
            return 1

    nid = agent.run_turn(
        session.log, Inspect(), None, [], question, exploration=False, cwd=tmp_path
    )
    source.unlink()
    replay = Log(session.log.path)
    node = replay.state.nodes[nid]
    assert context.user_input(node) == str(question)
    assert "before" in node.payload[0]["content"][1]["text"]
    assert "after" not in node.payload[0]["content"][1]["text"]
    again = context.build(replay.state, nid, [], "继续")
    assert again.messages[: len(node.payload)] == node.payload
    assert again.messages[0]["content"][2]["source"]["data"] == image().data
    assert requests[0][0]["content"] == node.payload[0]["content"]


def test_four_protocols_preserve_images_and_pdf() -> None:
    pdf = Attachment(name="spec.pdf", data=base64.b64encode(b"%PDF-1.4\nexample").decode())
    msg = context.input_message(Input("解释附件", [image(), pdf]))
    chat = openai_compat.to_openai("S", [msg])[1]["content"]
    assert [p["type"] for p in chat] == ["text", "image_url", "file"]
    assert chat[1]["image_url"]["url"].endswith(image().data)
    assert chat[2]["file"]["filename"] == "spec.pdf"
    responses = openai_responses.to_items([msg])
    assert [m["content"][0]["type"] for m in responses] == [
        "input_text",
        "input_image",
        "input_file",
    ]
    parts = gemini.to_contents([msg])[0]["parts"]
    assert parts[1]["inlineData"] == {"mimeType": "image/png", "data": image().data}
    assert parts[2]["inlineData"]["mimeType"] == "application/pdf"
    provider = AnthropicProvider("fake", api_key="test", compat=True)
    body = provider._request("S", [msg], [])
    assert body["messages"][0]["content"][1] == msg["content"][1]
    assert body["messages"][0]["content"][2]["source"]["data"] == pdf.data
    assert "cache_control" in body["messages"][0]["content"][-1]
    assert "cache_control" not in msg["content"][-1], "formatting cannot mutate the frozen payload"
    provider.client.close()


def test_file_paths_only_match_whole_paste_and_handle_spaces(tmp_path: Path) -> None:
    path = tmp_path / "中文 file.txt"
    path.write_text("hello")
    assert attachments.pasted_paths(str(path)) == [path]
    assert attachments.pasted_paths(f'"{path}"') == [path]
    assert attachments.pasted_paths(str(path).replace(" ", r"\ ")) == [path]
    assert attachments.pasted_paths(path.as_uri()) == [path]
    assert attachments.pasted_paths(f"请阅读 {path}") == []
    assert attachments.pasted_paths("file://other-host/tmp/file.txt") == []
    assert attachments.pasted_paths("hello /") == []


def test_size_format_and_count_validation(tmp_path: Path) -> None:
    bad = tmp_path / "binary.bin"
    bad.write_bytes(b"\x00\xff\x00")
    with pytest.raises(ValueError, match="无法解析"):
        Attachment.read(bad)
    text = tmp_path / "huge.txt"
    text.write_bytes(b"x" * (attachments.MAX_TEXT + 1))
    with pytest.raises(ValueError, match="128 KiB"):
        Attachment.read(text)
    with pytest.raises(ValueError, match="编码无效"):
        Attachment(name="bad.png", data="!not base64!").block()
    with pytest.raises(ValueError, match="8 个附件"):
        Input("question", [image()] * 9)
    with pytest.raises(ValueError, match="20 MiB"):
        Attachment(name="big", data="A" * (attachments.MAX_INPUT * 2)).block()


def test_text_attachment_redacts_configured_credentials(tmp_path: Path) -> None:
    providers.save_auth("deepseek", api_key="sk-user-secret-0123456789")
    path = tmp_path / "debug.log"
    path.write_text("key=sk-user-secret-0123456789")
    text = Attachment.read(path).block()["text"]
    assert "sk-user-secret-0123456789" not in text and "[kaipi: API key redacted]" in text


@pytest.mark.parametrize("paste", ["path", "clipboard", "delete", "text", "raw"])
def test_terminal_paste_keys_snapshot_and_remove_attachments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paste: str
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    path = tmp_path / "screen shot.png"
    path.write_bytes(PNG)
    monkeypatch.setattr(attachments, "clipboard", lambda: ([image()], ""))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        terminal = tui.Terminal(core.Session(tmp_path))
        if paste == "clipboard":
            pipe.send_text("看看这个 \x16\r")
        elif paste == "raw":
            pipe.send_text(f'"{path}"\r')
        elif paste == "text":
            pipe.send_text("问题 \x1b[200~普通文字 /\x1b[201~\r")
        else:
            pipe.send_text(
                f'看看这个 \x1b[200~"{path}"\x1b[201~'
                + ("\x7f" if paste == "delete" else "")
                + "\r"
            )
        value = terminal.prompt()
        if paste in ("path", "clipboard", "raw"):
            assert isinstance(value, Input) and len(value.attachments) == 1
            assert value.attachments[0].raw() == PNG
            assert "[image1]" in value
        else:
            assert not isinstance(value, Input)
            assert value == ("看看这个" if paste == "delete" else "问题 普通文字 /")
        assert not terminal.attachments


def test_attachment_drafts_remain_separate_across_submissions(tmp_path: Path) -> None:
    with create_app_session(output=DummyOutput()):
        terminal = tui.Terminal(core.Session(tmp_path))
        terminal.session.validate_while_typing = False
        terminal.session.default_buffer.auto_suggest = None
        terminal.add_attachments([image()])
        assert terminal.collect_input("") == ""
        assert terminal.attachments, "a model finishing must not clear the next draft"
        first = terminal.collect_input(terminal.session.default_buffer.text)
        terminal.session.default_buffer.reset()
        other = Attachment(name="other.txt", data=base64.b64encode(b"different").decode())
        terminal.add_attachments([other])
        second = terminal.collect_input(terminal.session.default_buffer.text)
        assert isinstance(first, Input) and first.attachments[0].raw() == PNG
        assert isinstance(second, Input) and second.attachments[0].raw() == b"different"


def test_multiple_clipboard_pastes_accumulate_while_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    monkeypatch.setenv("TERM", "xterm-256color")
    started, release = threading.Event(), threading.Event()
    document = Attachment(name="notes.txt", data=base64.b64encode(b"notes").decode())
    batches = iter([[image(), image()], [document, image()]])

    def clipboard() -> tuple[list[Attachment], str]:
        batch = next(batches)
        if not started.is_set():
            started.set()
            assert release.wait(3)
        return batch, ""

    monkeypatch.setattr(attachments, "clipboard", clipboard)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        terminal = tui.Terminal(core.Session(tmp_path))

        async def submit() -> str:
            async def keys() -> None:
                pipe.send_text("比较这些附件 \x16")
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert started.is_set()
                pipe.send_text("\x16\r")
                await asyncio.sleep(0.05)
                assert not terminal.session.app.is_done
                release.set()

            task = asyncio.create_task(keys())
            try:
                result = await asyncio.wait_for(terminal.session.prompt_async(), 5)
                await task
                return result
            finally:
                release.set()

        raw = asyncio.run(submit())
        assert terminal.attachments, terminal.notice
        value = terminal.collect_input(raw)
        assert value == "比较这些附件 [image1][image2][file1][image3]"
        assert isinstance(value, Input) and len(value.attachments) == 4
        assert value.attachments[2] == document
        terminal.session.validate_while_typing = False
        terminal.session.default_buffer.auto_suggest = None
        terminal.session.default_buffer.reset()
        terminal.add_attachments([image()])
        assert terminal.session.default_buffer.text == "[image1]", "new drafts restart numbering"


def test_deleting_an_attachment_does_not_renumber_remaining_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        terminal = tui.Terminal(core.Session(tmp_path))
        terminal.session.validate_while_typing = False
        terminal.session.default_buffer.auto_suggest = None
        terminal.add_attachments([image(), image().model_copy(update={"name": "second.png"})])
        # Move before image2, remove image1 as a whole, then paste another image.
        pipe.send_text("[image1][image2]" + "\x1b[D" * len("[image2]") + "\x7f")
        path = tmp_path / "more.png"
        path.write_bytes(PNG)
        pipe.send_text(f'\x1b[200~"{path}"\x1b[201~\r')
        value = terminal.prompt()
        assert value == "[image3][image2]"
        assert isinstance(value, Input) and len(value.attachments) == 2
        assert [item.name for item in value.attachments] == ["more.png", "second.png"]


def test_failed_file_batch_keeps_existing_draft(tmp_path: Path) -> None:
    good = tmp_path / "a.txt"
    good.write_text("ok")
    bad = tmp_path / "b.bin"
    bad.write_bytes(b"\xff\x00")
    with create_app_session(output=DummyOutput()):
        terminal = tui.Terminal(core.Session(tmp_path))
        terminal.session.validate_while_typing = False
        terminal.session.default_buffer.auto_suggest = None
        terminal.session.default_buffer.text = "original question"
        terminal.paste_text(f"{good} {bad}")
        assert terminal.session.default_buffer.text == "original question"
        assert not terminal.attachments and "无法解析" in terminal.notice


def test_word_and_excel_attachments_extract_contents(tmp_path: Path) -> None:
    from datetime import date
    from zipfile import ZipFile

    from openpyxl import Workbook

    word = tmp_path / "需求.docx"
    with ZipFile(word, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="r1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>登录接口需求</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>超时</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>3秒</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>""",
        )
    body = Attachment.read(word).block()["text"]
    assert "登录接口需求" in body and "超时" in body and "3秒" in body
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "统计"
    sheet.append(["日期", "费用", "说明"])
    sheet.append([date(2026, 11, 2), 12.5, "中文"])
    sheet.append([None, "=SUM(B2:B2)", True])
    book.create_sheet("第二张表").append(["hello"])
    excel = tmp_path / "统计.xlsx"
    book.save(excel)
    text = Attachment.read(excel).block()["text"]
    assert "[工作表: 统计]" in text and "[工作表: 第二张表]" in text
    assert "2026-11-02" in text and "12.5" in text and "中文" in text
    assert "=SUM(B2:B2)" in text, "formulas must be sent as text, never evaluated"
    assert "hello" in text


def test_document_detection_and_decompression_bounds(tmp_path: Path) -> None:
    from zipfile import ZIP_DEFLATED, ZipFile

    invalid = tmp_path / "broken.docx"
    invalid.write_bytes(b"broken document")
    assert "broken document" in Attachment.read(invalid).block()["text"], (
        "content wins over extension"
    )
    old = tmp_path / "legacy.xls"
    old.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1\0\xff")
    with pytest.raises(ValueError, match="无法解析"):
        Attachment.read(old)
    bomb = tmp_path / "compressed.docx"
    with ZipFile(bomb, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b" " * (21 * 1024 * 1024))
    with pytest.raises(ValueError, match="解压后过大"):
        Attachment.read(bomb)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_system_clipboard_reads_files_and_image_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    import shutil
    import subprocess
    import sys
    from types import SimpleNamespace

    path = tmp_path / "space name.txt"
    path.write_text("hello")
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/wl-paste" if name == "wl-paste" else None,
    )
    commands: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> SimpleNamespace:
        commands.append(args)
        if platform == "darwin":
            data = json.dumps({"paths": [str(path)]}).encode()
        elif "--list-types" in args:
            data = b"image/png\ntext/plain\n"
        else:
            data = PNG
        return SimpleNamespace(stdout=data)

    monkeypatch.setattr(subprocess, "run", run)
    items, text = attachments.clipboard()
    assert len(items) == 1 and not text
    assert items[0].raw() == (b"hello" if platform == "darwin" else PNG)
    assert commands[0][0] == ("osascript" if platform == "darwin" else "wl-paste")


def test_failed_clipboard_paste_keeps_question_and_does_not_auto_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setenv("TERM", "xterm-256color")

    def fail() -> tuple[list[Attachment], str]:
        raise ValueError("clipboard unavailable")

    monkeypatch.setattr(attachments, "clipboard", fail)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        terminal = tui.Terminal(core.Session(tmp_path))

        async def submit() -> str:
            async def keys() -> None:
                pipe.send_text("original question\x16\r")
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if terminal.notice == "clipboard unavailable":
                        break
                assert terminal.notice == "clipboard unavailable"
                assert not terminal.session.app.is_done
                assert terminal.session.default_buffer.text == "original question"
                pipe.send_text("\r")

            task = asyncio.create_task(keys())
            result = await asyncio.wait_for(terminal.session.prompt_async(), 3)
            await task
            return result

        assert asyncio.run(submit()) == "original question"
        assert not terminal.attachments


@pytest.mark.parametrize("fmt", ["HEIF", "AVIF", "TIFF", "BMP"])
@pytest.mark.parametrize("name", ["photo.heic", "wrong-extension.txt", "no-extension"])
def test_image_content_detection_normalizes_all_supported_decoders(fmt: str, name: str) -> None:
    from io import BytesIO

    from PIL import Image
    from pillow_heif import register_heif_opener

    register_heif_opener(thumbnails=False)
    original = Image.new("RGB", (40, 24), "#487a9c")
    stream = BytesIO()
    original.save(stream, format=fmt)
    item = Attachment(name=name, data=base64.b64encode(stream.getvalue()).decode())
    blocks = item.blocks()
    source = blocks[0]["source"]
    assert source["media_type"] in ("image/png", "image/jpeg")
    with Image.open(BytesIO(base64.b64decode(source["data"]))) as normalized:
        assert normalized.size == (40, 24)
    assert fmt in item.summary and "→" in item.summary
    assert any(b["type"] == "text" and name in b["text"] for b in blocks), (
        "conversion metadata is frozen too"
    )


def test_orientation_transparency_and_size_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    from io import BytesIO

    from PIL import Image

    from kaipi.attachments import images

    source = Image.new("RGB", (40, 24), "red")
    exif = Image.Exif()
    exif[274] = 6
    stream = BytesIO()
    source.save(stream, format="JPEG", exif=exif)
    item = Attachment(name="phone.jpg", data=base64.b64encode(stream.getvalue()).decode())
    with Image.open(BytesIO(base64.b64decode(item.block()["source"]["data"]))) as fixed:
        assert fixed.size == (24, 40)
        assert fixed.getexif().get(274, 1) == 1
    rgba = Image.new("RGBA", (32, 24), (10, 20, 30, 64))
    stream = BytesIO()
    rgba.save(stream, format="TIFF")
    monkeypatch.setattr(images, "MAX_EDGE", 16)
    item = Attachment(name="transparent", data=base64.b64encode(stream.getvalue()).decode())
    with Image.open(BytesIO(base64.b64decode(item.block()["source"]["data"]))) as fixed:
        assert fixed.size == (16, 12) and fixed.mode == "RGBA"
        pixel = fixed.getpixel((0, 0))
        assert isinstance(pixel, tuple) and pixel[3] == 64
    assert "缩放" in item.summary


def test_multiple_frames_are_preserved_in_order_and_replay(tmp_path: Path) -> None:
    from io import BytesIO

    from PIL import Image

    frames = [Image.new("RGB", (16, 12), color) for color in ["red", "green", "blue"]]
    stream = BytesIO()
    frames[0].save(stream, format="TIFF", save_all=True, append_images=frames[1:])
    item = Attachment(name="pages.tiff", data=base64.b64encode(stream.getvalue()).decode())
    message = context.input_message(Input("比较这些页", [item]))
    images = [b for b in message["content"] if b["type"] == "image"]
    assert len(images) == 3 and "3 帧全部保留" in item.summary
    pixels = []
    for block in images:
        with Image.open(BytesIO(base64.b64decode(block["source"]["data"]))) as im:
            pixels.append(im.convert("RGB").getpixel((0, 0)))
    assert pixels == [(255, 0, 0), (0, 128, 0), (0, 0, 255)]
    assert (
        len(
            [
                m
                for m in openai_responses.to_items([message])
                if m["content"][0]["type"] == "input_image"
            ]
        )
        == 3
    )
    assert len([p for p in gemini.to_contents([message])[0]["parts"] if "inlineData" in p]) == 3
    many = BytesIO()
    frames[0].save(many, format="TIFF", save_all=True, append_images=frames * 3)
    with pytest.raises(ValueError, match="最多支持"):
        Attachment(name="many.tiff", data=base64.b64encode(many.getvalue()).decode()).blocks()


def test_renamed_word_spreadsheet_and_powerpoint_documents(tmp_path: Path) -> None:

    from pptx import Presentation

    # Reuse a valid OOXML document produced by the existing Word extraction test.
    test_word_and_excel_attachments_extract_contents(tmp_path)
    for name, expected in [("需求.docx", "登录接口需求"), ("统计.xlsx", "12.5")]:
        raw = (tmp_path / name).read_bytes()
        text = Attachment(name="renamed.data", data=base64.b64encode(raw).decode()).block()["text"]
        assert expected in text
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "设计方案"
    slide.placeholders[1].text = "通用附件转换"
    path = tmp_path / "deck.pptx"
    deck.save(str(path))
    text = Attachment(name="slides.bin", data=base64.b64encode(path.read_bytes()).decode()).block()[
        "text"
    ]
    assert "设计方案" in text and "通用附件转换" in text


def test_heic_decoder_orientation_is_not_applied_twice() -> None:
    from io import BytesIO

    from PIL import Image
    from pillow_heif import register_heif_opener

    register_heif_opener(thumbnails=False)
    photo = Image.new("RGB", (40, 24), "red")
    exif = Image.Exif()
    exif[274] = 6
    data = BytesIO()
    photo.save(data, format="HEIF", exif=exif)
    raw = data.getvalue()
    # The HEIF decoder applies container transforms and resets EXIF orientation.
    with Image.open(BytesIO(raw)) as expected:
        expected_size = expected.size
        expected_pixels = expected.convert("RGB").tobytes()
    item = Attachment(name="phone.heic", data=base64.b64encode(raw).decode())
    with Image.open(BytesIO(base64.b64decode(item.block()["source"]["data"]))) as result:
        assert result.size == expected_size
        assert result.convert("RGB").tobytes() == expected_pixels


def test_animation_durations_and_16_bit_images_are_preserved() -> None:
    from io import BytesIO

    from PIL import Image

    frames = [Image.new("RGB", (16, 12), c) for c in ["red", "green", "blue"]]
    data = BytesIO()
    frames[0].save(
        data, format="GIF", save_all=True, append_images=frames[1:], duration=[100, 200, 300]
    )
    animation = Attachment(name="animation.gif", data=base64.b64encode(data.getvalue()).decode())
    assert len([b for b in animation.blocks() if b["type"] == "image"]) == 3
    assert "100,200,300" in animation.summary
    grey = Image.new("I;16", (3, 1))
    grey.putdata([0, 32768, 65535])
    data = BytesIO()
    grey.save(data, format="TIFF")
    image16 = Attachment(name="sensor.tif", data=base64.b64encode(data.getvalue()).decode())
    with Image.open(BytesIO(base64.b64decode(image16.block()["source"]["data"]))) as result:
        assert [result.getpixel((i, 0)) for i in range(3)] == [
            (0, 0, 0),
            (127, 127, 127),
            (255, 255, 255),
        ]
