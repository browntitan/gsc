import html
import re
from bs4 import BeautifulSoup


def _strip_page_directives(raw: str) -> str:
    """Remove ASP.NET <%@ ... %> directives."""
    return re.sub(r"^\s*<%@.*?%>\s*", "", raw, flags=re.MULTILINE)


def _extract_canvas_via_regex(raw: str) -> str:
    """
    Extract the inner HTML from <mso:CanvasContent1>...</mso:CanvasContent1>
    if present. SharePoint often stores the real page content here.
    """
    match = re.search(
        r"<mso:CanvasContent1[^>]*>(.*?)</mso:CanvasContent1>",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""

    escaped_html = match.group(1).strip()
    return html.unescape(escaped_html)


def extract_visible_text(aspx_raw: str) -> str:
    """Convert raw .aspx content into cleaned visible text."""
    cleaned = _strip_page_directives(aspx_raw)
    canvas_html = _extract_canvas_via_regex(cleaned)
    html_to_parse = canvas_html or cleaned

    soup = BeautifulSoup(html_to_parse, "html.parser")

    for tag in soup([
        "script", "style", "noscript", "header", "footer", "nav", "aside", "svg"
    ]):
        tag.decompose()

    raw_text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in raw_text.splitlines()]
    return "\n".join(line for line in lines if line)
