"""source= reads local paths and binary file-like objects; url= stays remote."""

import gzip
import io
import tempfile

import pytest

from feedlib import build_feed

from xmlstreamer import Raw, StreamInterpreter, Transport

THREE = [{"t": "one"}, {"t": "two"}, {"t": "three"}]

# One malformed item between healthy ones: <t> never closes.
BROKEN_MIDDLE = (
    b"<feed>"
    b"<item><t>one</t></item>"
    b"<item><t>two</item>"
    b"<item><t>three</t></item>"
    b"</feed>"
)


class _OneWayStream:
    """The shape of a pipe: read() and nothing else."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)


class _Read1Stream:
    """A buffered pipe: read() would block to fill, read1() returns
    whatever is already there (in tiny pieces, to stress chunking)."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        raise AssertionError("read1 must be preferred when available")

    def read1(self, size: int = -1) -> bytes:
        return self._buffer.read(7 if size is None or size < 0 else min(size, 7))


def make_interpreter(source, **kwargs) -> StreamInterpreter:
    return StreamInterpreter(source=source, separator_tag="item", **kwargs)


def feed_file(tmp_path, data: bytes):
    path = tmp_path / "feed.xml"
    path.write_bytes(data)
    return path


def test_path_str_streams_items(tmp_path):
    path = feed_file(tmp_path, build_feed(THREE))
    with make_interpreter(str(path)) as interpreter:
        assert list(interpreter) == THREE


def test_path_object_streams_items(tmp_path):
    path = feed_file(tmp_path, build_feed(THREE))
    with make_interpreter(path) as interpreter:
        assert list(interpreter) == THREE


def test_file_like_streams_items(tmp_path):
    with make_interpreter(io.BytesIO(build_feed(THREE))) as interpreter:
        assert list(interpreter) == THREE


def test_non_seekable_file_like_streams_items():
    # Nothing beyond read() may be required: stdin has no seek.
    with make_interpreter(_OneWayStream(build_feed(THREE))) as interpreter:
        assert list(interpreter) == THREE


def test_file_like_read1_is_preferred_over_read():
    # read(n) on a buffered pipe blocks until n bytes or EOF; read1(n)
    # hands over what already arrived. A live pipe needs the latter.
    with make_interpreter(_Read1Stream(build_feed(THREE))) as interpreter:
        assert list(interpreter) == THREE


def test_gzip_path_is_transparent(tmp_path):
    path = feed_file(tmp_path, gzip.compress(build_feed(THREE)))
    with make_interpreter(str(path)) as interpreter:
        assert list(interpreter) == THREE


def test_gzip_file_like_is_transparent():
    compressed = io.BytesIO(gzip.compress(build_feed(THREE)))
    with make_interpreter(compressed) as interpreter:
        assert list(interpreter) == THREE


def test_legacy_encoding_from_source(tmp_path):
    # The encoding sample runs on local sources too: latin-1 bytes with
    # no declaration must come back as text, not mojibake.
    items = [{"t": "programación región café"}] * 5
    path = feed_file(tmp_path, build_feed(items, encoding="latin-1"))
    with make_interpreter(str(path)) as interpreter:
        assert list(interpreter) == items


def test_url_and_source_together_raise():
    with pytest.raises(ValueError):
        StreamInterpreter(
            url="http://unused.invalid/feed",
            separator_tag="item",
            source=io.BytesIO(b"<feed></feed>"),
        )


def test_neither_url_nor_source_raises():
    with pytest.raises(ValueError):
        StreamInterpreter(separator_tag="item")


def test_transport_with_source_raises():
    # Transport is user_agent, auth, proxy, timeout, download_mode: all
    # network concepts. Accepting it here would be dead configuration.
    with pytest.raises(ValueError):
        make_interpreter(io.BytesIO(b"<feed></feed>"), transport=Transport())


def test_text_mode_file_raises_at_construction(tmp_path):
    path = feed_file(tmp_path, build_feed(THREE))
    with open(path, "r", encoding="utf-8") as handle:
        with pytest.raises(TypeError):
            make_interpreter(handle)


@pytest.mark.parametrize("bad", [123, 4.5, object(), [b"<feed/>"]])
def test_source_of_wrong_type_raises(bad):
    with pytest.raises(TypeError):
        make_interpreter(bad)


