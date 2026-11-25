"""Check the built deck against its Markdown source: layouts, title slide, notes and text that fits its box.

Usage: check_deck.py SLIDES.md REFERENCE.pptx DECK.pptx

The fit is an estimate from conservative average glyph widths: it needs no fonts and no renderer.
"""

import json
import posixpath
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
EMU_PER_POINT = 12700
GLYPH_WIDTH = {"Libre Baskerville": 0.58, "Bricolage Grotesque": 0.54, "Courier New": 0.6}
LINE_HEIGHT = {"Libre Baskerville": 1.25, "Bricolage Grotesque": 1.25, "Courier New": 1.14}


@dataclass(frozen=True)
class Expected:
    layout: str
    title: str
    notes: bool
    footer: str = ""
    code: str = ""


def plain(node: object) -> str:
    """The text of a pandoc AST node."""
    if isinstance(node, list):
        return "".join(plain(child) for child in node)
    if not isinstance(node, dict):
        return ""
    if node.get("t") in ("Space", "SoftBreak", "LineBreak"):
        return " "
    if node.get("t") == "Str":
        return str(node["c"])
    if node.get("t") == "Code":
        return str(node["c"][1])
    return plain(node.get("c"))


def code_blocks(node: object) -> list[str]:
    """The code blocks below `node`."""
    if isinstance(node, list):
        return [found for child in node for found in code_blocks(child)]
    if not isinstance(node, dict):
        return []
    if node.get("t") == "CodeBlock":
        return [str(node["c"][1])]
    return code_blocks(node.get("c"))


def divs(node: object, name: str) -> list[dict[str, Any]]:
    """The Divs of class `name` below `node`."""
    if isinstance(node, list):
        return [found for child in node for found in divs(child, name)]
    if not isinstance(node, dict):
        return []
    if node.get("t") == "Div" and name in node["c"][0][1]:
        return [node]
    return divs(node.get("c"), name)


def expected_slides(source: Path) -> tuple[list[Expected], dict[str, str]]:
    """The slides the source asks for (a level-1 heading is a section, a level-2 one a slide) and its metadata."""
    run = subprocess.run(["pandoc", "-t", "json", str(source)], check=True, capture_output=True, text=True)
    document = json.loads(run.stdout)
    meta = {key: plain(value) for key, value in document["meta"].items()}
    groups: list[tuple[int, str, list[object]]] = []
    for block in document["blocks"]:
        if block["t"] == "Header" and block["c"][0] <= 2:
            groups.append((block["c"][0], plain(block["c"][2]), []))
        elif groups:
            groups[-1][2].append(block)
    slides = [Expected("Title Slide", meta["title"], False)]
    for level, title, body in groups:
        columns = divs(body, "columns")
        layout = "Section Header" if level == 1 else "Two Content" if columns else "Title and Content"
        footer = plain([div["c"][1] for div in divs(body, "footer")])
        slides.append(Expected(layout, title, bool(divs(body, "notes")), footer, "\n".join(code_blocks(body))))
    return slides, meta


class Deck:
    def __init__(self, path: Path) -> None:
        self.zip = zipfile.ZipFile(path)

    def xml(self, part: str) -> ET.Element:
        return ET.fromstring(self.zip.read(part))

    def related(self, part: str, kind: str) -> str | None:
        """The part `part` refers to with a relationship of type `kind`."""
        folder, name = posixpath.split(part)
        for relation in self.xml(f"{folder}/_rels/{name}.rels"):
            if relation.get("Type", "").endswith("/" + kind):
                return posixpath.normpath(posixpath.join(folder, relation.get("Target", "")))
        return None

    def slides(self) -> list[str]:
        """The slides, in the order of the presentation."""
        targets = {relation.get("Id"): relation.get("Target", "") for relation in self.xml("ppt/_rels/presentation.xml.rels")}
        ids = self.xml("ppt/presentation.xml").iter(f"{P}sldId")
        return [posixpath.join("ppt", targets[slide.get(f"{R}id")]) for slide in ids]

    def layouts(self) -> dict[str, bytes]:
        """The shape tree of each layout, by the layout's name."""
        found = {}
        for part in self.zip.namelist():
            if re.fullmatch(r"ppt/slideLayouts/[^/]+\.xml", part):
                content = self.xml(part).find(f"{P}cSld")
                assert content is not None
                found[content.get("name", "")] = ET.canonicalize(ET.tostring(content)).encode()
        return found


def placeholders(root: ET.Element) -> dict[tuple[str, str], ET.Element]:
    """The placeholder shapes, by type and index, as pandoc writes both."""
    found = {}
    for shape in root.iter(f"{P}sp"):
        holder = shape.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
        if holder is not None:
            found[holder.get("type", "obj"), holder.get("idx", "0")] = shape
    return found


def text(element: ET.Element) -> str:
    return "".join(run.text or "" for run in element.iter(f"{A}t"))


def wrapped_lines(words: str, columns: int) -> int:
    """The lines of a greedy wrap; a word longer than a line is an overflow, reported by the caller."""
    lines, used = 1, 0
    for word in words.split():
        needed = len(word) + (1 if used else 0)
        if used + needed > columns:
            lines, used = lines + 1, len(word)
        else:
            used += needed
    return lines


def lines(paragraph: ET.Element) -> list[str]:
    """The lines of a paragraph, as its breaks divide it; the spaces that keep code's indentation are plain spaces."""
    found = [""]
    for element in paragraph.iter():
        if element.tag == f"{A}br":
            found.append("")
        elif element.tag == f"{A}t":
            found[-1] += (element.text or "").replace("\N{NO-BREAK SPACE}", " ")
    return found


