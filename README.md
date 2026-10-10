![xmlstreamer: continuous feed / discrete output](https://raw.githubusercontent.com/carlosplanchon/xmlstreamer/main/assets/banner.png)

# xmlstreamer

[![PyPI version](https://img.shields.io/pypi/v/xmlstreamer.svg)](https://pypi.org/project/xmlstreamer/)
[![Python versions](https://img.shields.io/pypi/pyversions/xmlstreamer.svg)](https://pypi.org/project/xmlstreamer/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/carlosplanchon/xmlstreamer)

**Stream huge, messy XML feeds into Python records - without loading the entire document into memory or hiding partial failures.**

**xmlstreamer** is a Python library designed for efficient, memory-friendly streaming and parsing of large XML feeds from various sources, including compressed formats. It supports decompression, character encoding detection, tag-based itemization, and optional item filtering, making it ideal for handling real-time XML data feeds, large datasets, and complex structured content.

## Why xmlstreamer?

Large XML feeds retrieved from remote systems can be malformed, incorrectly encoded, or interrupted in transit. xmlstreamer processes them incrementally and isolates failures when corruption is contained within an item, while reporting partial failures explicitly.

With a strict parser, one broken record invalidates the entire document. With an aggressive recovery parser, malformed input may be silently rewritten into data that was never actually there. xmlstreamer is built for the space between those two behaviors:

- process large XML documents without loading them entirely into memory;
- deliver every intact item, before and after localized corruption;
- never repair damaged XML into records that were not in the feed (with one judgement call that cannot be made for you - markup that opens and never closes - left in your hands and documented below);
- expose partial failures explicitly instead of presenting incomplete processing as success.

It is intended for XML ingestion, archival processing, ETL and migration workflows where bounded memory, conservative recovery and explicit failure semantics matter.

### When not to use it

- If your XML is trusted, consistently well-formed and raw speed is the primary concern, `lxml.etree.iterparse` will usually be faster and simpler - the benchmark below says exactly that.
- If your data lives in XML attributes (`<book id="123" year="1998"/>`), this library will not extract it: items are built from element text only.
- If your separator tag legitimately nests inside itself, that is outside this library's documented contract.

xmlstreamer is for large or unreliable inputs where conservative recovery, bounded memory and explicit partial-failure semantics matter more than maximum parser throughput.

## Features

- **Streamed Data Parsing**: Parses XML data directly from HTTP, HTTPS, or FTP sources - or from a local file or any binary stream (`source=`) - without loading the entire file into memory.
- **GZIP Decompression Support**: Seamlessly handles GZIP-compressed XML feeds, including multi-member (concatenated) gzip.
- **Encoding Detection**: Automatically detects character encodings on both plain and GZIP streams (UTF-16, Latin-1, etc.) and transcodes them to UTF-8 on the fly. Valid UTF-8 input is never altered. Detection reads the head of the feed, so bytes that contradict it much later cannot be transcoded: those are replaced with `U+FFFD`, warned once per run and counted in the closing stats.
- **Customizable Item Tokenization**: Uses a byte-level tokenizer to isolate items and parses each item independently with Expat. Separator tags inside comments, CDATA, processing instructions or DOCTYPE declarations are ignored, so markup can never open or close an item - nor invent one. Attribute values may contain `>` (only `<` and `&` are illegal there) without confusing the scan, and buffer or chunk sizes never change what comes out. One contract: the separator tag must not nest inside itself (no `<item>` containing another `<item>`). One knob for the one case with no universal answer, a section that never closes: `Sections(on_limit=...)` chooses between reading it as content past `max_size` (the default: bounded damage) and discarding it whatever follows (never a record, at the price of the rest of the feed).
- **Configurable Runtime and Buffering**: Includes configurable buffer sizes and runtime limits, allowing you to tailor performance to your application's needs. `buffer_size` is the initial read buffer, not a size limit: items larger than the buffer are still parsed (the buffer grows as needed to fit the current item). `max_running_time` is a per-run budget measured on a monotonic clock, so an NTP step or a daylight saving shift can never cut a run short. It covers the whole run, acquisition included: the download, the encoding sample and the scan all check it. What it cannot do is interrupt a read or a parse already in flight, so a run can overshoot by one read (up to 64 KiB of network, however long that takes to arrive) or by one item's parse.
- **Download Timeouts**: `Transport(timeout=(connect, read))` in seconds, default `(30, 300)`. The read timeout is the maximum silence between bytes on the streamed download, not a total duration, so huge-but-flowing feeds are never cut while stalled connections fail fast. Pass `Transport(timeout=None)` to disable.
- **Authentication and Proxies**: Basic, Digest, Bearer and API-key-header authentication, plus requests-style proxy support.
- **Two Download Modes**: spool to a temporary file (default) or stream straight from the network, with explicit error semantics (`FeedInterruptedError`) and deterministic resource release via `close()` / context manager.
- **Flexible Filtering**: pass any callable as `item_filter`; it receives each flat item dict and its truthy/falsy return keeps or drops the item. Note that items are built from element text only: XML attributes (like `id` in `<book id="123">`) are not extracted.
- **Typed**: ships PEP 561 type hints (`py.typed`), so type checkers and editors see the whole public API.
- **Property-tested**: beyond the example-based suite, generative tests (Hypothesis) verify the core invariants over thousands of generated feeds - item isolation under corruption, chunking invariance, and crash-freedom on arbitrary bytes.

## Installation

Install with uv or pip:

```bash
uv add xmlstreamer        # or: pip install xmlstreamer
```

Requires Python 3.10 or newer.

## Usage

```python
from xmlstreamer import StreamInterpreter

interpreter = StreamInterpreter(
    url="https://example.com/large-feed.xml",
    separator_tag="item",
)

for item in interpreter:
    print(item)  # each item is a flat {path: text} dict
```

That is the whole program: gzip, character encodings and malformed items are handled by default, and the feed is never loaded whole into memory. Tuning knobs (`buffer_size`, `max_running_time`, `Transport`, filters, nested output) exist when you need them and are documented below.

The interpreter is reusable configuration; iterating it opens a run (`FeedRun`) that owns one pass over the feed. Runs are independent, so nested loops and retries over the same interpreter are ordinary code, and the interpreter answers for the last one through `last_run` and the `stats_*` properties.

Local data uses the same pipeline: pass `source=` instead of `url=` - a filesystem path or any binary file-like object (`io.BytesIO(...)` in a unit test, `sys.stdin.buffer` in a pipe). Gzip and encoding detection still apply, and no temporary file is involved. A path can be iterated again like a URL (each run reopens it); a file-like object is consumed by its first run, and a second run raises instead of yielding zero items.

## Command line

The package doubles as a diagnostic tool: stream any feed to JSON Lines without writing code.

```bash
uvx xmlstreamer https://example.com/large-feed.xml --tag item --limit 5
```

The positional argument is a URL, a local file path, or `-` for stdin, so the same tool replays a saved feed and composes in shell pipelines:

```bash
xmlstreamer saved-feed.xml --tag item
curl -s https://example.com/large-feed.xml | xmlstreamer - --tag item
```

One JSON object per line on stdout, ready to pipe into `jq`, a file, or whatever sits downstream. In [Nushell](https://www.nushell.sh/) that stream becomes a native table in one pipeline - type the fields, filter, group and export from there (values arrive as strings, so promote what you need):

```nushell
xmlstreamer https://example.com/large-feed.xml --tag item
| from json --objects
| update price {|r| $r.price | into float}
| where price < 10
| to csv
```

The useful flags mirror the API: `--mode stream` (no temp file), `--nested` (nested dicts), `--raw` (each item's bytes instead of JSON; add `--print0` for NUL-delimited framing), `--limit N` (sample cheaply and release the source), `--max-seconds S` (time budget) and `-v` (progress and stats on stderr). Errors exit non-zero with a one-line message, and a mid-feed cut in stream mode reports how many items were delivered before dying.

## Cookbook

[`COOKBOOK.md`](COOKBOOK.md) has task-oriented recipes: surviving mid-feed connection cuts without duplicating work, feeds bigger than RAM, cheap sampling, JSONL pipelines, production logging, processing many feeds in parallel, and more. Every snippet in it is extracted and executed by the test suite against a local feed server, so the recipes cannot drift from the real API.

## Performance

Honest numbers against the obvious alternatives. Everything below comes from the reproducible harness in [`benchmarks/`](benchmarks/README.md), and a single command re-measures all of it on your machine:

```bash
uv run --group bench python benchmarks/run_all.py
```

*Measured 2026-08-08 on an i5-1135G7, Python 3.14.6, lxml 6.1.1, xmltodict 1.0.4. Absolute numbers track the machine; the ratios are stable across runs.*

**Speed.** Parse-only throughput: every parser consumes the same in-memory bytes, and each cell keeps the best of 7 interleaved rounds, so CPU frequency drift hits everyone equally.

| feed | xmlstreamer | lxml iterparse (recover=True) | stdlib ET iterparse | xmltodict |
|---|---|---|---|---|
| flat items | 90k items/s | 251k (2.8x faster) | 204k (2.3x faster) | 60k (slower) |
| CDATA-heavy | 61k items/s | 111k (1.8x faster) | 124k (2.0x faster) | 51k (slower) |

lxml is the speed king. If your feeds are always well-formed, correctly declared and never truncated, use lxml. The next two tables are about what happens when they are not.

**Peak memory.** Each parser processes the same 121 MB feed alone, in a fresh subprocess, and reports its peak RSS: the highest amount of physical RAM the process ever occupied while running - measured by the kernel, not estimated.

| parser | peak RSS |
|---|---|
| xmlstreamer | 31 MB - flat: identical on a 5 MB and a 121 MB feed |
| lxml iterparse | 25 MB |
| stdlib ET iterparse | 18 MB |
| xmltodict (streaming mode) | 23 MB |
| xmltodict (as commonly used) | 444 MB - grows with the feed |

Encoding detection only runs when the first 64 KiB are not already valid UTF-8, so the common case never pays for it; legacy-encoded feeds add a transient detection peak at startup. Either way the number does not grow with the feed - whole-document parsing (the last row) does.

**Broken feeds.** This table is the reason the library exists: what each parser does when the input is damaged. The cells summarize verdicts generated by code, not written by hand - run the harness for the full, unabridged table.

| input damage | xmlstreamer | lxml (recover=True) | stdlib ET / xmltodict |
|---|---|---|---|
| unclosed tag in one item | keeps the healthy items, warns | fabricates an item that does not exist | whole feed lost (exception) |
| raw `&` or `<` in item text | drops that item with a warning | silently deletes text around it | whole feed lost |
| encoding declaration lies | decodes correctly (content wins over the declaration) | silent mojibake | silent mojibake or feed lost |
| download truncated mid-item | delivers intact items only | emits the truncated fragment as a real item | feed lost |
| separator tag nested in itself | unsupported (documented contract) | parses it | parses it |

lxml and the stdlib win the first table by 2-3x; xmlstreamer is the only column with no silent failure in the last one. That trade is the point of the library: tens of thousands of items per second is more than a feed pipeline typically needs, and damaged input is never repaired into records that were not in the feed. What the library cannot decode or cannot read as markup is reported, never passed off as data.

## Transport: authentication, proxies, timeouts

Everything about how the feed is reached lives in a single `Transport` object: user agent, auth, requests-style proxy dict, timeouts and download mode. Omit it entirely and the defaults apply.

```python
from xmlstreamer import BearerAuth, StreamInterpreter, Transport

interpreter = StreamInterpreter(
    url=url,
    separator_tag="book",
    transport=Transport(
        auth=BearerAuth(token="..."),
        proxy={"http": "http://proxy:8080", "https": "http://proxy:8080"},
    ),
)
```

- `BasicAuth(username, password)` / `DigestAuth(username, password)`: HTTP(S) basic and digest auth. On FTP URLs the username and password are used for login.
- `BearerAuth(token)`: sends `Authorization: Bearer <token>`.
- `ApiKeyAuth(header, value)`: sends the key in a custom header, e.g. `ApiKeyAuth(header="X-Api-Key", value="...")`.

## Download modes

By default the feed is spooled to a temporary file before parsing (`Transport(download_mode="temp")`): network errors surface before the first item, and a failed download yields nothing. With `Transport(download_mode="stream")` the bytes flow straight from the network to the parser - no temp file (nothing in TMPDIR), memory bounded by the largest single item (or the largest tag or markup section, whichever is bigger, the latter capped by `Sections(max_size=...)`) plus fixed buffers, rather than by the feed size, and first items before the download finishes - in exchange for a different error contract: a connection lost mid-feed raises `FeedInterruptedError` from the iteration, carrying `items_delivered` and the original network error chained as `__cause__`. Failures before the first item (connecting, HTTP errors, the initial encoding-detection read) keep their raw exception type in both modes.

```python
from xmlstreamer import FeedInterruptedError, StreamInterpreter, Transport

interpreter = StreamInterpreter(
    url=url, separator_tag="book", transport=Transport(download_mode="stream")
)
try:
    for item in interpreter:
        process(item)
except FeedInterruptedError as exc:
    log.warning("feed cut after %s items: %s", exc.items_delivered, exc.__cause__)
```

Stopping early (a `break`) keeps the source open until the interpreter is garbage collected. Call `interpreter.close()` (or use the interpreter as a context manager) to release it right away - the socket in stream mode, the temporary file in temp mode. Closing ends the run: it logs its stats, calls `run_finished(run)` and delivers nothing more, so what was left in its buffer never leaks out afterwards:

```python
with StreamInterpreter(
    url=url, separator_tag="book", transport=Transport(download_mode="stream")
) as interpreter:
    for item in interpreter:
        if enough(item):
            break
```

## Errors

Every failure of a feed or of the network xmlstreamer raises on its own descends from `XMLStreamerError`, so a single `except` clause covers the library:

- `FeedInterruptedError`: stream mode only, the connection died mid-feed. Carries `items_delivered` and the original network error as `__cause__`.
- `UnsupportedSchemeError` (also a `ValueError`): the URL scheme is not http, https or ftp. The message names the scheme, never the URL, which can carry credentials.

Network errors are not wrapped while the connection is being established: a 404 is `requests.exceptions.HTTPError` and a stalled server is `requests.exceptions.Timeout`, in both download modes. A `source=` path that does not open raises its raw `OSError` (`FileNotFoundError` and friends) at the same point. In stream mode, network errors after that point surface as `FeedInterruptedError` instead. Malformed items are never raised: they are logged at `WARNING` and skipped.

Programming mistakes stay plain Python and are deliberately outside that family, because they are not something a caller handles at runtime: bad arguments are `TypeError` / `ValueError` at the constructor, and opening a run while `close()` is releasing everything is a `RuntimeError`.

## Nested output (opt-in)

Items are flat `{path: text}` dicts by default (`item["publisher/city"]`), which is the stable, primitive form. An item made of bare text rather than fields (`<isbn>978-0-1</isbn>` with `separator_tag="isbn"`) comes out keyed by that same tag: `{"isbn": "978-0-1"}`. If you prefer nested dicts, pass `output=Nested()` (or call `xmlstreamer.to_nested(item)` on any flat item):

```python
from xmlstreamer import Nested, StreamInterpreter

interpreter = StreamInterpreter(url=url, separator_tag="book", output=Nested())
for item in interpreter:
    print(item["publisher"]["city"])
```

Numbered siblings keep their numbering by default (`item["authors"]["author_1"]` - never implicit lists, so the shape cannot depend on how many siblings an item happened to have), mixed-content text lands under `"#text"`, and item filters always operate on the flat form.

To get real lists, declare which paths are collections with `Nested(force_list=...)`. Declared elements are ALWAYS lists, in document order, with one element or many. `Nested(force_list=True)` declares every path: fully uniform output where every element is a list, in the strict XML-to-JSON style. The declaration is validated and frozen when `Nested` is built, so a one-shot iterable cannot reshape later items:

```python
interpreter = StreamInterpreter(
    url=url,
    separator_tag="book",
    output=Nested(force_list={"authors/author"}),
)
for item in interpreter:
    for author in item["authors"]["author"]:
        print(author["name"])
```

## Raw output (opt-in)

`output=Raw()` delivers each item as bytes instead of a dict: the slice between the item's separator tags, exactly as the parser would receive it - after transparent gunzip, after stream-level normalization to UTF-8, before any per-item repair, and never re-serialized. Selection does not change - an item that does not parse is still dropped with a warning, and `item_filter` still sees the flat dict - only the representation does. The separator tags themselves (and any attributes on them) are not part of the slice.

```python
from xmlstreamer import Raw, StreamInterpreter

interpreter = StreamInterpreter(
    source="saved-feed.xml", separator_tag="book", output=Raw()
)
for item_bytes in interpreter:
    ...  # b"<title>The Cartographer</title><year>2011</year>"
```

Two uses stand out: splitting a huge feed into per-item work units without re-serializing them, and forensics - item-level `U+FFFD` replacement can only happen when the stream needed no transcoding, so in exactly that case the parsed dict carries the replacement while the raw bytes keep the stream's original bytes. On the command line this is `--raw`; items are newline-delimited by default (readable, but an item may itself span lines), and `--print0` switches to NUL - which cannot appear in XML, so that framing is unambiguous.

Type checkers follow the mode: with `output=Raw()` the interpreter is a `StreamInterpreter[bytes]`, with `Nested()` or the default it is a `StreamInterpreter[dict[str, Any]]` - so existing typed code keeps checking clean after upgrading.

## Markup that never closes

Comments, CDATA, processing instructions and DOCTYPE declarations are skipped whole: a separator tag written inside one is markup, not an item. What happens when one of them never closes is the single question this library will not answer for you, because both answers are defensible and they fail in opposite directions:

```python
from xmlstreamer import Sections, StreamInterpreter

interpreter = StreamInterpreter(
    url=url,
    separator_tag="book",
    sections=Sections(max_size=16 * 1024 * 1024, on_limit="text"),
)
```

`on_limit="text"` (the default) concludes that an opener still unterminated after `max_size` bytes was never markup, and re-reads it as content. That is what a stray `<?` in a description usually is - pasted code, or the `<?xml:namespace ...>` a word processor left behind - and it keeps every item after it. The cost: a genuine section larger than `max_size` would be read as content, and records could come out of it.

`on_limit="markup"` keeps discarding instead, in constant memory. No record can ever come from inside a section, and the price is everything that follows an opener that never closes.

Either way the decision is announced with a WARNING, and it is measured from the opener over the stream, so buffer and chunk sizes never change it. At end of feed neither policy re-reads: a cut download is a cut download, and re-reading a truncated comment full of commented-out items would invent them.

## Logging

xmlstreamer logs through the standard `logging` module under the `xmlstreamer` logger name (per-feed progress and encoding detection at `DEBUG`, end-of-feed stats at `INFO`, everything lossy at `WARNING`). The `WARNING` level is where the library reports what it could not keep: discarded malformed items, corrupt or truncated gzip, a feed that ends inside an item, a section that never terminates, a key collision that reshaped an item, and invalid UTF-8 replaced with `U+FFFD` (warned once per run, then counted in the closing stats line). Nothing is dropped or altered without a line in the log. To see its output:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## Filtering

Pass any callable as `item_filter`: it receives each parsed item as the flat `{path: text}` dict, and its return value decides - truthy keeps the item, falsy drops it. Filters run before the optional `Nested` conversion (they always see flat keys) and may mutate the dict in place. Three per-run counters form a funnel, `stats_total_items >= stats_parsed_items >= stats_delivered_items`, and the gaps have different owners: total minus parsed is items broken in the *feed* (dropped with a WARNING), parsed minus delivered is items dropped by *your* filter. (The example uses the third-party `dateparser` package; `datetime.fromisoformat` covers well-formed dates with the stdlib.)

```python
from datetime import datetime, timedelta

import dateparser

from xmlstreamer import StreamInterpreter

def keep_recent(item: dict) -> bool:
    parsed = dateparser.parse(item.get("pubDate", ""))
    if parsed is None:
        return True  # unparseable or missing date: keep the item
    return parsed > datetime.now() - timedelta(days=7)

interpreter = StreamInterpreter(
    url=url,
    separator_tag="item",
    item_filter=keep_recent,
)

for item in interpreter:
    print(item)  # only items from the last 7 days
```

For parameterized filters, close over the config (or use `functools.partial`):

```python
import functools

def keep_recent(days: int, item: dict) -> bool:
    parsed = dateparser.parse(item.get("pubDate", ""))
    return parsed is None or parsed > datetime.now() - timedelta(days=days)

interpreter = StreamInterpreter(
    url=url,
    separator_tag="item",
    item_filter=functools.partial(keep_recent, 30),
)
```

Filters chain with plain Python - `all()` short-circuits like a pipeline
of predicates, and the order is the order of the list:

```python
FILTERS = [has_isbn, keep_recent, in_stock]

item_filter=lambda item: all(f(item) for f in FILTERS)
```

**If your filter raises, the exception propagates raw and the run stops
there.** This is deliberate, and the mirror image of how broken items are
treated: damage in the *data* (a malformed item) is dropped with a
WARNING and the run continues, but a bug in *your* code is never
absorbed - the library will not silently turn a filter typo into kept or
dropped items, and the traceback points at your line. (This contract is
pinned by a test.) If you want a different policy for a flaky filter,
wrap it - the choice of what a failing filter means belongs to you:

```python
def tolerant(f, on_error=False):
    def wrapped(item):
        try:
            return f(item)
        except Exception:
            logging.warning("filter failed on item: %r", item)
            return on_error  # False drops the item, True keeps it.
    return wrapped

item_filter=tolerant(keep_recent)
```

## Subclassing for alerts

`run_finished(run)` is called once when a run ends, however it ends: the feed ended, its time budget ran out, or the caller closed it. It runs after that run released its source and logged its stats, and it receives the run that ended, so its counters are the right ones even if another run is already open. Nothing needs to be raised or chained, and nothing should: the hook reports, so an exception it raises is logged at `ERROR` with its traceback and goes no further - a failed alert never undoes items already delivered:

```python
from xmlstreamer import StreamInterpreter

class AlertingStreamInterpreter(StreamInterpreter):
    def run_finished(self, run):
        if run.stats_parsed_items == 0:
            print("--- ZERO ITEMS ALERT ---")
```

## Contributing and security

Bug reports with a reproducible broken feed are the most valuable contribution this project can receive - [`CONTRIBUTING.md`](CONTRIBUTING.md) explains how to file one safely and what pull requests need (including the DCO sign-off). For vulnerabilities, never open a public issue: use the private channel described in [`SECURITY.md`](SECURITY.md).

## Technical Acknowledgments

The shape of this library - a byte-level tokenizer feeding a per-item interpreter - descends from Ruslan Spivak's [Let's Build A Simple Interpreter](https://ruslanspivak.com/lsbasi-part1/) series, which taught its author to think in lexers and interpreters. If you want to understand this codebase deeply, that series is the best prerequisite there is.
