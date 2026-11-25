"""Build the static site into out/: the tour, the tapes, and the dashboard from the installed argus-iris wheel.

The dashboard's files are copied unchanged except index.html, which gains one stylesheet and one script tag.
"""

from __future__ import annotations

import shutil
from importlib.resources import files
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUT = ROOT / "out"
# Cloudflare Web Analytics is on for the zone, as on dragonhound.dev: its edge adds the beacon script to HTML.
CSP = "; ".join(
    [
        "default-src 'self'", "script-src 'self' https://static.cloudflareinsights.com", "style-src 'self'", "img-src 'self'", "connect-src 'self' https://cloudflareinsights.com",
        "frame-src 'self'", "frame-ancestors 'self'", "object-src 'none'", "base-uri 'none'", "form-action 'none'",
    ]
)  # fmt: skip


def once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"expected exactly one {old!r}")
    return text.replace(old, new)


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.copytree(ROOT / "web", OUT)
    shutil.copytree(ROOT / "tape", OUT / "tape")
    ui = OUT / "dash" / "ui"
    shutil.copytree(str(files("argus_iris") / "ui"), ui)

    index = ui / "index.html"
    html = once(
        index.read_text(),
        '<script type="module" src="app.js"></script>',
        '<link rel="stylesheet" href="../demo.css">\n  <script src="../shim.js"></script>\n'
        '  <script type="module" src="app.js"></script>',
    )
    index.write_text(html)

    strip = (ROOT / "web/vendor/dh/dh-strip.html").read_text()
    page = OUT / "index.html"
    page.write_text(once(page.read_text(), "<!-- dh-strip -->", strip[strip.index("<nav") :]))
    (OUT / "_headers").write_text(
        f"/*\n  Content-Security-Policy: {CSP}\n  X-Frame-Options: SAMEORIGIN\n  X-Content-Type-Options: nosniff\n"
        "  Referrer-Policy: strict-origin-when-cross-origin\n  Permissions-Policy: camera=(), geolocation=(), microphone=()\n"
        "  Cross-Origin-Opener-Policy: same-origin\n  Strict-Transport-Security: max-age=31536000\n"
        # The framed dashboard is part of the same visit: no-transform keeps the edge from adding a second beacon to it.
        "/dash/ui/*\n  Cache-Control: public, max-age=0, must-revalidate, no-transform\n"
    )
    print(f"built {sum(1 for f in OUT.rglob('*') if f.is_file())} files into {OUT.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
