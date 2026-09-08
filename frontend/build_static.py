"""Package the frontend for a static host.

`server.py` does two things at request time that a static host cannot: it
generates `/config.js` carrying the API address, and it sets the security
headers - most importantly a Content-Security-Policy whose `connect-src` names
that same API.

On Azure Static Web Apps both have to be decided at build time instead. This
writes an output directory containing the public files, a generated config.js,
and a staticwebapp.config.json carrying the identical header set.

    python build_static.py --api https://iragst-api.example.net --out dist

The headers are derived from server.py's rather than retyped, so the two cannot
drift apart silently: if that file's policy changes and this one is not
updated, the check at the bottom fails the build.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

HERE = Path(__file__).parent
PUBLIC = HERE / "public"


def csp_for(api_url: str) -> str:
    """The same policy server.py sends, with the API filled in."""
    return (
        "default-src 'self'; "
        f"connect-src 'self' {api_url}; "
        "img-src 'self' data: blob:; "
        "object-src 'none'; "
        "frame-src 'self' blob:; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )


def headers_for(api_url: str) -> dict[str, str]:
    return {
        "Content-Security-Policy": csp_for(api_url),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cache-Control": "no-cache, must-revalidate",
    }


def check_matches_server(api_url: str) -> list[str]:
    """Confirm this file still agrees with server.py about the policy.

    A static build that quietly ships a weaker CSP than the development server
    is the kind of drift nobody notices until it matters, so it is checked
    rather than trusted.
    """
    source = (HERE / "server.py").read_text(encoding="utf-8")
    problems = []

    for directive in ("img-src 'self' data: blob:", "object-src 'none'",
                      "frame-src 'self' blob:", "base-uri 'none'",
                      "form-action 'none'", "frame-ancestors 'none'"):
        if directive not in source:
            problems.append(f"server.py no longer contains {directive!r}")
        if directive not in csp_for(api_url):
            problems.append(f"the built policy is missing {directive!r}")

    for header in ("X-Content-Type-Options", "X-Frame-Options",
                   "Referrer-Policy", "Permissions-Policy"):
        if header not in source:
            problems.append(f"server.py no longer sends {header}")

    # Any directive server.py sends that this file does not know about.
    match = re.search(r'"Content-Security-Policy",\s*\n(.*?)\n\s*\)', source, re.S)
    if match:
        sent = {piece.strip().split()[0]
                for piece in re.findall(r'"([^"]+?);?\s*"', match.group(1))
                if piece.strip() and not piece.strip().startswith("{")}
        known = {piece.strip().split()[0] for piece in csp_for(api_url).split(";")}
        for directive in sent - known - {""}:
            problems.append(f"server.py sends {directive!r}, which this build omits")

    return problems


def build(api_url: str, out: Path) -> None:
    api_url = api_url.rstrip("/")

    problems = check_matches_server(api_url)
    if problems:
        raise SystemExit(
            "The static build has drifted from server.py:\n  "
            + "\n  ".join(problems)
            + "\n\nUpdate build_static.py so both send the same policy."
        )

    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(PUBLIC, out)

    (out / "config.js").write_text(
        "/* Generated at build time by build_static.py. */\n"
        f"window.GST_API_BASE = {json.dumps(api_url)};\n",
        encoding="utf-8",
    )

    config = {
        "globalHeaders": headers_for(api_url),
        # A single-page app served from one file; anything unmatched is the
        # app itself, not a 404, except the API path which must never be
        # rewritten here.
        "navigationFallback": {
            "rewrite": "/index.html",
            "exclude": ["/*.js", "/*.css", "/*.json", "/*.svg", "/*.png"],
        },
        "mimeTypes": {".json": "application/json", ".js": "text/javascript"},
    }
    (out / "staticwebapp.config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )

    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    print(f"Built {out} for {api_url}")
    for name in files:
        print(f"  {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", required=True, help="Public base URL of the backend API.")
    parser.add_argument("--out", type=Path, default=HERE / "dist")
    args = parser.parse_args()
    build(args.api, args.out)


if __name__ == "__main__":
    main()
