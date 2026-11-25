"""Adapt the Google Slides template into a reference document pandoc accepts.

Usage: adapt_template.py TEMPLATE.pptx REFERENCE.pptx

pandoc finds the layouts by name, and the placeholders by type; the template has neither as pandoc wants them.
The template itself is never changed: every adjustment is here, and one that finds nothing to adjust is an error.
"""

import sys
import zipfile
from pathlib import Path
from xml.dom import minidom
from xml.dom.minidom import Document, Element

LAYOUTS = "ppt/slideLayouts/slideLayout"
NOTES_MASTER = "ppt/notesMasters/notesMaster1.xml"

# The layouts by file: the template's name, and the one pandoc looks for. pandoc wants a Comparison layout even when no
# slide uses it; the one given here lacks its placeholders, so that a slide that did use it would stop the build.
NAMES = {
    f"{LAYOUTS}1.xml": ("BLANK", "Blank"),
    f"{LAYOUTS}2.xml": ("TITLE", "Title Slide"),
    f"{LAYOUTS}3.xml": ("OBJECT", "Title and Content"),
    f"{LAYOUTS}4.xml": ("SECTION_HEADER", "Section Header"),
    f"{LAYOUTS}5.xml": ("TWO_OBJECTS", "Comparison"),
    f"{LAYOUTS}6.xml": ("TWO_OBJECTS_WITH_TEXT", "Two Content"),
    f"{LAYOUTS}7.xml": ("TITLE_ONLY", "Title Only"),
    f"{LAYOUTS}8.xml": ("OBJECT_WITH_CAPTION_TEXT", "Content with Caption"),
}

Box = tuple[int, int, int, int]  # x, y, width, height, in EMU


class TemplateChanged(Exception):
    """The template is not the one these adjustments were written for."""


def one(found: list[Element], what: str) -> Element:
    if len(found) != 1:
        raise TemplateChanged(f"expected one {what}, found {len(found)}")
    return found[0]


def child(element: Element, tag: str) -> Element:
    """The only element `tag` below `element`."""
    return one(list(element.getElementsByTagName(tag)), f"{tag} in {element.tagName}")


def placeholder(document: Document, kind: str, index: str = "") -> Element:
    """The shape holding the placeholder of type `kind` and index `index`."""
    shapes = []
    for shape in document.getElementsByTagName("p:sp"):
        for holder in shape.getElementsByTagName("p:ph"):
            if (holder.getAttribute("type"), holder.getAttribute("idx")) == (kind, index):
                shapes.append(shape)
    return one(shapes, f"placeholder {kind} {index}")


def decoration(document: Document, tag: str) -> Element:
    """The only `tag` shape that is not a placeholder."""
    return one([s for s in document.getElementsByTagName(tag) if not s.getElementsByTagName("p:ph")], f"decoration {tag}")


def retype(shape: Element, kind: str) -> None:
    """Give a placeholder the type `kind`; none is a generic content placeholder."""
    holder = child(shape, "p:ph")
    holder.removeAttribute("type")
    if kind:
        holder.setAttribute("type", kind)


def place(shape: Element, box: Box) -> None:
    offset, extent = child(shape, "a:off"), one(list(child(shape, "a:xfrm").getElementsByTagName("a:ext")), "a:ext")
    for element, names, values in ((offset, "x y", box[:2]), (extent, "cx cy", box[2:])):
        for name, value in zip(names.split(), values):
            element.setAttribute(name, str(value))


def resize(shape: Element, points: int) -> None:
    """Set the size of the text, and of its bullets, at every level."""
    for tag, attribute in (("a:defRPr", "sz"), ("a:buSzPts", "val")):
        for element in child(shape, "a:lstStyle").getElementsByTagName(tag):
            element.setAttribute(attribute, str(points * 100))


def unbullet(shape: Element) -> None:
    """Make the first level plain text: no bullet, no indent."""
    level = child(shape, "a:lvl1pPr")
    level.setAttribute("marL", "0")
    level.setAttribute("indent", "0")
    bullet = child(level, "a:buChar")
    level.replaceChild(shape.ownerDocument.createElement("a:buNone"), bullet)


def rename(document: Document, old: str, new: str) -> None:
    slide = child(document.documentElement, "p:cSld")
    if slide.getAttribute("name") != old:
        raise TemplateChanged(f"expected the layout {old!r}, found {slide.getAttribute('name')!r}")
    slide.setAttribute("name", new)


