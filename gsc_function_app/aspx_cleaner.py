import html
import re

from bs4 import BeautifulSoup


PAGE_DIRECTIVE_RE = re.compile(r"^\s*<%@.*?%>\s*", flags=re.MULTILINE)
CANVAS_CONTENT_RE = re.compile(
    r"<mso:CanvasContent1[^>]*>(.*?)</mso:CanvasContent1>",
    flags=re.IGNORECASE | re.DOTALL,
)
REMOVABLE_TAGS = [
    "script",
    "style",
    "noscript",
    "header",
    "footer",
    "nav",
    "aside",
    "svg",
]


def strip_page_directives(raw: str) -> str:
    return PAGE_DIRECTIVE_RE.sub("", raw)


def extract_canvas_content(raw: str) -> str:
    match = CANVAS_CONTENT_RE.search(raw)
    if not match:
        return ""
    return html.unescape(match.group(1).strip())


def normalize_visible_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_visible_text(aspx_raw: str) -> str:
    cleaned = strip_page_directives(aspx_raw)
    canvas_html = extract_canvas_content(cleaned)
    html_to_parse = canvas_html or cleaned

    soup = BeautifulSoup(html_to_parse, "html.parser")
    for tag in soup(REMOVABLE_TAGS):
        tag.decompose()

    return normalize_visible_text(soup.get_text(separator="\n"))
