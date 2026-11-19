#!/usr/bin/env python3
"""Build the static demo site: the real pages from web/, with paths made relative and demo/fake-api.js loaded first.

    python3 demo/build.py [outdir]     (default: _site)

The pages are otherwise unchanged. The fake answers every API request in the browser, so any static file server
(or GitHub Pages under a subpath) can host the result. See demo/README.md.
"""

import shutil
import sys
from pathlib import Path

REPO: Path = Path(__file__).resolve().parent.parent
WEB: Path = REPO / "web"
VENDOR: Path = REPO / ".local" / "web"
VENDORED: tuple[str, ...] = (
    "three.module.min.js",
    "OrbitControls.js",
    "highlight.min.js",
)
# console.html is the site's front page
PAGES: dict[str, str] = {
    "console.html": "index.html",
    "analytics.html": "analytics.html",
    "analytics3d.html": "analytics3d.html",
}
SHIM: str = '<script src="demo/fake-api.js"></script>\n<script src="auth.js"></script>'
# (file, old, new, how many times old must occur): a page that changed under one of these fails the build
REWRITES: list[tuple[str, str, str, int]] = [
    *[(p, '<script src="/auth.js"></script>', SHIM, 1) for p in PAGES],
    *[
        (p, '<script src="/base.js"></script>', '<script src="base.js"></script>', 1)
        for p in PAGES
    ],
    *[(p, 'href="/arke.css"', 'href="arke.css"', 1) for p in PAGES],
    ("console.html", 'href="/analytics"', 'href="analytics.html"', 1),
    ("console.html", 'href="/analytics3d"', 'href="analytics3d.html"', 1),
    ("console.html", '"/vendor/highlight.min.js"', '"vendor/highlight.min.js"', 1),
    ("analytics.html", 'href="/"', 'href="./"', 2),
    ("analytics.html", 'href="analytics"', 'href="analytics.html"', 1),
    ("analytics.html", 'href="analytics3d"', 'href="analytics3d.html"', 1),
    ("analytics3d.html", 'href="/"', 'href="./"', 2),
    ("analytics3d.html", 'href="analytics"', 'href="analytics.html"', 1),
    ("analytics3d.html", 'href="analytics3d"', 'href="analytics3d.html"', 1),
    # the import map: its addresses need the leading ./
    ("analytics3d.html", '"/vendor/', '"./vendor/', 2),
    ("analytics-page.js", '"/#agent/"', '"./#agent/"', 1),
    ("analytics-page.js", '"/#project/"', '"./#project/"', 1),
    # the demo's invented history covers about two days: a week-long default view would be mostly empty
    ("analytics-page.js", 'pref("an.range", "604800")', 'pref("an.range", "86400")', 1),
]
# what must not survive in a built page: a path that only the real server answers
LEFTOVERS: tuple[str, ...] = ('src="/', 'href="/', '"/vendor/')


def build(out: Path) -> None:
    out = out.resolve()
    if out in (REPO, WEB, REPO / "demo", REPO / "arke") or REPO.is_relative_to(out):
        sys.exit(f"refusing to build into {out}: it would overwrite the checkout")
    missing: list[str] = [n for n in VENDORED if not (VENDOR / n).is_file()]
    if missing:
        sys.exit(
            f"missing {', '.join(missing)} in {VENDOR}: run python3 install-service.py --assets-only"
        )
    texts: dict[str, str] = {
        p.name: p.read_text(encoding="utf-8")
        for p in [*WEB.glob("*.html"), *WEB.glob("*.js"), *WEB.glob("*.css")]
    }
    for name, old, new, count in REWRITES:
        found: int = texts[name].count(old)
        if found != count:
            sys.exit(
                f"{name}: expected {count} of {old!r}, found {found}. Update REWRITES in demo/build.py."
            )
        texts[name] = texts[name].replace(old, new)
    for name in PAGES:
        for bad in LEFTOVERS:
            if bad in texts[name]:
                sys.exit(
                    f"{name} still has an absolute path ({bad}...). Add a rewrite for it in demo/build.py."
                )
    for sub in ("demo", "vendor"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        (out / PAGES.get(name, name)).write_text(text, encoding="utf-8")
    for name in VENDORED:
        shutil.copyfile(VENDOR / name, out / "vendor" / name)
    shutil.copyfile(REPO / "demo" / "fake-api.js", out / "demo" / "fake-api.js")
    shutil.copyfile(REPO / "arke" / "help.md", out / "help.md")
    (out / ".nojekyll").touch()  # GitHub Pages: serve the files as they are
    print(f"demo built in {out}")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "_site"))
