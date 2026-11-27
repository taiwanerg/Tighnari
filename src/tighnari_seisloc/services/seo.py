"""Search discovery documents and metadata for the public observatory pages."""

from html import escape
from urllib.parse import quote, urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi.responses import HTMLResponse, Response

SITEMAP_SIZE = 1000


def public_base(request, configured):
    base = (configured or str(request.base_url)).rstrip("/")
    url = urlsplit(base)
    if (
        url.scheme not in ("http", "https")
        or not url.netloc
        or url.username
        or url.password
        or url.query
        or url.fragment
        or any(c.isspace() for c in base)
    ):
        raise ValueError(
            "TIGHNARI_PUBLIC_URL must be an absolute HTTP(S) URL without credentials, query or fragment"
        )
    return base


def event_path(eid):
    return "/events/" + quote(eid, safe="")


def sitemap_document(base, paths, *, index=False):
    root = Element(
        "sitemapindex" if index else "urlset",
        xmlns="http://www.sitemaps.org/schemas/sitemap/0.9",
    )
    for path in paths:
        entry = SubElement(root, "sitemap" if index else "url")
        SubElement(entry, "loc").text = base + path
    return Response(
        tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


def page_response(template, base, path, description, *, title=None, summary=None):
    html = template.read_text(encoding="utf-8")
    if title:
        start = html.index("<title>")
        end = html.index("</title>", start) + len("</title>")
        html = html[:start] + "<title>" + escape(title) + "</title>" + html[end:]
    metadata = (
        f'<meta name="description" content="{escape(description, quote=True)}" />\n'
        '<meta name="robots" content="index, follow" />\n'
        f'<link rel="canonical" href="{escape(base + path, quote=True)}" />\n'
    )
    html = html.replace("</head>", metadata + "</head>", 1)
    if summary:
        html = html.replace(
            '<main class="detail-main">',
            '<main class="detail-main"><noscript><p>' + escape(summary) + "</p></noscript>",
            1,
        )
    return HTMLResponse(html, headers={"Cache-Control": "public, no-cache"})
