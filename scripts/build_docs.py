"""Generate the static GitHub Pages dashboard from the web/ assets.

    python scripts/build_docs.py

Same page as the local dashboard; the only difference is a data-endpoint
attribute on <body> pointing at the committed status JSON instead of the
live API. Run whenever web/ changes (the CI workflow also runs it).
"""
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scanner.main import stamp_assets                        # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB, DOCS = ROOT / "web", ROOT / "docs"


def main():
    (DOCS / "static").mkdir(parents=True, exist_ok=True)
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    for name in ("style.css", "app.js"):
        shutil.copyfile(WEB / name, DOCS / "static" / name)
    html = (WEB / "index.html").read_text(encoding="utf-8")
    html = html.replace("<body>", '<body data-endpoint="data/status.json">', 1)
    # Same content hash the local server uses: a phone that cached the old
    # dashboard must not keep rendering it after a deploy.
    html = stamp_assets(html, WEB)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    print(f"docs built -> {DOCS}")


if __name__ == "__main__":
    main()