def test_missing_path_fails_at_iter(tmp_path):
    # Reachability stays an iteration-time error, like the URL scheme:
    # the type is checked at construction, the filesystem is not.
    interpreter = make_interpreter(str(tmp_path / "absent.xml"))
    with pytest.raises(FileNotFoundError):
        iter(interpreter)


def test_path_source_restarts_per_iteration(tmp_path):
    # A path is an address, like a URL: every run reopens it in full.
    path = feed_file(tmp_path, build_feed(THREE))
    with make_interpreter(str(path)) as interpreter:
        assert list(interpreter) == THREE
        assert list(interpreter) == THREE


def test_file_like_source_is_one_shot():
    # A consumed object cannot honor a second run: failing loudly beats
    # a silent zero-item iteration.
    with make_interpreter(io.BytesIO(build_feed(THREE))) as interpreter:
        assert list(interpreter) == THREE
        with pytest.raises(RuntimeError, match="cannot be re-read"):
            iter(interpreter)


def test_file_like_is_not_closed_by_the_run():
    # The run closes what it opens; a caller's object stays the
    # caller's (closing stdin would be hostile).
    stream = io.BytesIO(build_feed(THREE))
    with make_interpreter(stream) as interpreter:
        list(interpreter)
    assert stream.closed is False


@pytest.mark.parametrize(
    "make_source",
    [str, io.BytesIO],
    ids=["path", "file-like"],
)
def test_source_never_touches_a_temp_file(tmp_path, monkeypatch, make_source):
    # A local source has nothing to spool: the temp-file machinery of
    # the download path must never run.
    def boom(*args, **kwargs):
        raise AssertionError("a local source must not spool to a temp file")

    monkeypatch.setattr(tempfile, "TemporaryFile", boom)
    data = build_feed(THREE)
    source = make_source(feed_file(tmp_path, data)) if make_source is str\
        else make_source(data)
    with make_interpreter(source) as interpreter:
        assert list(interpreter) == THREE


@pytest.mark.parametrize(
    "make_source",
    [str, io.BytesIO],
    ids=["path", "file-like"],
)
def test_broken_item_in_source_feed_drops_loudly(
    tmp_path, caplog, make_source
):
    # The gauntlet contract survives the transport change: the broken
    # item drops with a warning, its neighbors are delivered.
    source = make_source(feed_file(tmp_path, BROKEN_MIDDLE))\
        if make_source is str else make_source(BROKEN_MIDDLE)
    with caplog.at_level("WARNING", logger="xmlstreamer"):
        with make_interpreter(source) as interpreter:
            assert list(interpreter) == [{"t": "one"}, {"t": "three"}]
    assert "Discarding unparseable item" in caplog.text


def test_raw_output_yields_item_bytes():
    # Raw() emits the slice between the separator tags, exactly as the
    # parser receives it; the tags themselves were consumed by the scan.
    feed = io.BytesIO(build_feed([{"t": "one"}, {"t": "two"}]))
    with make_interpreter(feed, output=Raw()) as interpreter:
        assert list(interpreter) == [b"<t>one</t>", b"<t>two</t>"]


def test_raw_selection_matches_parsed_mode():
    # Raw changes the representation, never the selection: an item that
    # does not parse is dropped in both modes.
    with make_interpreter(
        io.BytesIO(BROKEN_MIDDLE), output=Raw()
    ) as interpreter:
        assert list(interpreter) == [b"<t>one</t>", b"<t>three</t>"]


def test_raw_with_filter_still_parses():
    # The filter contract is unchanged: it sees the flat dict even when
    # the delivered representation is bytes.
    feed = io.BytesIO(build_feed(THREE))
    with make_interpreter(
        feed,
        output=Raw(),
        item_filter=lambda item: item["t"] != "two",
    ) as interpreter:
        assert list(interpreter) == [b"<t>one</t>", b"<t>three</t>"]


def test_raw_keeps_undecodable_bytes(tmp_path):
    # U+FFFD replacement happens on a copy made for parsing: the raw
    # bytes keep what actually arrived, which is what forensics needs.
    padding = build_feed([{"t": "x" * 1000} for _ in range(70)])
    data = padding.replace(
        b"</feed>", b"<item><t>caf\xe9</t></item></feed>"
    )
    path = feed_file(tmp_path, data)

    with make_interpreter(str(path)) as interpreter:
        assert list(interpreter)[-1] == {"t": "caf\ufffd"}
    with make_interpreter(str(path), output=Raw()) as interpreter:
        assert list(interpreter)[-1] == b"<t>caf\xe9</t>"