def adapt_title_slide(document: Document) -> None:
    title = placeholder(document, "title")
    retype(title, "ctrTitle")
    place(title, (930750, 1252000, 7900000, 4700000))
    resize(title, 54)
    author = placeholder(document, "body", "1")
    retype(author, "subTitle")
    unbullet(author)
    resize(author, 24)
    date = author.cloneNode(True)
    author.parentNode.appendChild(date)
    place(author, (930750, 6250000, 7900000, 1000000))
    retype(date, "dt")
    child(date, "p:ph").setAttribute("idx", "10")
    identity = child(date, "p:cNvPr")
    identity.setAttribute("id", "90")
    identity.setAttribute("name", "Date")
    place(date, (930750, 7350000, 7900000, 600000))


def adapt_content(document: Document) -> None:
    resize(placeholder(document, "title"), 50)
    body = placeholder(document, "body", "1")
    retype(body, "")
    place(body, (930750, 3400000, 9013800, 5500000))
    resize(body, 28)


def adapt_section(document: Document) -> None:
    """Centre the title; the card is for a text pandoc never writes: show the illustration whole in its place."""
    card = decoration(document, "p:pic")
    card.parentNode.removeChild(card)
    illustration = decoration(document, "p:sp")
    place(illustration, (10900000, 1643500, 5647500, 7000000))
    crop = child(illustration, "a:fillRect")
    for side in "lrtb":
        crop.setAttribute(side, "0")
    outline = child(illustration, "a:custGeom")
    rectangle = document.createElement("a:prstGeom")
    rectangle.setAttribute("prst", "rect")
    rectangle.appendChild(document.createElement("a:avLst"))
    outline.parentNode.replaceChild(rectangle, outline)
    title = placeholder(document, "title")
    place(title, (930750, 1643500, 9000000, 7000000))
    child(title, "a:bodyPr").setAttribute("anchor", "ctr")


def adapt_two_content(document: Document) -> None:
    resize(placeholder(document, "title"), 50)
    for index, x in (("1", 3400000), ("2", 9500000)):
        body = placeholder(document, "body", index)
        retype(body, "")
        place(body, (x, 3350000, 5400000, 4800000))
        resize(body, 24)


def adapt_caption(document: Document) -> None:
    retype(placeholder(document, "body", "1"), "")


def adapt_notes_master(document: Document) -> None:
    """Add the slide number pandoc copies its field id from."""
    if document.getElementsByTagName("a:fld"):
        raise TemplateChanged("the notes master has a field already")
    number = minidom.parseString(
        '<p:sp xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:nvSpPr><p:cNvPr id="90" name="Slide Number"/><p:cNvSpPr txBox="1"/>'
        '<p:nvPr><p:ph type="sldNum" idx="12" sz="quarter"/></p:nvPr></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="3884613" y="8685213"/><a:ext cx="2971800" cy="457200"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        "<p:txBody><a:bodyPr/><a:lstStyle/><a:p>"
        '<a:fld id="{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}" type="slidenum"><a:rPr lang="it-IT"/><a:t>‹#›</a:t></a:fld>'
        "</a:p></p:txBody></p:sp>"
    ).documentElement
    child(document.documentElement, "p:spTree").appendChild(document.importNode(number, True))


ADAPTATIONS = {
    f"{LAYOUTS}2.xml": adapt_title_slide,
    f"{LAYOUTS}3.xml": adapt_content,
    f"{LAYOUTS}4.xml": adapt_section,
    f"{LAYOUTS}6.xml": adapt_two_content,
    f"{LAYOUTS}8.xml": adapt_caption,
    NOTES_MASTER: adapt_notes_master,
}


def adapt(template: Path, reference: Path) -> None:
    with zipfile.ZipFile(template) as source:
        missing = (NAMES.keys() | ADAPTATIONS.keys()) - set(source.namelist())
        if missing:
            raise TemplateChanged(f"missing parts: {', '.join(sorted(missing))}")
        parts = [(entry, source.read(entry)) for entry in source.infolist()]
    adapted = []
    for entry, content in parts:
        name = entry.filename
        if name in NAMES or name in ADAPTATIONS:
            document = minidom.parseString(content)
            if name in NAMES:
                rename(document, *NAMES[name])
            if name in ADAPTATIONS:
                ADAPTATIONS[name](document)
            content = document.toxml(encoding="UTF-8")
        adapted.append((entry, content))
    with zipfile.ZipFile(reference, "w") as target:
        for entry, content in adapted:
            target.writestr(entry, content, zipfile.ZIP_DEFLATED)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    try:
        adapt(Path(sys.argv[1]), Path(sys.argv[2]))
    except TemplateChanged as error:
        sys.exit(f"{sys.argv[1]}: {error}")
