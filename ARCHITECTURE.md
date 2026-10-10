# Architecture

One page on how xmlstreamer works inside, why it is built that way, and
which invariants a change must preserve. The whole engine lives in
`xmlstreamer/__init__.py` on purpose: you can read it top to bottom in
one sitting, and this document is the map for that reading.

## The two-layer design

The core decision, from which everything else follows: parsing is split
into two layers that never share responsibilities.

1. **The `Tokenizer` sees only bytes and never understands XML.** It
   scans the stream for the opening and closing separator tags and cuts
   out one item's worth of bytes at a time. It does not decode, validate
   or interpret. It does know the shapes that are markup rather than
   content - comments, CDATA, processing instructions, DOCTYPE
   declarations, quoted attribute values - because a separator tag
   inside one of those is not an item.
2. **expat parses one item at a time, in isolation.** Each cut-out item
   is wrapped and fed to a fresh expat parser (`parse_item`), which
   either produces a flat dict or fails for that item alone.

Why: a malformed item cannot poison the document, because no parser
ever sees the document - expat only ever sees one item. Corruption
contained inside an item's delimiters drops that item, with a WARNING,
without affecting its neighbors. Structural corruption that damages an
item delimiter may also prevent adjacent items from being isolated, but
it never fabricates or mutates an emitted record. This is the
property the whole library exists to provide; strict parsers lose the
entire feed at the first broken byte, and recovery parsers rewrite
damage into records that never existed.

Its limit is worth stating exactly, because it is a property of the
damage and not of the design: an item is isolated when its corruption
is CONTAINED - bad nesting, a stray `<`, an entity that never
resolves. Damage that OPENS markup and never closes it (a `<?`, a
`<!--`, a `<![CDATA[`) is not inside an item any more: what follows is
inside the section, neighbours included. `Sections` bounds how far that
can go while the feed keeps flowing; when the feed ends there, the rest
is lost rather than re-read, because re-reading markup is how records
that never existed get invented.

The price of the design is one documented contract: the separator tag
must not nest inside itself, because a byte-level scanner cannot know
which `</item>` closes which `<item>` without understanding XML.

The lexer/interpreter split is classic compiler construction; this
library's rendition owes its shape (and the `Tokenizer` /
`StreamInterpreter` names) to Ruslan Spivak's "Let's Build A Simple
Interpreter" series (https://ruslanspivak.com/lsbasi-part1/). Feeds add
one twist the classic design never needed: a source program is rejected
whole on a syntax error, but a feed must survive one - so this lexer
isolates damage instead of halting.

## The full pipeline

```
URL -> download (Transport: temp file | straight stream)
   (or: source= - a local path or binary file-like object, no download)
    -> gzip detection by magic bytes (multi-member supported)
    -> encoding resolution (valid utf-8 passes through untouched;
       chardet runs only for non-utf-8 or NUL-dense samples)
    -> Tokenizer (bytes -> one item's bytes at a time)
    -> parse_item (expat, per item -> flat {path: text} dict)
    -> optional Nested() reshaping (after filters)
```

`StreamInterpreter` is the configuration: url or source, separator,
filter, output, transport and section policy, reusable across runs.
Iterating
it opens a `FeedRun`, which owns one pass: its source, its tokenizer,
its stats. The interpreter answers for the last one (`last_run`, and
the `stats_*` properties) so reading stats after a loop stays simple.

## Invariants the code protects

These are the properties a change must not break. Most of them are
pinned by tests (`tests/`), including generative ones (Hypothesis).

- **The pending bytes are `BUFFER[POS:]`.** The tokenizer advances a
  cursor instead of re-slicing the buffer per item; consumed bytes are
  compacted away once per refill (`feed_origin_buffer`), never in the
  hot path.
- **Partial tails are never discarded.** A `<ite` or `<!-` cut at a
  buffer boundary is kept (`pending_tag_tail`) until more bytes
  arrive; an unterminated comment or CDATA section requests more data
  without consuming anything.
- **Separator matches inside markup do not count** (`advance_to`).
  Comments, CDATA, processing instructions and DOCTYPE declarations are
  scanned over, so a `<item>` written inside one can neither open, nor
  close, nor become an item.
- **The scanner never re-reads a byte** (`SCAN`). Section walking and
  tag walking are both resumable: split across refills, they continue
  where they stopped instead of restarting at the opener, so a huge
  comment (or a huge attribute value) costs one pass in total, not one
  per refill.
- **The same bytes give the same items, however they arrive.** Buffer
  and chunk sizes change when work happens, never what comes out: a
  `>` inside a quoted attribute value does not end a tag at any buffer
  size, and section limits are measured over the stream.
- **A section that never ends is not a section** (under the default
  policy). Past its size limit the scanner logs a WARNING and re-reads
  the opener as ordinary text, which bounds both the memory it can hold
  and the damage a stray `<?` in a description can do. `Sections(
  on_limit="markup")` keeps discarding instead, and then no record can
  come out of a section at all.