def overflow(shape: ET.Element, layout_shape: ET.Element) -> str | None:
    """Why the text of `shape` does not fit the box its layout gives it, if it does not."""
    box = layout_shape.find(f"{P}spPr/{A}xfrm/{A}ext")
    level = layout_shape.find(f"{P}txBody/{A}lstStyle/{A}lvl1pPr")
    style = None if level is None else level.find(f"{A}defRPr")
    if box is None or level is None or style is None:
        return "its layout has no box or no text style"
    inherited = style.find(f"{A}latin")
    height = int(box.get("cy", "0")) / EMU_PER_POINT - 7.2
    spacing = level.find(f"{A}spcBef/{A}spcPts")
    before = int(spacing.get("val", "0")) / 100 if spacing is not None else 0
    used = 0.0
    for paragraph in shape.iter(f"{A}p"):
        run = paragraph.find(f"{A}r/{A}rPr")
        own = None if run is None else run.find(f"{A}latin")
        code = run is not None and own is not None and own.get("typeface") == "Courier New"
        size = int((run.get("sz") if run is not None else None) or style.get("sz", "0")) / 100
        typeface = "Courier New" if code else inherited.get("typeface", "") if inherited is not None else ""
        properties = paragraph.find(f"{A}pPr")
        left = (properties.get("marL") if properties is not None else None) or level.get("marL", "0")
        columns = int((int(box.get("cx", "0")) - 2 * 91425 - int(left)) / EMU_PER_POINT / (size * GLYPH_WIDTH[typeface]))
        count = 0
        for line in lines(paragraph):
            longest = line if code else max(line.split(), key=len, default="")
            if len(longest) > columns:
                return f"{longest!r} is longer than a line of {columns} characters"
            count += 1 if code else wrapped_lines(line, columns)
        tallest = size
        for line_break in paragraph.iter(f"{A}br"):  # a break is as tall as its own size, or as the style's
            own_size = line_break.find(f"{A}rPr")
            tallest = max(tallest, int((own_size.get("sz") if own_size is not None else None) or style.get("sz", "0")) / 100)
        used += count * tallest * LINE_HEIGHT[typeface] + before
    return f"about {used:.0f} pt of text in a box of {height:.0f} pt" if used > height else None


def footer_problem(slide: ET.Element, wanted: str) -> str | None:
    """Why the footer of a slide is not the line the source asks for, if it is not."""
    shapes = [s for s in slide.iter(f"{P}sp") if s.find(f"{P}nvSpPr/{P}nvPr/{P}ph") is None]
    if [text(shape) for shape in shapes] != ([wanted] if wanted else []):
        return f"its footer is {[text(shape) for shape in shapes]}"
    if not wanted:
        return None
    box, run = shapes[0].find(f"{P}spPr/{A}xfrm/{A}ext"), next(shapes[0].iter(f"{A}rPr"))
    font = run.find(f"{A}latin")
    if box is None or font is None:
        return "its footer has no box or no font"
    glyph = int(run.get("sz", "0")) / 100 * GLYPH_WIDTH[font.get("typeface", "")]
    columns = int((int(box.get("cx", "0")) - 2 * 91425) / EMU_PER_POINT / glyph)
    return f"its footer is longer than a line of {columns} characters" if len(wanted) > columns else None


def check(source: Path, reference: Path, built: Path) -> list[str]:
    expected, meta = expected_slides(source)
    deck = Deck(built)
    ours = Deck(reference).layouts()
    theirs = deck.layouts()
    parts = deck.slides()
    errors = []
    if len(parts) != len(expected):
        errors.append(f"{len(parts)} slides, the source asks for {len(expected)}")
    for number, (part, want) in enumerate(zip(parts, expected), start=1):
        where = f"slide {number} ({want.title})"
        slide = deck.xml(part)
        layout_part = deck.related(part, "slideLayout")
        assert layout_part is not None
        layout = deck.xml(layout_part)
        content = layout.find(f"{P}cSld")
        assert content is not None
        name = content.get("name", "")
        if name != want.layout:
            errors.append(f"{where}: layout {name!r} instead of {want.layout!r}")
        if theirs[name] != ours.get(name):
            errors.append(f"{where}: the layout {name!r} is not the template's")
        boxes = placeholders(slide)
        titles = [text(shape) for (kind, _), shape in boxes.items() if kind in ("title", "ctrTitle")]
        if [title.replace("’", "'") for title in titles] != [want.title.replace("’", "'")]:
            errors.append(f"{where}: its title is {titles}")
        if number == 1:
            written = text(slide)
            for key in ("author", "date"):
                if meta[key] not in written:
                    errors.append(f"{where}: the {key} is missing")
        if want.notes and not deck.related(part, "notesSlide"):
            errors.append(f"{where}: its notes are missing")
        content = [line for (kind, _), shape in boxes.items() if kind == "obj" for p in shape.iter(f"{A}p") for line in lines(p)]
        if want.code and [line.rstrip() for line in want.code.split("\n")] != [line.rstrip() for line in content]:
            errors.append(f"{where}: its code is not the source's")
        if reason := footer_problem(slide, want.footer):
            errors.append(f"{where}: {reason}")
        layout_boxes = placeholders(layout)
        for key, shape in boxes.items():
            if key not in layout_boxes:
                errors.append(f"{where}: its layout has no placeholder {key}")
            elif reason := overflow(shape, layout_boxes[key]):
                errors.append(f"{where}: the {key[0]} overflows: {reason}")
    return errors


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    problems = check(*(Path(argument) for argument in sys.argv[1:]))
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"{len(problems)} problems" if problems else "the deck is sound")
    sys.exit(1 if problems else 0)
