# xmlstreamer cookbook

Task-oriented recipes. Every runnable snippet below is extracted from this
file and executed by the test suite against a local feed server - if a
recipe stops working, the test suite fails.

Convention: snippets assume a variable `url` pointing at your feed. Replace
it with your own. The example feeds are book catalogs with a `<book>`
separator tag.

## 1. First items from a feed

The minimum program is one object and one loop. Items arrive as flat
`{path: text}` dicts. By default the feed is spooled to a temporary file
first; recipe 2 shows how to parse while it downloads.

<!-- recipe: first-items -->
```python
from xmlstreamer import StreamInterpreter

interpreter = StreamInterpreter(url=url, separator_tag="book")
for item in interpreter:
    print(item["title"])
```

Gzip, character encodings and malformed items are handled without any
configuration: see "Things you do not need a recipe for" at the bottom.

## 2. A feed bigger than your RAM

The default (`download_mode="temp"`) spools the feed to a temporary file
and parses from there, so nothing is delivered until the download ends.
`download_mode="stream"` parses bytes as they arrive from the network
instead: no temporary file, first items before the download finishes, and
memory bounded by the largest single item (or the largest tag or markup
section, whichever is bigger) plus fixed buffers, rather than by the feed
size. The trade: a connection lost mid-feed raises `FeedInterruptedError`
from the loop (the default temp mode fails before the first item
instead).

<!-- recipe: bounded-memory -->
```python
from xmlstreamer import FeedInterruptedError, StreamInterpreter, Transport

interpreter = StreamInterpreter(
    url=url,
    separator_tag="book",
    transport=Transport(download_mode="stream"),
)
titles = []
try:
    for item in interpreter:
        titles.append(item["title"])
except FeedInterruptedError as exc:
    print(f"connection lost after {exc.items_delivered} items")
```

## 3. Survive a connection cut without duplicating work

Sooner or later a remote server will drop the connection mid-feed. The
pattern that survives it: retry the whole feed, keep a set of the ids
you already processed, and skip those on the next pass. Items are never
handled twice, and one flaky attempt does not cost you the healthy part.

<!-- recipe: survive-cut -->
```python
from xmlstreamer import FeedInterruptedError, StreamInterpreter, Transport

def fetch_all(url, attempts=3):
    seen_ids = set()
    books = []
    for attempt in range(attempts):
        interpreter = StreamInterpreter(
            url=url,
            separator_tag="book",
            transport=Transport(download_mode="stream"),
        )
        try:
            for item in interpreter:
                if item["id"] not in seen_ids:
                    seen_ids.add(item["id"])
                    books.append(item)
            return books
        except FeedInterruptedError as exc:
            print(f"attempt {attempt + 1} cut after {exc.items_delivered}")
    raise RuntimeError(f"feed still failing after {attempts} attempts")

books = fetch_all(url)
```

Iterating the same interpreter again simply opens another run, with
its own source and counters, so a retry loop over one instance is
safe. A fresh instance per attempt keeps it obvious anyway.

## 4. Authenticated feeds

Everything about reaching the feed lives in `Transport`. The four auth
schemes cover most feed APIs:

<!-- recipe: auth-feeds -->
```python
from xmlstreamer import ApiKeyAuth, StreamInterpreter, Transport

interpreter = StreamInterpreter(
    url=url,
    separator_tag="book",
    transport=Transport(auth=ApiKeyAuth(header="X-Api-Key", value="my-key")),
)
books = list(interpreter)
```

The other schemes, plus a proxy, look like this (same `Transport` object):

```python
from xmlstreamer import BasicAuth, BearerAuth, DigestAuth, Transport

Transport(auth=BasicAuth("user", "password"))   # also FTP login
Transport(auth=DigestAuth("user", "password"))
Transport(auth=BearerAuth("token"))
Transport(
    auth=BearerAuth("token"),
    proxy={"http": "http://proxy:8080", "https": "http://proxy:8080"},
)
```

## 5. Keep only recent items

`item_filter` accepts any callable: it receives the flat item dict, a
truthy return keeps the item, and a falsy one drops it. To parameterize
a filter, close over its configuration with `functools.partial`:

<!-- recipe: keep-recent -->
```python
import functools
from datetime import datetime, timedelta

from xmlstreamer import StreamInterpreter

def added_after(cutoff, item):
    try:
        added = datetime.fromisoformat(item.get("added", ""))
    except ValueError:
        return True  # missing or unparseable date: keep the item
    return added >= cutoff

interpreter = StreamInterpreter(
    url=url,
    separator_tag="book",
    item_filter=functools.partial(
        added_after, datetime.now() - timedelta(days=30)
    ),
)
recent = list(interpreter)
```

For wildly inconsistent date formats, `dateparser.parse` is a drop-in
replacement for `fromisoformat` (third-party dependency).

Two patterns that need no library support. Chaining: compose predicates
with `item_filter=lambda i: all(f(i) for f in FILTERS)` - `all()`
short-circuits, so the chain stops at the first filter that rejects.
Error policy: an exception raised by your filter propagates raw and
stops the run (a filter bug is caller code, not feed damage - it is
never silently converted into kept or dropped items). To tolerate a
flaky filter instead, wrap it in a try/except that returns `True`
(keep) or `False` (drop) on failure - which one is your call.

## 6. Real lists for repeated tags

Repeated sibling tags are numbered by default (`author`, `author_1`, ...)
so the item shape never depends on how many siblings arrived. When you WANT
a list, declare the path: declared paths are ALWAYS lists, with one element
or many.

