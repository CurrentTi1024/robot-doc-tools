import io
import json
import math
import os
from collections import Counter
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

import pypdfium2 as pdfium

from .config import DATA, MAX_PAGES

PDF_VALIDATION_LOCK = Lock()


class InvalidPDF(ValueError):
    pass


def validate_pdf(body):
    if not body.startswith(b"%PDF-"):
        raise InvalidPDF("Invalid PDF signature")
    try:
        with PDF_VALIDATION_LOCK, pdfium.PdfDocument(body) as pdf:
            if not 1 <= len(pdf) <= MAX_PAGES:
                raise InvalidPDF("PDF page limit exceeded")
            if pdfium.raw.FPDF_GetSecurityHandlerRevision(pdf.raw) >= 0:
                raise InvalidPDF("Encrypted PDFs are unsupported")
            for index in range(len(pdf)):
                with closing(pdf[index]) as page:
                    width, height = page.get_size()
                    if not all(math.isfinite(n) and 0 < n <= 6000 for n in (width, height)):
                        raise InvalidPDF("PDF page dimensions exceed processing limits")
    except pdfium.PdfiumError:
        raise InvalidPDF("Unreadable or password-protected PDF") from None


@lru_cache(maxsize=1)
def paddle_pipeline():
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(DATA / "paddlex"))
    from paddleocr import PaddleOCRVL
    model_root = DATA / "models"
    layout = os.getenv("RW_LAYOUT_DIR") or (model_root / "layout" if (model_root / "layout" / "inference.pdiparams").is_file() else None)
    vl = os.getenv("RW_VL_DIR") or (model_root / "vl" if (model_root / "vl" / "model.safetensors").is_file() else None)
    return PaddleOCRVL(
        pipeline_version="v1.6",
        layout_detection_model_dir=str(layout) if layout else None,
        vl_rec_model_dir=str(vl) if vl else None,
        device=os.getenv("RW_OCR_DEVICE", "gpu:0"),
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_chart_recognition=True,
        use_ocr_for_image_block=True,
    )


def normalized(text):
    return " ".join(text.casefold().split())


def extract(body, rules, storage, prefix, *, smoke=False):
    pages = []
    DATA.mkdir(parents=True, exist_ok=True)
    with pdfium.PdfDocument(body) as pdf, TemporaryDirectory(prefix="rw-pdf-", dir=DATA) as directory:
        for index in range(len(pdf)):
            with closing(pdf[index]) as page, closing(page.get_textpage()) as textpage:
                native = textpage.get_text_range()
                scale = min(2.0, 2200 / max(page.get_size()))
                with closing(page.render(scale=scale)) as bitmap:
                    image = bitmap.to_pil().copy()
            number = index + 1
            path = Path(directory) / f"{number}.png"
            image.save(path)
            storage.put(f"{prefix}/{number}.png", path.read_bytes())
            blocks = []
            if not smoke:
                results = list(paddle_pipeline().predict(str(path)))
                if len(results) != 1:
                    raise ValueError("OCR returned an unexpected number of pages")
                payload = results[0].json
                payload = payload.get("res", payload)
                items = payload["parsing_res_list"]
                for entry in items:
                    bounds = entry["block_bbox"]
                    if len(bounds) != 4 or not all(math.isfinite(float(n)) for n in bounds):
                        continue
                    x0, y0, x1, y1 = map(round, bounds)
                    box = (max(0, x0), max(0, y0), min(image.width, x1), min(image.height, y1))
                    if box[0] >= box[2] or box[1] >= box[3]:
                        continue
                    token = f"p{number}-b{len(blocks) + 1}"
                    ref = f"{prefix}/{token}.png"
                    crop = io.BytesIO()
                    image.crop(box).save(crop, format="PNG")
                    storage.put(ref, crop.getvalue())
                    blocks.append({"id": token, "type": entry["block_label"],
                                   "text": str(entry.get("block_content") or ""),
                                   "bbox": list(box), "image_ref": ref})
            if native.strip():
                blocks.append({"id": f"p{number}-native", "type": "native_text", "text": native,
                               "bbox": None, "image_ref": f"{prefix}/{number}.png"})
            content = normalized(" ".join(block["text"] for block in blocks))
            matches = [page_rule["page"] for page_rule in rules["pages"]
                       if any(normalized(anchor) in content for anchor in page_rule["anchors"])]
            pages.append({"pdf_page": number, "template_page": matches[0] if len(matches) == 1 else None,
                          "pixel_size": [image.width, image.height], "bbox_space": "rendered_image_xyxy",
                          "image_ref": f"{prefix}/{number}.png", "blocks": blocks})
    counts = Counter(page["template_page"] for page in pages)
    for page in pages:
        if counts[page["template_page"]] > 1:
            page["template_page"] = None
    engine = "pdfium_native_smoke" if smoke else "PaddleOCR-VL-1.6"
    return {"engine": engine, "pages": pages}
