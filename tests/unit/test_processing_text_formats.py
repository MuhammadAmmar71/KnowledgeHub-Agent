import base64
import urllib.parse
import zlib

import pytest

from app.processing.drawio import DrawioProcessor
from app.processing.html import HtmlProcessor
from app.processing.json_processor import JsonProcessor
from app.processing.markdown import MarkdownProcessor
from app.processing.python import PythonProcessor
from app.processing.svg import SvgProcessor
from app.processing.text import TextProcessor
from app.sharepoint.files import DownloadedFile


def _file(name: str, content: bytes) -> DownloadedFile:
    return DownloadedFile(item_id="1", filename=name, content=content, content_type=None)


@pytest.mark.asyncio
async def test_markdown_passthrough_preserves_unicode():
    f = _file("t.md", "# Título\n\nSome émoji 🎉".encode("utf-8"))
    result = await MarkdownProcessor().extract(f)
    assert "Título" in result.text
    assert "🎉" in result.text


@pytest.mark.asyncio
async def test_python_preserves_code_exactly():
    code = "def foo():\n    return 1\n"
    f = _file("t.py", code.encode("utf-8"))
    result = await PythonProcessor().extract(f)
    assert result.text == code


@pytest.mark.asyncio
async def test_json_valid_is_pretty_printed():
    f = _file("t.json", b'{"a":1,"nested":{"b":[1,2,3]}}')
    result = await JsonProcessor().extract(f)
    assert '"a": 1' in result.text
    assert not result.warnings


@pytest.mark.asyncio
async def test_json_invalid_stored_raw_with_warning():
    f = _file("bad.json", b"{not valid json")
    result = await JsonProcessor().extract(f)
    assert result.text == "{not valid json"
    assert result.warnings


@pytest.mark.asyncio
async def test_html_strips_scripts_and_styles_preserves_headings_and_links():
    html = b"""
    <html><head><style>.x{color:red}</style></head>
    <body>
    <h1>Title Here</h1>
    <script>alert(1)</script>
    <p>Hello <b>world</b>, visit <a href="https://example.com">our site</a>.</p>
    <nav>Home | About</nav>
    </body></html>
    """
    f = _file("t.html", html)
    result = await HtmlProcessor().extract(f)
    assert "alert(1)" not in result.text
    assert "color:red" not in result.text
    assert "Home | About" not in result.text  # nav is boilerplate, spec section 30
    assert "Title Here" in result.text
    assert "Hello" in result.text and "world" in result.text
    assert "https://example.com" in result.text


@pytest.mark.asyncio
async def test_drawio_uncompressed_extracts_node_labels():
    xml = b"""<mxfile><diagram name="Page-1"><mxGraphModel><root>
    <mxCell id="2" value="Start&lt;br&gt;Here" vertex="1"/>
    <mxCell id="3" value="End" vertex="1"/>
    </root></mxGraphModel></diagram></mxfile>"""
    f = _file("t.drawio", xml)
    result = await DrawioProcessor().extract(f)
    assert "PAGE: Page-1" in result.text
    assert "End" in result.text
    assert not result.warnings


@pytest.mark.asyncio
async def test_drawio_compressed_format_is_decoded():
    """
    Draw.io's app-saved format: base64(raw-deflate(urlencode(xml))). This
    is the format most real .drawio files use, not the plain-XML form.
    """
    inner_xml = '<mxGraphModel><root><mxCell id="2" value="Compressed Node" vertex="1"/></root></mxGraphModel>'
    url_encoded = urllib.parse.quote(inner_xml, safe="")
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    compressed = compressor.compress(url_encoded.encode("utf-8")) + compressor.flush()
    b64 = base64.b64encode(compressed).decode("ascii")

    xml = f'<mxfile><diagram name="Compressed-Page">{b64}</diagram></mxfile>'.encode("utf-8")
    f = _file("c.drawio", xml)
    result = await DrawioProcessor().extract(f)

    assert "Compressed Node" in result.text
    assert "PAGE: Compressed-Page" in result.text
    assert not result.warnings


@pytest.mark.asyncio
async def test_drawio_malformed_xml_reports_warning_not_crash():
    f = _file("broken.drawio", b"<mxfile><diagram>not closed")
    result = await DrawioProcessor().extract(f)
    assert result.warnings


@pytest.mark.asyncio
async def test_txt_passthrough_strips_bom_and_preserves_unicode():
    f = _file("t.txt", "\ufeffAgent instructions: café ✓\nLine two".encode("utf-8"))
    result = await TextProcessor().extract(f)
    assert result.text == "Agent instructions: café ✓\nLine two"


@pytest.mark.asyncio
async def test_svg_extracts_title_desc_and_text_labels_not_markup():
    svg = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
      <title>Branch Strategy</title>
      <desc>How feature branches merge into main</desc>
      <rect x="0" y="0" width="10" height="10" style="fill:red"/>
      <path d="M0 0 L10 10"/>
      <text x="5" y="5">main</text>
      <text x="5" y="20"><tspan>feature/</tspan><tspan>login</tspan></text>
    </svg>"""
    result = await SvgProcessor().extract(_file("b.svg", svg))
    assert result.text.splitlines() == [
        "Branch Strategy",
        "How feature branches merge into main",
        "main",
        "feature/login",
    ]
    assert result.metadata == {"title": "Branch Strategy"}
    assert "path" not in result.text and "fill" not in result.text
    assert not result.warnings


@pytest.mark.asyncio
async def test_svg_without_text_warns_but_does_not_fail():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>'
    result = await SvgProcessor().extract(_file("shapes.svg", svg))
    assert result.text == ""
    assert result.warnings


@pytest.mark.asyncio
async def test_svg_malformed_xml_reports_warning_not_crash():
    result = await SvgProcessor().extract(_file("broken.svg", b"<svg><text>unclosed"))
    assert result.text == ""
    assert result.warnings
