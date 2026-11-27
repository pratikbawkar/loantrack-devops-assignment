"""Static checks on the frontend (plain HTML/CSS/JS, served by nginx).

Guards the rule that the frontend never touches the database: no drivers,
connection strings, credentials or hardcoded backend addresses; it only calls
the relative /api/loans path. Needs the repository's frontend/ directory next
to backend/ (run the tests with the repository root available).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
INDEX = FRONTEND / "index.html"
CSS = FRONTEND / "css" / "styles.css"
JS = FRONTEND / "js" / "app.js"
REQUIRED_FILES = [INDEX, CSS, JS]
NGINX_CONF = FRONTEND / "nginx.conf"

# Files the browser receives. The container build files next to them are
# infrastructure: they are allowed, but never served to the browser.
STATIC_SUFFIXES = {".html", ".css", ".js"}
CONTAINER_FILES = {"Dockerfile", ".dockerignore", "nginx.conf"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_frontend_files() -> list[Path]:
    return [p for p in FRONTEND.rglob("*") if p.is_file()]


def _static_assets() -> list[Path]:
    return [p for p in _all_frontend_files() if p.suffix in STATIC_SUFFIXES]


def _strip_js_comments(source: str) -> str:
    # Comments may name the unsafe APIs they avoid; only executable code is checked.
    # Safe here because the frontend contains no URLs (enforced by another test).
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(^|\s)//[^\n]*", r"\1", without_blocks)


def _function_body(source: str, name: str) -> str:
    """Return the {...} body of `function name(` / `async function name(` (brace matched)."""
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", source)
    assert match, f"function {name} not found in app.js"
    start = source.index("{", match.end())
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"unbalanced braces in function {name}")


class _Element:
    def __init__(self, tag: str, attrs: dict[str, str | None], ancestors: list[str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.ancestors = ancestors  # tags/ids of enclosing elements


class _Collector(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[_Element] = []
        self._stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        self.elements.append(_Element(tag, attr_map, list(self._stack)))
        if tag not in self.VOID:
            label = f"{tag}#{attr_map['id']}" if attr_map.get("id") else tag
            self._stack.append(label)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i].split("#")[0] == tag:
                del self._stack[i:]
                break


@pytest.fixture(scope="module")
def html() -> list[_Element]:
    parser = _Collector()
    parser.feed(_read(INDEX))
    return parser.elements


@pytest.fixture(scope="module")
def js() -> str:
    return _read(JS)


def _by_id(elements: list[_Element], element_id: str) -> _Element:
    found = [e for e in elements if e.attrs.get("id") == element_id]
    assert len(found) == 1, f"expected exactly one element with id={element_id!r}"
    return found[0]


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def test_frontend_directory_exists():
    assert FRONTEND.is_dir(), f"frontend directory not found at {FRONTEND}"


@pytest.mark.parametrize("path", REQUIRED_FILES, ids=lambda p: p.relative_to(FRONTEND).as_posix())
def test_required_file_exists_and_not_empty(path):
    assert path.is_file()
    assert path.stat().st_size > 0


def test_frontend_is_static_only_without_dependencies():
    for path in _all_frontend_files():
        relative = path.relative_to(FRONTEND)
        is_container_file = len(relative.parts) == 1 and path.name in CONTAINER_FILES
        assert path.suffix in STATIC_SUFFIXES or is_container_file, f"unexpected file in static frontend: {relative}"
    for name in ("package.json", "node_modules", "requirements.txt"):
        assert not any(name in p.parts or p.name == name for p in FRONTEND.rglob("*")), name


# ---------------------------------------------------------------------------
# HTML structure
# ---------------------------------------------------------------------------


def test_page_loads_local_stylesheet_and_script(html):
    stylesheets = [e.attrs.get("href") for e in html if e.tag == "link" and e.attrs.get("rel") == "stylesheet"]
    scripts = [e.attrs.get("src") for e in html if e.tag == "script"]
    assert stylesheets == ["css/styles.css"]
    assert scripts == ["js/app.js"]


def test_no_external_resources(html):
    for element in html:
        for attr in ("src", "href", "action"):
            value = element.attrs.get(attr)
            if value:
                assert not re.match(r"^(?:[a-z]+:)?//", value, re.I), f"external {attr}={value!r}"


def test_add_loan_form_has_the_three_fields_and_no_status(html):
    form = _by_id(html, "loan-form")
    assert form.tag == "form"
    inside = [e for e in html if "form#loan-form" in e.ancestors]
    names = {e.attrs.get("name") for e in inside if e.tag in {"input", "select", "textarea"}}
    assert names == {"borrower_name", "loan_amount", "property_city"}
    assert "status" not in names
    assert any(e.tag == "button" and e.attrs.get("type") == "submit" for e in inside)
    # Every field has a visible label.
    labels = {e.attrs.get("for") for e in html if e.tag == "label"}
    assert {"borrower_name", "loan_amount", "property_city"} <= labels


def test_loans_table_has_expected_columns(html):
    table = _by_id(html, "loans-table")
    assert table.tag == "table"
    headers = [e for e in html if e.tag == "th" and "table#loans-table" in e.ancestors]
    assert len(headers) == 6
    assert _by_id(html, "loans-body").tag == "tbody"


@pytest.mark.parametrize("element_id", ["loans-loading", "loans-empty", "loans-error", "form-message"])
def test_loading_empty_error_and_success_state_elements_exist(html, element_id):
    _by_id(html, element_id)


def test_footer_shows_serving_pod_and_version(html):
    for element_id in ("served-by", "app-version"):
        element = _by_id(html, element_id)
        assert "footer" in element.ancestors, f"{element_id} must be inside the footer"


# ---------------------------------------------------------------------------
# JavaScript behaviour
# ---------------------------------------------------------------------------


def test_api_path_is_relative_api_loans(js):
    assert re.search(r'const\s+API_LOANS\s*=\s*"/api/loans"\s*;', js)


def test_every_fetch_uses_the_relative_api_constant(js):
    targets = re.findall(r"fetch\(\s*([^,)\s]+)", js)
    assert len(targets) == 2, f"expected one GET and one POST fetch, found {targets}"
    assert set(targets) == {"API_LOANS"}


def test_get_lists_loans(js):
    body = _function_body(js, "loadLoans")
    assert "fetch(API_LOANS" in body
    assert "method" not in body  # default method is GET
    assert "response.status === 503" in body
    assert "renderLoans(" in body


def test_post_creates_loan_and_handles_201_422_503(js):
    body = _function_body(js, "onSubmit")
    assert "fetch(API_LOANS" in body
    assert re.search(r'method:\s*"POST"', body)
    assert "JSON.stringify(payload)" in body
    for status in (201, 422, 503):
        assert f"response.status === {status}" in body, f"HTTP {status} not handled"


def test_table_reloads_after_successful_creation(js):
    body = _function_body(js, "onSubmit")
    created_block = body.split("response.status === 201", 1)[1].split("response.status === 422", 1)[0]
    assert "loadLoans()" in created_block


def test_payload_never_contains_status(js):
    body = _function_body(js, "buildPayload")
    assert "status" not in body.lower()
    keys = set(re.findall(r"(\w+):", body))
    assert keys == {"borrower_name", "loan_amount", "property_city"}


def test_footer_reads_both_response_headers(js):
    body = _function_body(js, "updateFooter")
    assert 'headers.get("X-Served-By")' in body
    assert 'headers.get("X-App-Version")' in body
    assert "ui.servedBy.textContent" in body
    assert "ui.appVersion.textContent" in body
    # Called after both the GET and the POST.
    assert "updateFooter(response)" in _function_body(js, "loadLoans")
    assert "updateFooter(response)" in _function_body(js, "onSubmit")


@pytest.mark.parametrize(
    "unsafe", ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]
)
def test_values_are_rendered_with_safe_dom_apis(js, unsafe):
    code = _strip_js_comments(js)
    assert unsafe not in code
    assert "textContent" in code


def test_comment_stripping_does_not_hide_real_code():
    sample = 'a.innerHTML = x; // note\n/* block */ b.textContent = y;'
    stripped = _strip_js_comments(sample)
    assert "innerHTML" in stripped and "textContent" in stripped
    assert "note" not in stripped and "block" not in stripped


# ---------------------------------------------------------------------------
# Security: no database access, credentials or hardcoded backend addresses
# ---------------------------------------------------------------------------

FORBIDDEN_TERMS = [
    "psycopg",
    "postgres",  # also matches postgresql:// connection strings
    "database_url",
    "db_host",
    "db_port",
    "db_password",
    "password",
    "secret",
    "credential",
    "pg_",
    "jdbc",
    "mysql",
    "sqlite",
    "mongodb",
    "connectionstring",
    "require(",
    "import ",
]


@pytest.mark.parametrize("term", FORBIDDEN_TERMS)
def test_no_database_drivers_or_credentials(term):
    for path in _all_frontend_files():
        assert term not in _read(path).lower(), f"{term!r} found in {path.relative_to(FRONTEND)}"


HARDCODED_ADDRESS_PATTERNS = [
    r"https?://",
    r"wss?://",
    r"\blocalhost\b",
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b",  # any IPv4 address
    r"\bbackend\s*:",  # compose/k8s service name with a port
    r":(?:8000|8080|5432|5433)\b",
    r"host\.docker\.internal",
    r"svc\.cluster\.local",
]


@pytest.mark.parametrize("pattern", HARDCODED_ADDRESS_PATTERNS)
def test_no_hardcoded_backend_addresses(pattern):
    # Browser code only: the nginx config legitimately names the backend.
    assets = _static_assets()
    assert len(assets) >= 3
    for path in assets:
        match = re.search(pattern, _read(path), re.IGNORECASE)
        assert match is None, f"{pattern!r} matched {match.group(0)!r} in {path.relative_to(FRONTEND)}"


# ---------------------------------------------------------------------------
# nginx: proxies /api/ to the backend only, answers /healthz itself
# ---------------------------------------------------------------------------


def test_nginx_proxies_api_to_backend_and_strips_prefix():
    conf = _read(NGINX_CONF)
    assert re.search(r"location /api/ \{", conf)
    assert "rewrite ^/api/(.*)$ /$1 break;" in conf
    upstreams = re.findall(r"^\s*set \$backend_upstream (\S+);", conf, re.M)
    assert upstreams == ["http://${BACKEND_HOST}:${BACKEND_PORT}"]
    assert len(re.findall(r"^\s*proxy_pass ", conf, re.M)) == 1


def test_nginx_healthz_is_local_not_proxied():
    conf = _read(NGINX_CONF)
    block = re.search(r"location = /healthz \{(.*?)\}", conf, re.S)
    assert block, "nginx must answer /healthz itself"
    assert "return 200" in block.group(1)
    assert "proxy_pass" not in block.group(1)


def test_nginx_listens_on_unprivileged_port():
    assert re.search(r"^\s*listen 8080;", _read(NGINX_CONF), re.M)
