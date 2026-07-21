#!/usr/bin/env python3
"""Extract PDF text and annotations; optionally render pages for visual review."""

import argparse
import json
from pathlib import Path

import pymupdf


def inspect_pdf(path: Path, render_dir: Path | None = None, dpi: int = 144) -> dict:
    document = pymupdf.open(path)
    if document.needs_pass:
        raise ValueError("PDF is encrypted and requires a password")

    pages = []
    for page_index, page in enumerate(document):
        annotations = []
        annot = page.first_annot
        while annot:
            info = annot.info or {}
            annotations.append({
                "type": annot.type[1],
                "content": info.get("content") or "",
                "author": info.get("title") or "",
                "subject": info.get("subject") or "",
                "created_at": info.get("creationDate") or "",
                "modified_at": info.get("modDate") or "",
                "rect": list(annot.rect),
            })
            annot = annot.next

        rendered_path = None
        if render_dir is not None:
            render_dir.mkdir(parents=True, exist_ok=True)
            rendered = render_dir / f"page-{page_index + 1:04d}.png"
            page.get_pixmap(dpi=dpi, alpha=False).save(rendered)
            rendered_path = str(rendered.resolve())

        pages.append({
            "page": page_index + 1,
            "text": page.get_text("text"),
            "annotations": annotations,
            "rendered_path": rendered_path,
        })

    result = {
        "file": str(path.resolve()),
        "format": "pdf",
        "page_count": len(document),
        "metadata": dict(document.metadata or {}),
        "pages": pages,
    }
    document.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--dpi", type=int, default=144)
    args = parser.parse_args()
    result = inspect_pdf(args.input, args.render_dir, args.dpi)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
