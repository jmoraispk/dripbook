#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pdfplumber>=0.11"]
# ///
"""Turn an ebook PDF into daily lesson pages, reading the PDF with pdfplumber.

Runs entirely on this computer. Lessons start at the book's headings and run 3 to
5 minutes; their text is copied from the PDF unchanged and figures are cropped
out as images. All options are convert.py's (see --help).

  uv run converter/extract_pdfplumber.py BOOK.pdf
  uv run converter/extract_pdfplumber.py BOOK.pdf --pages 23-38

Output: converter/output/<book-slug>/ with preview.html, day-NN.html and .txt,
figures/ and lessons.json.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert  # noqa: E402

if __name__ == "__main__":
    sys.exit(convert.main(["--engine", "pdfplumber", *sys.argv[1:]]))
