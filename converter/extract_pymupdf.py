#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf>=1.24"]
# ///
"""Turn an ebook PDF into daily lesson pages, reading the PDF with PyMuPDF.

The same as extract_pdfplumber.py with a different PDF reader, so the two can be
compared on the same book. Runs entirely on this computer. PyMuPDF is AGPL
licensed: fine for running it ourselves, but check the licence before shipping it
inside a product. All options are convert.py's (see --help).

  uv run converter/extract_pymupdf.py BOOK.pdf
  uv run converter/extract_pymupdf.py BOOK.pdf --pages 23-38

Output: converter/output/<book-slug>-pymupdf/ with preview.html, day-NN.html and
.txt, figures/ and lessons.json.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert  # noqa: E402

if __name__ == "__main__":
    sys.exit(convert.main(["--engine", "pymupdf", *sys.argv[1:]]))