- **Every emitted item's content went through a full expat parse.**
  There is no path that emits unparsed or partially parsed content.
  Invalid items are dropped loudly (WARNING with cause and preview),
  never silently. The delimiters themselves are located lexically, not
  parsed: a separator tag the feed spelled wrong (an unquoted or
  repeated attribute, junk in a closing tag) still delivers its item,
  whose content is intact, and is reported once per run with a count.
- **User-code errors are never absorbed - except a report that fails.**
  An exception raised by the `item_filter` callable propagates raw to
  the caller and stops the run. The library absorbs damage in the data
  (drop + WARNING), never bugs in caller code: a filter typo must
  crash, not silently keep or drop items. The end-of-run hook is the
  one exception, and for the reason the rule exists: the filter DECIDES
  what the caller receives, while `run_finished(run)` only REPORTS. A
  report that raises is logged at ERROR with its traceback and goes no
  further, so it cannot undo work already delivered, strand another
  run's socket, or mask the exception a `with` block was unwinding.
- **Losing anything costs a WARNING.** Dropped items, corrupt or
  truncated gzip, a feed ending inside an item, a section that never
  terminates, invalid UTF-8 replaced with `U+FFFD` (once per run, then
  counted in the closing stats), a key collision that had to reshape an
  item. Reading a run's log tells you everything the run could not
  keep; silence means nothing was lost.
- **Entity declarations are rejected at the declaration** (billion
  laughs and XXE seeds die before any expansion is possible), and the
  item that carried them is discarded like any other invalid item.
- **`buffer_size` is a starting size, not a limit.** The buffer grows
  to fit the current item; items larger than the buffer still parse.
- **Every run is independent.** `iter()` opens a `FeedRun` with its own
  source, tokenizer, counters AND time budget; nothing is shared with a
  previous or concurrent one, so nested loops and retries are ordinary
  code. The budget is the value the run opened with, not a reading of
  the interpreter's: retuning the configuration never moves a budget
  already granted.
- **A run ends once, however it ends.** The feed ended, the budget ran
  out, or the caller closed it (`with`, `close()`, or closing the
  interpreter, which ends every run it still has open): the source is
  released, the funnel logged and `run_finished(run)` called exactly one
  time. An ended run delivers nothing more - not even what its buffer
  still held - and stops scanning too; asking it for another item is a
  plain `StopIteration`. That holds on every path into it, and there
  are more than one: the `item_filter` runs inside the run's own loop,
  and so does every log handler, because the library logs while it
  scans. After the end no counter moves either, so the funnel a run
  reported when it closed is the funnel it ends with, whoever closed it
  and from wherever.
- **A teardown owns itself.** `close()` on the interpreter takes one
  snapshot of its open runs, and ending each one calls a hook that is
  caller code. Opening a run from there while that sweep runs would
  leave it open, so it raises `RuntimeError`; closing again from there
  is a no-op, because the sweep already running owns the teardown -
  otherwise it recursed once per run, and worse, carried another
  source's failure into that hook, where isolation would log it away as
  a broken hook instead of reporting a release that failed. A run that
  ended by itself may still be replaced from the hook: only the global
  teardown refuses.
- **The error boundary is sharp.** Failures before the first item
  (connect, HTTP status, opening a source, the initial encoding read)
  raise their raw
  exception in both download modes; once items are flowing in stream
  mode, network errors surface as `FeedInterruptedError` carrying
  `items_delivered`. Library-owned errors descend from
  `XMLStreamerError`.
- **Bad arguments fail at construction.** Every `StreamInterpreter`
  argument is validated in `__init__` (`TypeError`/`ValueError` at the
  call site); the `Tokenizer` re-checks its own. A mistake can never
  surface mid-iteration or as a silent zero-item run. The deliberate
  exceptions are reachability - the URL scheme, and whether a source
  path opens - which stay iteration-time errors to keep the error
  boundary above intact.
- **Resources release deterministically.** `close()` (or the context
  manager) releases the socket or temp file immediately; exhausting the
  iterator does the same via the generator's `finally` (primed so it
  runs even if no chunk was ever pulled). Closing the interpreter
  attempts every run before reporting anything: one source that will
  not release cannot strand the others, and neither can a Ctrl-C. A
  cancellation raised while the sweep runs is deferred rather than
  absorbed - what is left is bounded work - and then re-raised ahead of
  any release that merely failed, so an interrupt is never the thing
  that gets dropped, not even by a second one arriving later: the
  FIRST is what the caller gets. The sweep itself calls nothing but
  `close()` on each run: what failed to release is reported afterwards,
  because a log handler is caller code too and an interrupt raised from
  one is only harmless once every source is already out. That report
  happens even when a cancellation is already pending - skipping it
  would leave the releases that failed with no trace at all, neither
  raised nor logged.
- **Output shape never depends on the data.** Repeated siblings get
  numbered keys (`tag`, `tag_1`, ...) instead of implicit lists;
  `Nested(force_list=...)` paths are ALWAYS lists, with one element or
  many.
- **No key is ever overwritten.** When a feed carries a field literally
  named like a numbered key (`<x_1>` beside repeated `<x>`), numbering
  moves past the collision and warns, rather than dropping one of the
  two values. Both survive; the shape says so.
