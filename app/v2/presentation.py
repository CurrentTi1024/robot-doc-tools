from collections import Counter
from io import BytesIO
from math import cos, hypot, radians, sin
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock
from zipfile import BadZipFile, ZipFile

import pypdfium2 as pdfium
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.oxml.ns import qn

from app.config import MAX_BYTES


PDF_RENDER_LOCK = Lock()


class InvalidPresentation(ValueError):
    pass


def open_pptx(body):
    if len(body) > MAX_BYTES:
        raise InvalidPresentation("PPTX exceeds the upload limit")
    stream = BytesIO(body)
    try:
        with ZipFile(stream) as package:
            entries = package.infolist()
            names = set(package.namelist())
            if not {"[Content_Types].xml", "ppt/presentation.xml"} <= names:
                raise InvalidPresentation("Expected a PowerPoint .pptx package")
            if (len(names) != len(entries) or len(entries) > 10_000
                    or sum(part.file_size for part in entries) > 256 * 1024 * 1024):
                raise InvalidPresentation("PPTX exceeds package limits")
            if any("vbaproject" in name.lower() or "/activex/" in name.lower() for name in names):
                raise InvalidPresentation("Use a macro-free PPTX")
        return Presentation(stream)
    except InvalidPresentation:
        raise
    except (BadZipFile, ValueError, KeyError, OSError, TypeError, SyntaxError, RuntimeError, NotImplementedError) as exc:
        raise InvalidPresentation("Unreadable PowerPoint package") from exc


def normalized(text):
    return " ".join(text.casefold().split())


def mapped_pages(presentation, pages):
    anchors = {
        entry["page"]: [re.compile(r"(?<![a-z0-9])" + re.escape(normalized(a)) + r"(?![a-z0-9])")
                        for a in entry["anchors"]]
        for entry in pages
    }
    found = {}
    for number, slide in enumerate(presentation.slides, 1):
        if slide._element.get("show") in ("0", "false", "off"):
            continue
        title = slide.shapes.title
        titles = ([normalized(title.text)] if title is not None and not hidden(title) else
                  [normalized(shape.text) for shape in slide.shapes
                   if shape.has_text_frame and not hidden(shape)])
        matches = [page for page, terms in anchors.items()
                   if any(term.search(text) for term in terms for text in titles)]
        if len(matches) < 2:
            found[number] = matches[0] if matches else None
    duplicates = Counter(value for value in found.values() if value is not None)
    reserved = set(duplicates)
    return {number: page for number, page in found.items()
            if page is not None and (duplicates[page] == 1 or number == page)} | {
        number: number for number, page in found.items()
        if page is None and number in anchors and number not in reserved
    }


def hidden(shape):
    props = shape._element.xpath("./*/p:cNvPr")
    return bool(props and props[0].get("hidden") in ("1", "true", "on"))


def rotate(x, y, cx, cy, angle, flip_x=False, flip_y=False):
    dx = (x - cx) * (-1 if flip_x else 1)
    dy = (y - cy) * (-1 if flip_y else 1)
    return cx + cos(angle) * dx - sin(angle) * dy, cy + sin(angle) * dx + cos(angle) * dy


def group_transform(shape, parent):
    transform = shape._element.grpSpPr.xfrm
    origin, extent = transform.off, transform.ext
    child, size = transform.chOff, transform.chExt

    def to_slide(x, y):
        x = origin.x + (x - child.x) * extent.cx / (size.cx or 1)
        y = origin.y + (y - child.y) * extent.cy / (size.cy or 1)
        return parent(*rotate(x, y, origin.x + extent.cx / 2, origin.y + extent.cy / 2,
                              radians(shape.rotation), transform.flipH, transform.flipV))

    return to_slide


def chart_text(chart):
    parts = []
    if chart.has_title and chart.chart_title.has_text_frame:
        parts.append(chart.chart_title.text_frame.text)
    for plot in chart.plots:
        categories = [category.label for category in plot.categories]
        for series in plot.series:
            values = list(series.values)
            labels = categories or list(range(1, len(values) + 1))
            parts.append(f"{series.name}: " + "; ".join(
                f"{label}={value}" for label, value in zip(labels, values)
            ))
    return "\n".join(parts)


