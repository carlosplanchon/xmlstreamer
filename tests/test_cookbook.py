"""Every runnable COOKBOOK.md snippet is extracted and executed here, so
the recipes can never drift from the real API."""

import json
import logging
import re
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

COOKBOOK = Path(__file__).resolve().parent.parent / "COOKBOOK.md"

RECIPE_RE = re.compile(
    r"<!-- recipe: ([a-z-]+) -->\s*```python\n(.*?)```", re.DOTALL
)

RECIPES = {
    match.group(1): match.group(2)
    for match in RECIPE_RE.finditer(COOKBOOK.read_text(encoding="utf-8"))
}

EXPECTED_RECIPES = {
    "first-items",
    "bounded-memory",
    "survive-cut",
    "auth-feeds",
    "keep-recent",
    "real-lists",
    "sample-cheaply",
    "feed-to-jsonl",
    "production-logging",
    "time-boxed-runs",
    "parallel-feeds",
    "replay-captured-feed",
}


def run_recipe(slug: str, url: str, **extra) -> dict:
    """Execute one snippet exactly as a reader would, with `url` bound."""
    namespace = {"url": url, **extra}
    code = compile(RECIPES[slug], f"COOKBOOK.md:{slug}", "exec")
    exec(code, namespace)
    return namespace


def book_feed(books: list) -> bytes:
    parts = ['<?xml version="1.0"?>', "<catalog>"]
    for book in books:
        parts.append("<book>")
        for tag, text in book.items():
            parts.append(f"<{tag}>{text}</{tag}>")
        parts.append("</book>")
    parts.append("</catalog>")
    return "".join(parts).encode()


THREE_BOOKS = book_feed([
    {"id": "1", "title": "A History of Bridges"},
    {"id": "2", "title": "Field Guide to Rivers"},
    {"id": "3", "title": "The Cartographer"},
])

# Much bigger than one 64 KiB stream chunk, so mid-feed cuts and early
# breaks leave a live source behind.
BIG_FEED = book_feed(
    [{"id": str(i), "title": f"Book {i}", "pad": "x" * 80} for i in range(3000)]
)


def test_cookbook_has_exactly_the_documented_recipes():
    assert set(RECIPES) == EXPECTED_RECIPES


def test_first_items(http_server):
    url = http_server.serve("/cb1.xml", THREE_BOOKS)
    namespace = run_recipe("first-items", url)
    assert namespace["interpreter"].stats_delivered_items == 3


def test_bounded_memory(http_server):
    url = http_server.serve("/cb2.xml", THREE_BOOKS)
    namespace = run_recipe("bounded-memory", url)
    assert namespace["titles"] == [
        "A History of Bridges", "Field Guide to Rivers", "The Cartographer",
    ]


class _CutOnceHandler(BaseHTTPRequestHandler):
    """First GET: half the body, then the connection drops. After that:
    the full body. Exercises the retry recipe end to end."""

    def do_GET(self):
        body = self.server.feed_body
        self.server.hits += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.server.hits == 1:
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.connection.close()
            return
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.fixture()
def cut_once_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CutOnceHandler)
    server.feed_body = BIG_FEED
    server.hits = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield server, f"http://{host}:{port}/cut.xml"
    finally:
        server.shutdown()
        server.server_close()


def test_survive_cut(cut_once_url):
    server, url = cut_once_url
    namespace = run_recipe("survive-cut", url)
    books = namespace["books"]
    assert len(books) == 3000
    assert len({book["id"] for book in books}) == 3000
    assert server.hits >= 2  # the first attempt really was cut


def test_auth_feeds(http_server):
    url = http_server.serve("/cb4.xml", THREE_BOOKS)
    namespace = run_recipe("auth-feeds", url)
    assert len(namespace["books"]) == 3
    assert http_server.last_headers()["x-api-key"] == "my-key"


def test_keep_recent(http_server):
    fresh = (datetime.now() - timedelta(days=2)).isoformat()
    stale = (datetime.now() - timedelta(days=400)).isoformat()
    feed = book_feed([
        {"id": "1", "title": "Fresh", "added": fresh},
        {"id": "2", "title": "Stale", "added": stale},
        {"id": "3", "title": "Undated"},
    ])
    url = http_server.serve("/cb5.xml", feed)
    namespace = run_recipe("keep-recent", url)
    titles = [item["title"] for item in namespace["recent"]]
    assert titles == ["Fresh", "Undated"]


def test_real_lists(http_server):
    feed = (
        b'<?xml version="1.0"?><catalog>'
        b"<book><title>Duo</title><authors>"
        b"<author><name>A. First</name></author>"
        b"<author><name>B. Second</name></author>"
        b"</authors></book>"
        b"<book><title>Solo</title><authors>"
        b"<author><name>C. Third</name></author>"
        b"</authors></book>"
        b"</catalog>"
    )
    url = http_server.serve("/cb6.xml", feed)
    namespace = run_recipe("real-lists", url)
    assert namespace["names"] == ["A. First", "B. Second"]
    solo = namespace["books"][1]["authors"]["author"]
    assert isinstance(solo, list) and len(solo) == 1  # ALWAYS a list


def test_sample_cheaply(http_server):
    url = http_server.serve("/cb7.xml", BIG_FEED)
    namespace = run_recipe("sample-cheaply", url)
    assert len(namespace["sample"]) == 10
    generator = namespace["interpreter"].last_run.tokenizer.feed_generator
    assert generator.gi_frame is None  # source released by the with-block


def test_feed_to_jsonl(http_server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = http_server.serve("/cb8.xml", THREE_BOOKS)
    run_recipe("feed-to-jsonl", url)
    lines = (tmp_path / "books.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["title"] == "A History of Bridges"


def test_production_logging(http_server, caplog):
    feed = (
        b'<?xml version="1.0"?><catalog>'
        b"<book><title>Good One</title></book>"
        b"<book><title>Broken"  # never closes: discarded with a warning
        b"</book>"
        b"<book><title>Good Two</title></book>"
        b"</catalog>"
    )
    url = http_server.serve("/cb9.xml", feed)
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        namespace = run_recipe("production-logging", url)
    assert len(namespace["books"]) == 2
    assert "Discarding unparseable item" in caplog.text


def test_parallel_feeds(http_server):
    urls = [http_server.serve(f"/pf{i}.xml", THREE_BOOKS) for i in range(3)]
    namespace = run_recipe("parallel-feeds", urls[0], feed_urls=urls)
    assert namespace["results"] == {u: 3 for u in urls}


def test_time_boxed_runs(http_server):
    url = http_server.serve("/cb10.xml", THREE_BOOKS)
    namespace = run_recipe("time-boxed-runs", url)
    assert len(namespace["books"]) == 3
    assert namespace["interpreter"].stats_delivered_items == 3


def test_replay_captured_feed(tmp_path):
    # The one recipe with no server at all: the feed is a file on disk.
    saved = tmp_path / "captured.xml"
    saved.write_bytes(THREE_BOOKS)
    namespace = run_recipe(
        "replay-captured-feed", "http://unused.invalid/", path=str(saved)
    )
    assert [book["title"] for book in namespace["books"]] == [
        "A History of Bridges", "Field Guide to Rivers", "The Cartographer",
    ]
