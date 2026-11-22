import argparse
from pathlib import Path

from reportlab.pdfgen import canvas


def generate(path, omit_page_label=None, move_page_label=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = canvas.Canvas(str(path))
    for page in range(1, 8):
        doc.setFont("Helvetica-Bold", 20)
        doc.drawString(65, 740, f"Workshop Section {page}")
        doc.setFont("Helvetica", 13)
        doc.drawString(65, 700, f"Operation {page}: inspect the pick station to prevent mislabeled trays.")
        doc.drawString(65, 678, "Category A describes relay inspection during normal production.")
        if page not in (omit_page_label, move_page_label):
            doc.drawString(65, 650, f"Ref-P{page}")
        if page == 7 and move_page_label is not None:
            doc.drawString(65, 625, f"Ref-P{move_page_label}")
        doc.showPage()
    doc.save()
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path, nargs="?", default=Path(".local/sample.pdf"))
    args = parser.parse_args()
    print(generate(args.path))