def line_data(shape, position):
    props = shape._element.xpath("./p:nvCxnSpPr/p:cNvCxnSpPr")[0]
    anchors = [props.find(qn(f"a:{side}Cxn")) for side in ("st", "end")]
    line = shape._element.xpath("./p:spPr/a:ln")
    arrows = [line[0].find(qn(f"a:{end}End")) if line else None for end in ("head", "tail")]
    direction = [end is not None and end.get("type", "none") != "none" for end in arrows]
    geometry = shape._element.xpath("./p:spPr/a:prstGeom")
    return {
        "ends": [int(anchor.get("id")) if anchor is not None else None for anchor in anchors],
        "points": [position(shape.begin_x, shape.begin_y), position(shape.end_x, shape.end_y)],
        "arrows": direction,
        "straight": bool(geometry and geometry[0].get("prst") == "line" and not shape.rotation),
    }


def objects(shapes, pos, grounds, width, height):
    blocks, lines = grounds
    for shape in shapes:
        if hidden(shape):
            continue
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            objects(shape.shapes, group_transform(shape, pos), grounds, width, height)
            continue
        x, y, w, h = shape.left, shape.top, shape.width, shape.height
        if any(value is None for value in (x, y, w, h)):
            continue
        corners = [pos(*rotate(px, py, x + w / 2, y + h / 2, radians(shape.rotation)))
                   for px in (x, x + w) for py in (y, y + h)]
        xs, ys = zip(*corners)
        rect = [max(0, min(xs)), max(0, min(ys)), min(width, max(xs)), min(height, max(ys))]
        if max(xs) < 0 or max(ys) < 0 or min(xs) > width or min(ys) > height:
            continue
        if shape.shape_type == MSO_SHAPE_TYPE.LINE and shape._element.tag == qn("p:cxnSp"):
            lines[shape.shape_id] = line_data(shape, pos)
            continue

        text, kind, visual = "", "text", False
        if shape.has_table:
            text = "\n".join(" | ".join(cell.text if not cell.is_spanned else "" for cell in row.cells)
                             for row in shape.table.rows)
            kind = "table"
        elif shape.has_chart:
            kind, visual = "chart", True
            try:
                text = chart_text(shape.chart)
            except (AttributeError, ValueError, NotImplementedError):
                pass  # Unsupported chart types can still be read as rendered evidence.
        elif shape.has_text_frame:
            text = shape.text.strip()
            if shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
                kind = "shape"
        elif shape.shape_type in {MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.LINKED_PICTURE,
                                   MSO_SHAPE_TYPE.IGX_GRAPHIC, MSO_SHAPE_TYPE.DIAGRAM,
                                   MSO_SHAPE_TYPE.FREEFORM, MSO_SHAPE_TYPE.PLACEHOLDER,
                                   MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT}:
            kind, visual = "visual", True
        if text.strip() or visual or kind == "shape":
            blocks[shape.shape_id] = {
                "kind": kind, "text": text, "visual": visual, "bbox_emu": rect,
            }