<!-- recipe: real-lists -->
```python
from xmlstreamer import Nested, StreamInterpreter

interpreter = StreamInterpreter(
    url=url,
    separator_tag="book",
    output=Nested(force_list={"authors/author"}),
)
books = list(interpreter)
names = [author["name"] for author in books[0]["authors"]["author"]]
```

## 7. Sample a feed without downloading it all

Stream mode plus an early `break` reads only what you consumed. The context
manager releases the connection on exit; without it, the socket would stay
open until garbage collection. Exiting also ends the run: it logs its stats
and stops delivering, so nothing left in its buffer arrives afterwards.

<!-- recipe: sample-cheaply -->
```python
from xmlstreamer import StreamInterpreter, Transport

sample = []
with StreamInterpreter(
    url=url,
    separator_tag="book",
    transport=Transport(download_mode="stream"),
) as interpreter:
    for item in interpreter:
        sample.append(item)
        if len(sample) == 10:
            break
```

## 8. Feed to JSONL in ten lines

Flat items are JSON-ready exactly as they arrive, so exporting a feed
needs no intermediate model. This really is the whole pipeline:

<!-- recipe: feed-to-jsonl -->
```python
import json

from xmlstreamer import StreamInterpreter

interpreter = StreamInterpreter(url=url, separator_tag="book")
with open("books.jsonl", "w", encoding="utf-8") as out:
    for item in interpreter:
        out.write(json.dumps(item, ensure_ascii=False) + "\n")
```

## 9. Production logging and a zero-items alert

xmlstreamer logs under the `xmlstreamer` logger name: end-of-feed stats at
INFO, discarded malformed items and runtime cuts at WARNING. A run that
delivers zero items is worth alerting on; hook the end of iteration to
catch it.

<!-- recipe: production-logging -->
```python
import logging

from xmlstreamer import StreamInterpreter

logging.basicConfig(level=logging.INFO)
logging.getLogger("xmlstreamer").setLevel(logging.WARNING)

class AlertingInterpreter(StreamInterpreter):
    def run_finished(self, run):
        if run.stats_parsed_items == 0:
            logging.error("feed delivered zero items")

books = list(AlertingInterpreter(url=url, separator_tag="book"))
```

`run_finished(run)` is called once per run, however that run ended (the
feed ended, the budget ran out, or you closed it), after it released its
source and logged its stats. It receives the run that ended, so its
counters are the right ones even when another run is already open. It is
a report, not a step of the run: if your alerting code raises (an
alerting call timing out, say), the error is logged at `ERROR` with its
traceback
and the run still ends normally - items already delivered stay
delivered. You can open a fresh run from it (a retry, for instance),
with one exception: while `interpreter.close()` is releasing everything,
opening one raises `RuntimeError` - a teardown hands out no new
sources.

## 10. Time-boxed runs for schedulers

`max_running_time` is a per-run budget in seconds, measured on a monotonic
clock. When the budget runs out, iteration ends cleanly: a WARNING is
logged, the source is released, and `StopIteration` ends the loop like a
normal end of feed. Everything already delivered stays processed, and
the next run starts with a fresh budget.

<!-- recipe: time-boxed-runs -->
```python
from xmlstreamer import StreamInterpreter

interpreter = StreamInterpreter(
    url=url,
    separator_tag="book",
    max_running_time=300,
)
books = list(interpreter)
print(f"delivered {interpreter.stats_delivered_items} items within the budget")
```

## 11. Process many feeds in parallel

Every `StreamInterpreter` is fully independent, and waiting on the
network releases the GIL - so a plain thread pool overlaps the
downloads of many feeds while their parsing interleaves in the gaps.
On slow remote servers this is worth several times the wall-clock time,
and the per-feed ordering guarantee is untouched.

<!-- recipe: parallel-feeds -->
```python
from concurrent.futures import ThreadPoolExecutor

from xmlstreamer import StreamInterpreter, Transport

def fetch(url):
    interpreter = StreamInterpreter(
        url=url,
        separator_tag="book",
        transport=Transport(download_mode="stream"),
    )
    return url, list(interpreter)

results = {}
with ThreadPoolExecutor(max_workers=6) as pool:
    for url, books in pool.map(fetch, feed_urls):
        results[url] = len(books)
```

Keep `max_workers` modest: the win comes from overlapping network
waits, not from parallel parsing - Python still parses one item at a
time across threads, so a pool buys nothing for local files.

## 12. Replay a captured feed from disk

A feed that broke something is worth keeping. Save the bytes once, then
reproduce the run locally as many times as the diagnosis needs - same
pipeline, gzip and encoding detection included, no network. The snippet
assumes `path` points at the saved file:

<!-- recipe: replay-captured-feed -->
```python
from xmlstreamer import StreamInterpreter

with StreamInterpreter(source=path, separator_tag="book") as interpreter:
    books = list(interpreter)
```

`source=` also takes any binary file-like object, so
`io.BytesIO(captured_bytes)` drives a unit test and `sys.stdin.buffer`
sits in a pipeline. A path can be iterated again (each run reopens it);
a file-like object is consumed by its first run.

## Things you do not need a recipe for

A fair question after a dozen recipes is what still needs configuring
by hand. These never do - the defaults already handle them:

- **Character encodings**: detected on the stream and transcoded to UTF-8,
  even when the XML declaration lies. Valid UTF-8 is never altered.
- **Gzip**: detected by magic bytes, multi-member archives included.
- **Malformed items**: discarded with a WARNING (cause and preview logged);
  their healthy neighbors are unaffected.
- **Items bigger than buffer_size**: the buffer grows to fit the current
  item; the parameter is a starting size, not a limit.