- **The same feed cannot mean two things.** An empty item is not a
  record in either spelling: `<item/>` and `<item></item>` are the same
  nothing, and neither reaches the consumer.
- **Configuration is validated and frozen where it is written.** Every
  `StreamInterpreter`, `Transport`, `Sections` and `Nested` argument is
  checked in its constructor, credentials and proxy routes included,
  and `Nested(force_list=...)` is frozen there too: a generator would
  otherwise be spent on the first item and silently reshape every one
  after it. Each value is checked as what it is: a header value must be
  latin-1, unpadded and free of line breaks (the wire drops or forges
  the rest), while a credential is only refused a line break, because
  Basic encodes it and FTP sends it verbatim. The latin-1 an
  `Authorization` header needs is checked on the HTTP path, which is
  the only place it binds, and only for what actually travels there:
  Basic carries both fields, Digest hashes the password as utf-8 and
  carries only the username.

## Map of the source

`xmlstreamer/__init__.py`, in reading order:

1. Errors (`XMLStreamerError` family) and auth/`Transport` dataclasses.
2. Acquisition plumbing: `download_file` / `_stream_feed_generator` /
   `get_source_feed_generator`, the two shared pipelines
   (`_feed_generator_from_file`, `_feed_generator_from_chunks`), gzip
   (`stream_gzip_decompress`), encoding (`resolve_sample_encoding`,
   `resolve_stream_encoding`, `decode_stream`), and `_owned_generator`
   (deterministic cleanup).
3. Per-item parsing: `ItemHandler` (flattening rules) and `parse_item`.
4. `Nested` / `to_nested` / `Raw` (opt-in output shapes).
5. `Tokenizer` (the byte layer: buffer, cursors, section scanning).
6. `StreamInterpreter` (the public iterator).

`xmlstreamer/__main__.py` is the CLI: a thin argparse wrapper that
streams items as JSON Lines or raw bytes. It uses only the public API.

## Safety nets

A change is validated by layers, cheapest first:

1. The example-based suite (`tests/`), including end-to-end tests
   against a local HTTP server - run on the oldest and newest supported
   Python.
2. Property-based tests (`tests/test_properties.py`): item isolation
   under generated corruption, chunking/buffer invariance, totality on
   arbitrary bytes.
3. Executable documentation: every `COOKBOOK.md` snippet runs in the
   suite, so docs cannot drift from the API.
4. The benchmark harness (`benchmarks/`), including the dirty-feed
   gauntlet whose verdicts are generated, not hand-written.

## Design decisions and their reasons

Each choice below, and the reason it rests on:

- **No implicit lists.** The shape of an item must not depend on how
  many siblings happened to arrive; collections are opt-in and then
  guaranteed (`force_list`).
- **No recovery parsing.** Repair-by-guessing fabricates records; the
  benchmark's gauntlet shows exactly how. Dropping loudly is the
  contract.
- **Every section type is skipped, and none of them can trap the
  scanner.** Comments, CDATA, processing instructions and DOCTYPE
  declarations are markup: a separator tag written inside one is not an
  item, and emitting it would fabricate a record. Where each one ends
  is decided by walking, not by matching a string, because quotes and
  the depth of a DOCTYPE's internal subset both hide `>` bytes that
  close nothing.
- **What an unterminated section means is the caller's call.** Trusting
  a feed to close what it opens is a separate matter from skipping
  markup, and there is no answer that is right for everyone: never
  re-reading cannot fabricate but loses everything after one stray
  `<?`, and re-reading bounds the damage but can turn a section longer
  than the limit into records. `Sections(max_size=..., on_limit="text" |
  "markup")` is that choice, defaulting to `"text"`. Its
  window is measured from the opener over the stream, so the decision
  never depends on how the reads fell. An unterminated section at EOF
  is NOT re-read under either policy: a cut download is a cut
  download, and re-reading a truncated comment full of commented-out
  items would invent them.
- **No async API.** A sync iterator composes with anything, and a dual
  API would double the most delicate surface (errors and cleanup).
- **No internal parallelism.** Parsing callbacks cannot release the
  GIL, and per-item fan-out breaks ordering and first-item latency.
  Parallelism belongs at the per-feed level, in the caller's hands
  (see the cookbook's parallel-feeds recipe).
- **Configuration and run are separate objects.** `StreamInterpreter`
  holds the settings and is reusable; iterating it opens a `FeedRun`
  that owns everything belonging to one pass, so nested loops and
  retries over one instance are ordinary code and `list(iter(x))`
  downloads the feed once. Acquisition happens inside `iter()`, so a
  feed that cannot be reached fails where callers already handle it.
  The end-of-run hook lives on the interpreter, for subclasses that
  alert on empty feeds: `run_finished(run)` is told WHICH run ended
  and reports rather than driving control flow. Transforming items is
  a generator the caller wraps around the run, not a `__next__` to
  override.

- **One file, two dependencies.** Auditability is a feature for a
  library that asks to be trusted with hostile input. The engine stays
  in one file until there is a concrete reason it cannot.