def distance_to_segment(point, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = dx * dx + dy * dy
    projection = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length if length else 0
    t = max(0, min(1, projection))
    return hypot(point[0] - start[0] - dx * t, point[1] - start[1] - dy * t)


def near_node(point, shapes, tolerance):
    candidates = []
    for identifier, (x1, y1, x2, y2) in shapes.items():
        if x1 <= point[0] <= x2 and y1 <= point[1] <= y2:
            distance = min(point[0] - x1, x2 - point[0], point[1] - y1, y2 - point[1])
        else:
            distance = hypot(max(x1 - point[0], 0, point[0] - x2),
                             max(y1 - point[1], 0, point[1] - y2))
        if distance <= tolerance:
            candidates.append(identifier)
    return candidates[0] if len(candidates) == 1 else None


BRANCH_LABEL = re.compile(r"^(y|n|yes|no|ok|ng|true|false|是|否|通过|不通过|通過|不通過)$", re.I)


def flow_graph(blocks, lines, width):
    nodes = {sid: item["bbox_emu"] for sid, item in blocks.items()
             if item["kind"] == "shape" and item["text"] and not BRANCH_LABEL.fullmatch(item["text"])}
    result = []
    for sid, line in lines.items():
        endpoints = [bound if bound in blocks else near_node(point, nodes, width * 0.009)
                     for bound, point in zip(line["ends"], line["points"])]
        head, tail = line["arrows"]
        if head and not tail:
            endpoints.reverse()
        # Keep undirected / unresolved connectors available for visual review.
        relation = {
            "shape_id": sid, "source": endpoints[0], "target": endpoints[1],
            "direction": "forward" if head != tail else "both" if head else "unknown",
            "provenance": "explicit" if all(bound in blocks for bound in line["ends"])
            else "geometry" if all(endpoints) else "unresolved",
            "labels": [],
        }
        result.append(relation)
    outdegree = Counter(edge["source"] for edge in result if all((edge["source"], edge["target"]))
                        and edge["direction"] == "forward")
    for sid, block in blocks.items():
        if not BRANCH_LABEL.fullmatch(block["text"]):
            continue
        x1, y1, x2, y2 = block["bbox_emu"]
        center = (x1 + x2) / 2, (y1 + y2) / 2
        candidates = sorted(
            [(distance_to_segment(center, *lines[edge["shape_id"]]["points"]), edge["shape_id"], edge)
             for edge in result if edge["direction"] == "forward"
             and outdegree[edge["source"]] >= 2 and edge["target"]
             and lines[edge["shape_id"]]["straight"]]
        )
        close = [(distance, edge) for distance, _, edge in candidates if distance < width * 0.045]
        if close and (len(close) == 1 or close[1][0] - close[0][0] > width * 0.006):
            close[0][1]["labels"].append({"shape_id": sid, "text": block["text"], "provenance": "geometry"})
    return result


def extract(presentation, rules, template=None):
    width, height = presentation.slide_width, presentation.slide_height
    if not width or not height:
        raise InvalidPresentation("PPTX page dimensions are missing")
    mapping = mapped_pages(presentation, rules["pages"])
    placeholders = {}
    if template is not None:
        other = extract(template, rules)
        for page in other["pages"]:
            placeholders[page["template_page"]] = {normalized(block["text"]) for block in page["blocks"]
                                                    if block["text"]}
    anchors = {normalized(anchor) for entry in rules["pages"] for anchor in entry["anchors"]}
    pages = []
    for number, slide in enumerate(presentation.slides, 1):
        if number not in mapping:
            continue
        blocks, lines = {}, {}
        objects(slide.shapes, lambda x, y: (x, y), (blocks, lines), width, height)
        links = flow_graph(blocks, lines, width)
        for identifier, block in blocks.items():
            block["id"] = f"p{number}-s{identifier}"
            if normalized(block["text"]) in (anchors | placeholders.get(mapping[number], set())):
                block["text"] = ""
            x1, y1, x2, y2 = block.pop("bbox_emu")
            block["bbox"] = [round(1000 * x1 / width), round(1000 * y1 / height),
                             round(1000 * x2 / width), round(1000 * y2 / height)]
        for edge in links:
            for key in ("source", "target"):
                edge[key] = blocks[edge[key]]["id"] if edge[key] in blocks else None
            for label in edge["labels"]:
                label["evidence_id"] = blocks[label["shape_id"]]["id"]
        if links:
            blocks["flow"] = {"id": f"p{number}-flow", "kind": "flow", "text": "", "edges": links,
                              "visual": True, "bbox": [0, 0, 1000, 1000]}
        pages.append({"page": number, "template_page": mapping[number],
                      "blocks": [block for block in blocks.values()
                                 if block["text"].strip() or block["visual"] or block["kind"] == "shape"]})
    return {"slide_count": len(presentation.slides), "pages": pages}


def render_pdf(body, binary, timeout=90):
    with TemporaryDirectory(prefix="rw-v2-") as folder:
        root = Path(folder)
        source = root / "proposal.pptx"
        source.write_bytes(body)
        # Use a private LibreOffice profile with macros disabled.
        profile = root / "profile"
        (profile / "user").mkdir(parents=True)
        (profile / "user" / "registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '</item></oor:items>', encoding="utf-8",
        )
        export = 'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"}}'
        subprocess.run([binary, f"-env:UserInstallation={profile.as_uri()}", "--headless",
                        "--convert-to", export, "--outdir", folder, str(source)],
                       check=True, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return (root / "proposal.pdf").read_bytes()


def crop_png(pdf, page_number, bbox, max_width=2048):
    from contextlib import closing

    with PDF_RENDER_LOCK, pdfium.PdfDocument(pdf) as doc, closing(doc[page_number - 1]) as page:
        width, height = page.get_size()
        if width <= 0 or height <= 0:
            raise InvalidPresentation("Invalid rendered slide size")
        ratio = min(4, max_width / max(width, height))
        with closing(page.render(scale=ratio)) as bitmap:
            image = bitmap.to_pil()
            x1, y1, x2, y2 = bbox
            crop = image.crop((int(image.width * x1 / 1000), int(image.height * y1 / 1000),
                               max(1, round(image.width * x2 / 1000)), max(1, round(image.height * y2 / 1000))))
            data = BytesIO()
            crop.save(data, format="PNG")
            return data.getvalue()
