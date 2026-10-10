import collections.abc
import dataclasses
import io
import logging
import os
import shutil
import tempfile
import time
import urllib.request
import weakref
import zlib
from contextlib import closing
from datetime import datetime
from xml.parsers import expat

from urllib.parse import quote, urlparse, urlunparse

import chardet

import requests

import re

from io import BufferedRandom

from enum import Enum

import codecs

from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Iterable
from typing import NoReturn
from typing import Any
from typing import IO
from typing import Generic
from typing import TypeVar
from typing import cast
from typing import overload
from typing import Self

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Single source of truth: pyproject reads this attribute at build time.
__version__ = "2.2.0"

__all__ = [
    "ApiKeyAuth",
    "BasicAuth",
    "BearerAuth",
    "DigestAuth",
    "FeedInterruptedError",
    "Nested",
    "FeedRun",
    "ParsedItem",
    "Raw",
    "Sections",
    "StreamInterpreter",
    "Transport",
    "USER_AGENT",
    "UnsupportedSchemeError",
    "XMLStreamerError",
    "__version__",
    "to_nested",
]

# Define the type of the feed_generator
FeedGeneratorT = Generator[bytes, None, None]

# An item filter takes the flat {path: text} item and keeps it on truthy.
ItemFilterT = Callable[[dict[str, Any]], Any] | None

# A local source: a filesystem path, or a binary file-like object.
SourceT = str | os.PathLike | IO[bytes]

# What a run yields, pinned at construction by output=: dicts by
# default and under Nested(), bytes under Raw().
ItemT = TypeVar("ItemT")


USER_AGENT: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWeb"\
    "Kit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"

# (connect, read) seconds; the read timeout is the maximum silence between
# bytes on a streamed download, not a total duration. None disables both.
DEFAULT_TIMEOUT: tuple = (30, 300)

TimeoutT = float | tuple | None

STREAM_CHUNK_SIZE: int = 64 * 1024

def is_valid_xml_name(name: str) -> bool:
    """
    Whether a tag can be called this, decided by the same parser that
    reads the feed.
    """
    seen: list = []
    parser = expat.ParserCreate()
    parser.StartElementHandler = lambda tag, attrs: seen.append((tag, attrs))
    try:
        parser.Parse(f"<{name}/>".encode(), True)
    except Exception:
        return False
    return len(seen) == 1 and seen[0][0] == name and not seen[0][1]

GZIP_MAGIC_NUMBER: bytes = b"\x1f\x8b"


class XMLStreamerError(Exception):
    """Base class for every error this library raises on its own."""


class UnsupportedSchemeError(XMLStreamerError, ValueError):
    """
    The URL scheme is none of http, https or ftp. The message names the
    scheme only: a URL can carry credentials.
    """


class FeedInterruptedError(XMLStreamerError):
    """
    Stream download mode: the connection died mid-feed. items_delivered
    counts items already handed out; the network error is __cause__.
    """

    def __init__(self, items_delivered: int, cause: Exception):
        self.items_delivered = items_delivered
        super().__init__(
            f"feed interrupted after {items_delivered} items: {cause}"
        )


class _StringFields:
    """Credentials are strings: anything else fails where it is written."""

    def __post_init__(self) -> None:
        for name in getattr(self, "__dataclass_fields__"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(
                    f"{type(self).__name__}.{name} must be a str"
                )
            # A credential reaches the wire inside a header or an FTP
            # command: a line break in one would forge a second.
            if "\n" in value or "\r" in value:
                raise ValueError(
                    f"{type(self).__name__}.{name} must not contain line "
                    "breaks"
                )


def _check_header_value(owner: str, field: str, value: str) -> None:
    """
    A value an HTTP header carries verbatim: padding around it is not
    part of the value (the wire drops it) and latin-1 is all a header
    can hold. Credentials are exempt: Basic encodes them and FTP sends
    them as they are.
    """
    if value != value.strip():
        raise ValueError(
            f"{owner}.{field} goes into a header verbatim, which does "
            "not carry padding: strip it."
        )
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError(
            f"{owner}.{field} must be latin-1: an HTTP header carries "
            "no other characters."
        ) from None


@dataclasses.dataclass(slots=True)
class BasicAuth(_StringFields):
    username: str
    password: str


@dataclasses.dataclass(slots=True)
class DigestAuth(_StringFields):
    username: str
    password: str


@dataclasses.dataclass(slots=True)
class BearerAuth(_StringFields):
    token: str

    def __post_init__(self) -> None:
        # dataclass(slots=True) rebuilds the class: a zero-argument
        # super() points at the original one.
        _StringFields.__post_init__(self)
        _check_header_value("BearerAuth", "token", self.token)


# An HTTP header name is a token: these are the only bytes it can hold.
HEADER_TOKEN: re.Pattern = re.compile(r"[!#$%&\'*+\-.^_`|~0-9A-Za-z]+\Z")


@dataclasses.dataclass(slots=True)
class ApiKeyAuth(_StringFields):
    header: str
    value: str

    def __post_init__(self) -> None:
        # dataclass(slots=True) rebuilds the class: a zero-argument
        # super() points at the original one.
        _StringFields.__post_init__(self)
        if HEADER_TOKEN.match(self.header) is None:
            raise ValueError(
                "ApiKeyAuth.header must be a header name (a non-empty "
                "HTTP token), not a phrase."
            )
        _check_header_value("ApiKeyAuth", "value", self.value)


AuthT = BasicAuth | DigestAuth | BearerAuth | ApiKeyAuth | None


def _validate_timeout(timeout: Any) -> None:
    """A timeout is seconds, or a (connect, read) pair of seconds."""
    values = timeout if isinstance(timeout, tuple) else (timeout,)
    if isinstance(timeout, tuple) and len(timeout) != 2:
        raise ValueError(
            "timeout as a tuple must be (connect, read) seconds."
        )
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(
                "timeout must be seconds, a (connect, read) pair, or None."
            )
        if value <= 0 or value != value or value == float("inf"):
            raise ValueError(
                "timeout seconds must be a positive number "
                "(infinity and NaN never time out)."
            )


@dataclasses.dataclass(slots=True)
class Transport:
    """
    How the feed is reached: identity, credentials, route and download
    strategy ("temp" spools to a temporary file, "stream" flows straight
    from the network and can raise FeedInterruptedError mid-iteration).
    """
    user_agent: str = USER_AGENT
    auth: AuthT = None
    proxy: dict[str, str] | None = None
    timeout: TimeoutT = DEFAULT_TIMEOUT
    download_mode: str = "temp"

    def __post_init__(self) -> None:
        # Checked here, not at the first byte: a bad transport must not
        # surface as a mid-download failure or an empty run.
        if self.download_mode not in ("temp", "stream"):
            raise ValueError(
                'download_mode must be "temp" (spool to a temporary '
                'file) or "stream" (no temp file; network errors can '
                'raise FeedInterruptedError mid-iteration).'
            )
        if not isinstance(self.user_agent, str):
            raise TypeError("user_agent must be a str")
        if "\n" in self.user_agent or "\r" in self.user_agent:
            raise ValueError(
                "user_agent goes into a header verbatim: no line breaks."
            )
        _check_header_value("Transport", "user_agent", self.user_agent)
        if self.auth is not None:
            if not isinstance(
                self.auth, (BasicAuth, DigestAuth, BearerAuth, ApiKeyAuth)
            ):
                raise TypeError(
                    "auth must be a BasicAuth, DigestAuth, BearerAuth or "
                    "ApiKeyAuth instance, or None."
                )
        if self.proxy is not None:
            if not isinstance(self.proxy, dict):
                raise TypeError(
                    'proxy must be a dict like '
                    '{"https": "http://host:port"}, or None.'
                )
            for scheme, route in self.proxy.items():
                if not isinstance(scheme, str) or not isinstance(route, str):
                    raise TypeError(
                        "proxy maps scheme strings to route strings"
                    )
        if self.timeout is not None:
            _validate_timeout(self.timeout)


def _check_latin1(auth: Any, fields: tuple[str, ...]) -> None:
    """
    Credentials that travel literally in an Authorization header,
    which carries latin-1 and nothing else. The HTTP path only: the
    same credential over FTP goes out as utf-8.
    """
    for field in fields:
        try:
            getattr(auth, field).encode("latin-1")
        except UnicodeEncodeError:
            raise ValueError(
                f"{type(auth).__name__}.{field} must be latin-1 over "
                "HTTP: that is all an Authorization header carries."
            ) from None


def _build_http_auth(auth: AuthT) -> Any | None:
    """Returns the auth object compatible with requests, or None."""
    if isinstance(auth, BasicAuth):
        # Basic encodes both as latin-1 and sends them on the wire.
        _check_latin1(auth, ("username", "password"))
        return (auth.username, auth.password)
    if isinstance(auth, DigestAuth):
        # Digest hashes the password as utf-8, so it never reaches the
        # header: only the username travels literally.
        _check_latin1(auth, ("username",))
        return requests.auth.HTTPDigestAuth(auth.username, auth.password)
    return None


def _build_http_headers(user_agent: str, auth: AuthT) -> dict:
    """Builds the headers dict including the authentication header if applicable."""
    headers = {"User-Agent": user_agent}
    if isinstance(auth, BearerAuth):
        headers["Authorization"] = f"Bearer {auth.token}"
    elif isinstance(auth, ApiKeyAuth):
        headers[auth.header] = auth.value
    return headers


def stream_gzip_decompress(stream: FeedGeneratorT) -> FeedGeneratorT:
    """
    Decompress gzip from an iterable of byte chunks. Handles multi-member
    files; trailing non-gzip garbage after the last member is tolerated.
    """
    # offset 32 to skip the header
    dec = zlib.decompressobj(wbits=32 + zlib.MAX_WBITS)
    dec_fed = False
    member_done = False
    for chunk in stream:
        data = bytes(chunk)
        while data:
            try:
                rv = dec.decompress(data)
            except zlib.error as exc:
                # Garbage is tolerated only AFTER a whole member: any other
                # failure cuts the feed short, and never silently. Gzip magic
                # means a member, however broken.
                if (dec_fed or not member_done
                        or data.startswith(GZIP_MAGIC_NUMBER)):
                    logger.warning(
                        "Gzip stream is corrupt (%s): everything after "
                        "this point is lost.", exc
                    )
                return
            dec_fed = True
            if rv:
                yield rv
            if not dec.eof:
                break
            data = dec.unused_data
            dec = zlib.decompressobj(wbits=32 + zlib.MAX_WBITS)
            dec_fed = False
            member_done = True

    if dec_fed and not dec.eof:
        logger.warning(
            "Gzip stream ended mid-member: the download was likely truncated."
        )


def download_file(
    url: str,
    transport: Transport,
    deadline: float | None = None,
        ) -> BufferedRandom:
    """
    Download file to temporary file.
    """
    logger.debug("Downloading file...")
    f = tempfile.TemporaryFile()
    try:
        return _download_into(f, url, transport, deadline)
    except BaseException:
        f.close()
        raise


def _ftp_open(
    url: str,
    transport: Transport,
        ):
    auth = transport.auth
    proxy = transport.proxy
    timeout = transport.timeout
    parsed_url = urlparse(url)
    if isinstance(auth, (BasicAuth, DigestAuth)):
        # Percent-encode credentials so reserved chars (@ : / #) can't
        # break the URL; urllib's FTP handler unquotes them before login.
        username: str = quote(auth.username, safe="")
        password: str = quote(auth.password, safe="")
        ftp_url = urlunparse(parsed_url._replace(
            netloc=f"{username}:{password}@{parsed_url.hostname}"
            + (f":{parsed_url.port}" if parsed_url.port else "")
        ))
    else:
        ftp_url = url
    if proxy:
        proxy_handler = urllib.request.ProxyHandler(proxy)
        opener = urllib.request.build_opener(proxy_handler)
    else:
        opener = urllib.request.build_opener()
    # urllib takes a single timeout; use the read component.
    ftp_timeout = timeout[1] if isinstance(timeout, tuple) else timeout
    return opener.open(ftp_url, timeout=ftp_timeout)


def _copy_within_deadline(
    source: Any,
    target: BufferedRandom,
    deadline: float | None,
) -> None:
    """
    Spool the download, stopping if the run's budget runs out: the
    budget covers acquiring the feed, not only parsing it. A read
    already in flight still has to return before the check is reached.
    """
    if deadline is None:
        shutil.copyfileobj(source, target)
        return
    while True:
        if time.monotonic() > deadline:
            logger.warning(
                "> XMLStreamer > Time budget exhausted while downloading: "
                "stopping with what arrived."
            )
            return
        chunk = source.read(STREAM_CHUNK_SIZE)
        if not chunk:
            return
        target.write(chunk)


def _download_into(
    f: BufferedRandom,
    url: str,
    transport: Transport,
    deadline: float | None = None,
        ) -> BufferedRandom:
    parsed_url = urlparse(url)
    if parsed_url.scheme == "ftp":
        with closing(_ftp_open(url, transport)) as r:
            _copy_within_deadline(r, f, deadline)

    elif parsed_url.scheme in ["http", "https"]:
        headers = _build_http_headers(transport.user_agent, transport.auth)
        http_auth = _build_http_auth(transport.auth)
        with requests.get(
            url,
            stream=True,
            headers=headers,
            auth=http_auth,
            proxies=transport.proxy,
            timeout=transport.timeout,
        ) as r:
            r.raise_for_status()
            r.raw.decode_content = True
            _copy_within_deadline(r.raw, f, deadline)
    else:
        raise UnsupportedSchemeError(
            f"unsupported URL scheme {parsed_url.scheme!r}: "
            "expected http, https or ftp"
        )

    return f


def is_gzip(
    temp_file: IO[bytes]
        ) -> bool:
    temp_file.seek(0)
    is_gzip_result: bool = temp_file.read(2) == GZIP_MAGIC_NUMBER
    temp_file.seek(0)

    return is_gzip_result


def decode_stream(
    generator: FeedGeneratorT,
    encoding: str
        ) -> FeedGeneratorT:
    """
    Transcode a byte stream to utf-8 on the fly: chunk cuts inside a
    multibyte char are buffered, invalid bytes degrade to U+FFFD.
    """
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    for chunk in generator:
        decoded_chunk = decoder.decode(chunk, final=False)
        if decoded_chunk:
            yield decoded_chunk.encode("utf-8")
    tail = decoder.decode(b"", final=True)
    if tail:
        yield tail.encode("utf-8")


PASSTHROUGH_ENCODINGS = {"utf-8", "utf8", "utf-8-sig", "ascii", "us-ascii"}


def sample_decodes_as_utf8(sample: bytes) -> bool:
    """
    True if the sample is valid utf-8; final=False tolerates a multibyte
    char cut at the sample boundary.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(sample, final=False)
        return True
    except UnicodeDecodeError:
        return False


def resolve_stream_encoding(
    detected_encoding: str | None,
    sample: bytes,
        ) -> str | None:
    """
    Return the codec for decode_stream, or None for passthrough. A valid
    utf-8 sample always wins over chardet (which misdetects utf-8 as
    legacy codecs); chardet only votes on non-utf-8 or NUL-dense samples.
    """
    if not sample:
        return None

    nul_dense: bool = sample.count(0) * 10 >= len(sample)
    if nul_dense is False and sample_decodes_as_utf8(sample):
        return None

    if detected_encoding is None:
        return None
    if detected_encoding.lower().replace("_", "-") in PASSTHROUGH_ENCODINGS:
        return None
    try:
        codecs.lookup(detected_encoding)
    except LookupError:
        # Codec unknown to Python: fall back to utf-8 passthrough.
        return None
    return detected_encoding


def resolve_sample_encoding(sample: bytes) -> str | None:
    """
    Decide the stream codec for a sample. Valid utf-8 resolves to
    passthrough without ever running chardet; only non-utf-8 or
    NUL-dense samples pay the detection cost.
    """
    if not sample:
        return None
    nul_dense: bool = sample.count(0) * 10 >= len(sample)
    if nul_dense is False and sample_decodes_as_utf8(sample):
        return None
    detected_encoding = chardet.detect(sample)["encoding"]
    logger.debug("Detected encoding: %s", detected_encoding)
    return resolve_stream_encoding(detected_encoding, sample)


def buffered_random_to_generator(
    buffered_random: IO[bytes],
    chunk_size: int = STREAM_CHUNK_SIZE
        ) -> Generator[bytes, None, None]:
    while True:
        chunk = buffered_random.read(chunk_size)
        if not chunk:
            break
        yield chunk


ENCODING_SAMPLE_SIZE: int = 64 * 1024


def read_stream_sample(
    generator: FeedGeneratorT,
    sample_size: int = ENCODING_SAMPLE_SIZE
        ) -> bytes:
    """
    Read at least sample_size bytes (chunk granularity), or the whole
    stream if shorter.
    """
    sample: bytes = b""
    try:
        while len(sample) < sample_size:
            sample += next(generator)
    except StopIteration:
        ...
    return sample


def _owned_generator(
    feed_generator: FeedGeneratorT,
    closer: Callable[[], Any],
        ) -> FeedGeneratorT:
    """
    Run closer() when the generator is exhausted, closed or errors out.
    Primed past a sentinel yield so close() reaches the finally even if
    the consumer never pulled a chunk.
    """
    def owned() -> FeedGeneratorT:
        try:
            yield b""
            yield from feed_generator
        finally:
            closer()

    generator = owned()
    next(generator)
    return generator


def _past_deadline(deadline: float | None, stage: str) -> bool:
    """The run's budget applies while acquiring the feed too."""
    if deadline is not None and time.monotonic() > deadline:
        logger.warning(
            "> XMLStreamer > Time budget exhausted while %s: stopping.",
            stage,
        )
        return True
    return False


def _feed_generator_from_chunks(
    raw_chunks: Iterable[bytes],
    closer: Callable[[], Any],
    deadline: float | None = None,
        ) -> FeedGeneratorT:
    """
    Feed generator over a one-way chunk iterator: gzip and encoding
    detection peek the head and re-emit it, since there is nothing to
    seek back. Worst case one encoding sample is held in memory.
    """
    try:
        head = b""
        for chunk in raw_chunks:
            head += chunk
            if len(head) >= 2 or _past_deadline(deadline, "reading the head"):
                break
        is_gzip_result: bool = head[:2] == b"\x1f\x8b"
        logger.debug("Is gzip: %s", is_gzip_result)

        def chained() -> FeedGeneratorT:
            if head:
                yield head
            yield from raw_chunks

        if is_gzip_result is True:
            decompressed = stream_gzip_decompress(stream=chained())
        else:
            decompressed = chained()

        sampled = []
        sampled_size = 0
        for chunk in decompressed:
            sampled.append(chunk)
            sampled_size += len(chunk)
            if sampled_size >= ENCODING_SAMPLE_SIZE\
                    or _past_deadline(deadline, "sampling the encoding"):
                break
        first_sample: bytes = b"".join(sampled)
        stream_encoding: str | None = resolve_sample_encoding(
            first_sample
        )
        logger.debug(
            "Stream transcoding: %s", stream_encoding or "not needed (utf-8)"
        )

        def replayed() -> FeedGeneratorT:
            yield from sampled
            yield from decompressed

        feed_generator = replayed()
        if stream_encoding is not None:
            feed_generator = decode_stream(
                feed_generator,
                stream_encoding
            )
    except BaseException:
        closer()
        raise

    return _owned_generator(feed_generator, closer)


def _stream_feed_generator(
    url: str,
    transport: Transport,
    deadline: float | None = None,
        ) -> FeedGeneratorT:
    """
    Feed generator without a temp file: gzip and encoding detection peek
    the stream head and re-emit it, since there is nothing to seek back.
    """
    parsed_url = urlparse(url)
    if parsed_url.scheme == "ftp":
        response = _ftp_open(url, transport)
        raw_chunks = iter(lambda: response.read(STREAM_CHUNK_SIZE), b"")
        closer = response.close
    elif parsed_url.scheme in ["http", "https"]:
        response = requests.get(
            url,
            stream=True,
            headers=_build_http_headers(transport.user_agent, transport.auth),
            auth=_build_http_auth(transport.auth),
            proxies=transport.proxy,
            timeout=transport.timeout,
        )
        closer = response.close
        try:
            response.raise_for_status()
        except BaseException:
            closer()
            raise
        raw_chunks = response.iter_content(chunk_size=STREAM_CHUNK_SIZE)
    else:
        raise UnsupportedSchemeError(
            f"unsupported URL scheme {parsed_url.scheme!r}: "
            "expected http, https or ftp"
        )

    return _feed_generator_from_chunks(
        raw_chunks=raw_chunks, closer=closer, deadline=deadline
    )


def _feed_generator_from_file(
    f: IO[bytes],
    closer: Callable[[], Any],
        ) -> FeedGeneratorT:
    """
    Feed generator over a seekable binary file: the gzip check and the
    encoding sample rewind with seek, nothing is held in memory.
    """
    f.seek(0)
    is_gzip_result: bool = is_gzip(temp_file=f)
    logger.debug("Is gzip: %s", is_gzip_result)

    def build_feed_generator() -> FeedGeneratorT:
        f.seek(0)
        if is_gzip_result is True:
            # Fixed-size chunks; iterating a binary file splits on \n bytes.
            return stream_gzip_decompress(
                stream=buffered_random_to_generator(buffered_random=f)
            )
        return buffered_random_to_generator(buffered_random=f)

    # Encoding detection runs on BOTH paths (gzip and plain): a plain
    # UTF-16 feed is otherwise invisible to the ASCII tag regexes.
    first_sample: bytes = read_stream_sample(
        generator=build_feed_generator()
    )
    stream_encoding: str | None = resolve_sample_encoding(first_sample)
    logger.debug(
        "Stream transcoding: %s", stream_encoding or "not needed (utf-8)"
    )

    # Restart the generator because the sample consumed part of it.
    feed_generator = build_feed_generator()
    if stream_encoding is not None:
        feed_generator = decode_stream(
            feed_generator,
            stream_encoding
        )

    return _owned_generator(feed_generator, closer)


def get_feed_generator(
    url: str,
    transport: Transport,
    deadline: float | None = None,
        ) -> FeedGeneratorT:
    if transport.download_mode == "stream":
        return _stream_feed_generator(
            url=url, transport=transport, deadline=deadline
        )

    temp_file: BufferedRandom = download_file(
        url=url, transport=transport, deadline=deadline
    )
    return _feed_generator_from_file(temp_file, temp_file.close)


def get_source_feed_generator(
    source: SourceT,
    deadline: float | None = None,
        ) -> FeedGeneratorT:
    """
    Feed generator for a local source. A path is opened, read through
    the seek-based pipeline and closed by the run. A file-like object
    is read once through the peeked pipeline and never closed here:
    the run closes what it opens, a caller's object stays the caller's.
    """
    if isinstance(source, (str, os.PathLike)):
        f = open(source, "rb")
        try:
            return _feed_generator_from_file(f, f.close)
        except BaseException:
            f.close()
            raise

    # read1 (when the object offers it) returns what already arrived
    # instead of blocking until a full chunk: what a pipe needs.
    read = getattr(source, "read1", source.read)
    raw_chunks = iter(lambda: read(STREAM_CHUNK_SIZE), b"")
    return _feed_generator_from_chunks(
        raw_chunks=raw_chunks,
        # Not the object's close: it was not opened here.
        closer=lambda: None,
        deadline=deadline,
    )


class ItemHandler:
    """
    Flattens one item's XML into {path: direct_text}: paths join tags
    with "/", siblings sharing a path get numbered (path, path_1, ...).
    Leaves always emit; parents only with non-whitespace direct text.
    An item of bare text emits under the separator's own tag name.
    """

    __slots__ = ("tags", "tags_appearances", "_stack")

    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.tags_appearances: dict[str, int] = {}
        # Stack frames: [key, direct_text, has_children]
        self._stack: list = []

    def startElement(self, tag: str, attributes: dict[str, str]) -> None:
        if not self._stack:
            # The root wrapper's key stays outside the numbering namespace:
            # a field sharing its name keeps its bare key.
            self._stack.append([tag, "", False])
            return

        self._stack[-1][2] = True
        if len(self._stack) > 1:
            path = f"{self._stack[-1][0]}/{tag}"
        else:
            # Direct child of the root wrapper: the path starts here.
            path = tag

        if path in self.tags_appearances:
            self.tags_appearances[path] += 1
            key = f"{path}_{self.tags_appearances[path]}"
        else:
            self.tags_appearances[path] = 0
            key = path

        if key in self.tags:
            # A field named like a numbered sibling already took this
            # key: number past it instead of overwriting its value.
            taken: str = key
            while key in self.tags:
                self.tags_appearances[path] += 1
                key = f"{path}_{self.tags_appearances[path]}"
            logger.warning(
                "Key collision on %r: <%s> numbered as %r instead. A field "
                "named like a numbered sibling makes the shape ambiguous.",
                taken, tag, key,
            )

        self._stack.append([key, "", False])

    def endElement(self, tag: str) -> None:
        key, direct_text, has_children = self._stack.pop()
        if not self._stack:
            # Root wrapper: emits only as a leaf (empty item edge).
            if has_children is False:
                self.tags[key] = direct_text
            return
        if has_children is False or direct_text.strip():
            self.tags[key] = direct_text

    def characters(self, content: str) -> None:
        if self._stack:
            self._stack[-1][1] += content


def _forbid_entity_constructs(*args: Any) -> NoReturn:
    """
    Reject entity declarations and external entity references: the item
    is discarded. Benign DOCTYPEs are allowed.
    """
    raise ValueError("entity declarations are forbidden")


def sanitize_item_bytes(item_string: bytes) -> tuple:
    """
    Return (bytes fit for expat, whether invalid ones were replaced).
    Fast path: already-valid utf-8 goes through untouched; only invalid
    bytes pay the decode/re-encode round trip, and say so.
    """
    try:
        item_string.decode("utf-8")
        return item_string, False
    except UnicodeDecodeError:
        return item_string.decode("utf-8", "replace").encode(), True


def parse_item(item_string: bytes) -> dict[str, Any] | None:
    decoded_string, _ = sanitize_item_bytes(item_string)
    return parse_sanitized_item(decoded_string)


def parse_sanitized_item(decoded_string: bytes) -> dict[str, Any] | None:
    handler = ItemHandler()
    parser = expat.ParserCreate()
    parser.buffer_text = True
    parser.EntityDeclHandler = _forbid_entity_constructs
    parser.UnparsedEntityDeclHandler = _forbid_entity_constructs
    parser.ExternalEntityRefHandler = _forbid_entity_constructs
    parser.StartElementHandler = handler.startElement
    parser.EndElementHandler = handler.endElement
    parser.CharacterDataHandler = handler.characters
    try:
        parser.Parse(decoded_string, True)
    except Exception as exc:
        logger.warning(
            "Discarding unparseable item (%s): %r",
            exc,
            decoded_string[:120],
        )
        return None

    for k in handler.tags.keys():
        handler.tags[k] = handler.tags[k].rstrip("\n")

    return handler.tags


NESTED_TEXT_KEY = "#text"

NUMBERED_SEGMENT_RE = re.compile(r"^(.+)_\d+$")


@dataclasses.dataclass(slots=True)
class Nested:
    """
    Output mode for StreamInterpreter: emit each item re-shaped with
    to_nested, forwarding force_list.
    """
    force_list: bool | Iterable[str] | None = None

    def __post_init__(self) -> None:
        # Frozen here: a generator is spent on the first item and
        # reshapes every one after it.
        if self.force_list is None or isinstance(self.force_list, bool):
            return
        if isinstance(self.force_list, str):
            self.force_list = frozenset({self.force_list})
            return
        try:
            paths = frozenset(self.force_list)
        except TypeError:
            raise TypeError(
                "force_list must be True, a path string, or an iterable "
                "of path strings."
            ) from None
        if not all(isinstance(path, str) for path in paths):
            raise TypeError("force_list paths must be strings.")
        self.force_list = paths


@dataclasses.dataclass(slots=True)
class Raw:
    """
    Output mode for StreamInterpreter: deliver each item as bytes
    instead of a parsed dict - the slice between its separator tags,
    exactly as the parser would receive it: after transparent gunzip
    and stream-to-utf-8 normalization, before any per-item repair,
    never re-serialized. The selection is unchanged: an item that
    does not parse is dropped.
    """


def to_nested(
    item: dict[str, Any],
    force_list: bool | Iterable[str] | None = None,
        ) -> dict[str, Any]:
    """
    Re-shape a flat {path: text} item into nested dicts (split on "/").
    Numbered siblings stay numbered, mixed-content text goes under
    "#text", and force_list paths ALWAYS come out as lists, in document
    order (force_list=True declares every path: all-lists shape).
    """
    declared_all = force_list is True
    if isinstance(force_list, str):
        # A lone string is one path, not an iterable of characters.
        force_list = {force_list}
    declared = (
        set(force_list)
        if force_list and force_list is not True
        else set()
    )
    nested: dict[str, Any] = {}
    seen_segments: dict[tuple, set] = {}
    list_elements: dict[tuple, dict[str, Any]] = {}

    def base_of(segment: str, level: tuple) -> str:
        # Numbered only if the unnumbered base was seen as a sibling.
        match = NUMBERED_SEGMENT_RE.match(segment)
        if match and match.group(1) in seen_segments.get(level, set()):
            return match.group(1)
        seen_segments.setdefault(level, set()).add(segment)
        return segment

    for path, value in item.items():
        segments = path.split("/")
        node = nested
        inst_prefix: tuple = ()
        base_prefix: tuple = ()
        for segment in segments[:-1]:
            base_segment = base_of(segment, base_prefix)
            inst_prefix += (segment,)
            base_prefix += (base_segment,)
            if declared_all or "/".join(base_prefix) in declared:
                elements = node.setdefault(base_segment, [])
                element = list_elements.get(inst_prefix)
                if element is None:
                    element = {}
                    list_elements[inst_prefix] = element
                    elements.append(element)
                node = element
            else:
                child = node.get(segment)
                if not isinstance(child, dict):
                    # Node already had direct text: it moves under #text.
                    child = {} if child is None else {NESTED_TEXT_KEY: child}
                    node[segment] = child
                node = child
        leaf = segments[-1]
        base_leaf = base_of(leaf, base_prefix)
        if declared_all or "/".join(base_prefix + (base_leaf,)) in declared:
            element = list_elements.get(inst_prefix + (leaf,))
            if element is not None:
                # Instance already created by its children: text -> #text.
                element[NESTED_TEXT_KEY] = value
            else:
                node.setdefault(base_leaf, []).append(value)
        else:
            existing = node.get(leaf)
            if isinstance(existing, dict):
                existing[NESTED_TEXT_KEY] = value
            else:
                node[leaf] = value
    return nested


# Hot path (one object per item): do not add runtime validation here.
@dataclasses.dataclass(slots=True)
class ParsedItem:
    content: bytes = b""
    parsed_content: dict[str, Any] | None = None
    # True when invalid utf-8 had to be replaced to parse this item.
    replaced: bool = False

    def parse_content(
        self,
        wrapper_open: bytes = b"<item>",
        wrapper_close: bytes = b"</item>",
    ) -> None:
        # The wrapper carries the feed's own separator name, so an item
        # made of bare text is keyed by the tag the feed really uses.
        decoded, self.replaced = sanitize_item_bytes(
            wrapper_open + self.content + wrapper_close
        )
        self.parsed_content: dict[str, Any] | None = parse_sanitized_item(
            decoded
        )


class ParsingState(Enum):
    SEEK_OPEN = "seek_open"
    SEEK_CLOSE = "seek_close"
    ITEM_PARSED = "item_parsed"
    EOF = "EOF"


# Sections whose content is markup, never items: a separator tag found
# inside one does not exist, and emitting it would fabricate a record.
SPECIAL_SECTION_PREFIXES = (b"<!--", b"<![CDATA[", b"<!DOCTYPE", b"<?")
LONGEST_SECTION_PREFIX: int = 9  # <![CDATA[ and <!DOCTYPE

# How long the scanner waits before deciding a section never was
# one. Generous: any real section closes well inside it.
MAX_SECTION_SIZE: int = 16 * 1024 * 1024

# One pass finds any section opener: "<!" (comment, CDATA, DOCTYPE) or
# "<?" (processing instruction).
SECTION_OPENER_PATTERN: re.Pattern = re.compile(rb"<[!?]")
# Inside a tag, only these bytes matter: quotes open and close runs
# where ">" is ordinary text.
TAG_STOP_PATTERN: re.Pattern = re.compile(rb"[>\"']")
# A closing tag has no attributes: quotes in it hide nothing.
TAG_END_PATTERN: re.Pattern = re.compile(rb">")
# Inside a DOCTYPE, brackets nest an internal subset, quotes hide
# everything, and comments and instructions are markup of their own.
DOCTYPE_STOP_PATTERN: re.Pattern = re.compile(rb"[<>\[\]\"']")
QUOTES: tuple = (b'"', b"'")
# XML says whitespace is exactly these four bytes.
XML_WHITESPACE: bytes = b" \t\r\n"
# What can never appear inside a name.
NAME_STOP: bytes = b" \t\r\n=/<>\"'"


# XML 1.0 names, spelled out in full.
_NAME_START: str = (
    ":A-Z_a-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u02ff\u0370-\u037d"
    "\u037f-\u1fff\u200c-\u200d\u2070-\u218f\u2c00-\u2fef"
    "\u3001-\ud7ff\uf900-\ufdcf\ufdf0-\ufffd\U00010000-\U000effff"
)
_NAME_REST: str = (
    _NAME_START + "0-9\u00b7\u0300-\u036f\u203f-\u2040."
)
XML_NAME_SHAPE: re.Pattern = re.compile(
    f"[{_NAME_START}][{_NAME_REST}\\-]*\\Z"
)


def is_xml_name(raw: bytes) -> bool:
    """Whether these bytes spell a name XML would accept."""
    if not raw:
        return False
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return XML_NAME_SHAPE.match(name) is not None


# XML 1.0 Char: everything a document may contain, and nothing else.
XML_CHAR_FORBIDDEN: re.Pattern = re.compile(
    "[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)


def is_xml_char(code: int) -> bool:
    """Whether a code point is one XML allows at all."""
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


# XML spells a numeric reference in ASCII digits only: every other
# digit-like character is not one, however Python classifies it.
DECIMAL_DIGITS: str = "0123456789"
HEX_DIGITS: str = "0123456789abcdefABCDEF"


def char_ref_code(digits: str, base: int) -> int:
    """
    The code point a numeric character reference names, or -1 when it
    names none. Accumulated digit by digit and given up past the last
    code point, so a reference of any length costs a few operations and
    never builds a huge integer out of feed bytes.
    """
    if not digits:
        return -1
    allowed: str = HEX_DIGITS if base == 16 else DECIMAL_DIGITS
    code: int = 0
    for char in digits:
        if char not in allowed:
            return -1
        code = code * base + int(char, base)
        if code > 0x10FFFF:
            return -1
    return code


def attvalue_is_well_formed(value: bytes) -> bool:
    """
    An attribute value is utf-8, holds no "<", carries only characters
    XML allows, and every "&" opens a reference that resolves to one:
    &name;, &#digits; or &#xhex;.
    """
    if b"<" in value:
        return False
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if XML_CHAR_FORBIDDEN.search(text) is not None:
        return False
    pos: int = 0
    while True:
        amp: int = text.find("&", pos)
        if amp == -1:
            return True
        semicolon: int = text.find(";", amp + 1)
        if semicolon == -1:
            return False
        body: str = text[amp + 1:semicolon]
        if body[:2] == "#x":
            if not is_xml_char(char_ref_code(body[2:], 16)):
                return False
        elif body[:1] == "#":
            if not is_xml_char(char_ref_code(body[1:], 10)):
                return False
        elif not is_xml_name(body.encode()):
            return False
        pos = semicolon + 1


def name_end(tag: bytes, pos: int, end: int) -> int:
    """End of the XML name starting at `pos`, or -1 when it is not one."""
    start: int = pos
    while pos < end and tag[pos:pos + 1] not in NAME_STOP:
        pos += 1
    if pos == start or not is_xml_name(tag[start:pos]):
        return -1
    return pos


def start_tag_is_well_formed(tag: bytes) -> bool:
    """
    A start tag as XML defines it: a name, then attributes separated by
    whitespace, each named once, quoted, and free of "<". Walked byte by
    byte, so a huge attribute value costs a scan and never backtracking.
    """
    end: int = len(tag) - 1  # the closing ">"
    if tag[end - 1:end] == b"/":
        end -= 1  # empty-element tag: "/>" carries no space between
    pos: int = name_end(tag, 1, end)
    if pos == -1:
        return False
    seen: set = set()
    while pos < end:
        spaced: bool = False
        while pos < end and tag[pos:pos + 1] in XML_WHITESPACE:
            pos += 1
            spaced = True
        if pos >= end:
            return True
        if not spaced:
            return False  # attributes must be separated
        stop: int = name_end(tag, pos, end)
        if stop == -1:
            return False
        name: bytes = tag[pos:stop]
        if name in seen:
            return False
        seen.add(name)
        pos = stop
        while pos < end and tag[pos:pos + 1] in XML_WHITESPACE:
            pos += 1
        if tag[pos:pos + 1] != b"=":
            return False
        pos += 1
        while pos < end and tag[pos:pos + 1] in XML_WHITESPACE:
            pos += 1
        quote: bytes = tag[pos:pos + 1]
        if quote not in QUOTES:
            return False
        close: int = tag.find(quote, pos + 1, end)
        if close == -1 or not attvalue_is_well_formed(tag[pos + 1:close]):
            return False
        pos = close + 1
    return True


def end_tag_is_well_formed(tag: bytes) -> bool:
    """An end tag carries a name, optional whitespace, and nothing else."""
    end: int = len(tag) - 1
    pos: int = name_end(tag, 2, end)
    if pos == -1:
        return False
    while pos < end and tag[pos:pos + 1] in XML_WHITESPACE:
        pos += 1
    return pos == end


@dataclasses.dataclass(slots=True)
class Sections:
    """
    What the scanner does with a markup section (comment, CDATA,
    processing instruction, DOCTYPE) that never closes. Past max_size,
    "text" re-reads the opener as content (bounded damage) and "markup"
    keeps discarding it (never a record, at the price of the rest).
    """
    max_size: int = MAX_SECTION_SIZE
    on_limit: str = "text"

    def __post_init__(self) -> None:
        if isinstance(self.max_size, bool)\
                or not isinstance(self.max_size, int):
            raise TypeError("max_size must be an int")
        if self.max_size <= 0:
            raise ValueError("max_size must be positive")
        if self.on_limit not in ("text", "markup"):
            raise ValueError(
                'on_limit must be "text" (past max_size the opener '
                'was not markup after all: it is re-read as content) or '
                '"markup" (keep discarding it, whatever follows).'
            )


@dataclasses.dataclass(slots=True)
class Tokenizer:
    feed_generator: FeedGeneratorT
    separator_tag: str
    buffer_size: int
    sections: Sections = dataclasses.field(default_factory=Sections)
    # Monotonic instant after which scanning stops, so a slow source
    # cannot outlive the run's budget between two items.
    deadline: float | None = None

    BUFFER: bytearray = dataclasses.field(default_factory=bytearray)
    # Consumption cursor: the pending bytes are BUFFER[POS:].
    # Consumed bytes compact away once per refill, never per item.
    POS: int = 0
    # Scanner cursor over what is already classified. It only moves
    # forward, so a huge section costs one pass, not one per refill.
    SCAN: int = 0

    # --- Internal variables. --- #
    PARSING_STATE: ParsingState = ParsingState.SEEK_OPEN
    # Terminator awaited while inside a section, with where it opened
    # and how long it may stay unterminated.
    SECTION: bytes | None = None
    SECTION_OPEN_AT: int = -1
    SECTION_LIMIT: int = 0
    SECTION_IS_DOCTYPE: bool = False
    # Resumable state of a DOCTYPE walk: subset depth, the quote it is
    # inside, and the terminator of an inner comment or instruction.
    DTD_DEPTH: int = 0
    DTD_QUOTE: bytes = b""
    DTD_INNER: bytes = b""
    # Set once the section outlived its limit and is being discarded
    # ("markup" policy): the warning is not repeated per refill.
    SECTION_GIVEN_UP: bool = False
    # Resumable walk of a tag whose ">" has not arrived: where it
    # opened, how far the walk got, and the quote it is inside.
    TAG_OPEN_AT: int = -1
    TAG_SCAN: int = 0
    TAG_QUOTE: bytes = b""
    TAG_STOP: re.Pattern = TAG_STOP_PATTERN
    TIMED_OUT: bool = False
    # Separator tags the feed spelled wrong (unquoted or repeated
    # attributes, leftovers in a closing tag): said once, counted.
    MALFORMED_TAGS: int = 0

    actual_item: ParsedItem = dataclasses.field(init=False)
    opening_tag_pattern: re.Pattern = dataclasses.field(init=False)
    closing_tag_pattern: re.Pattern = dataclasses.field(init=False)
    opening_head: bytes = dataclasses.field(init=False)
    closing_head: bytes = dataclasses.field(init=False)
    wrapper_open: bytes = dataclasses.field(init=False)
    wrapper_close: bytes = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        # Construction-time checks; hot-loop fields are never revalidated.
        if not isinstance(self.feed_generator, collections.abc.Generator):
            raise TypeError("feed_generator must be a generator")
        if not isinstance(self.separator_tag, str):
            raise TypeError("separator_tag must be a str")
        if not is_valid_xml_name(self.separator_tag):
            raise ValueError(
                f"separator_tag {self.separator_tag!r} is not a name an "
                "XML tag can have"
            )
        if isinstance(self.buffer_size, bool)\
                or not isinstance(self.buffer_size, int):
            raise TypeError("buffer_size must be an int")
        if self.buffer_size <= 0:
            raise ValueError("buffer_size must be positive")
        if not isinstance(self.sections, Sections):
            raise TypeError("sections must be a Sections(...) instance")
        if self.deadline is not None:
            if isinstance(self.deadline, bool)\
                    or not isinstance(self.deadline, (int, float)):
                raise TypeError(
                    "deadline must be a monotonic instant, or None"
                )
        if not isinstance(self.BUFFER, (bytes, bytearray)):
            raise TypeError("BUFFER must be bytes")
        self.BUFFER = bytearray(self.BUFFER)
        if not isinstance(self.POS, int):
            raise TypeError("POS must be an int")
        if not isinstance(self.PARSING_STATE, ParsingState):
            raise TypeError("PARSING_STATE must be a ParsingState")

        self.actual_item = ParsedItem()
        self.initialize_tag_patterns()

    def initialize_tag_patterns(self) -> None:
        opening_tag: bytes = self.separator_tag.encode()
        self.opening_head = b"<" + opening_tag
        self.closing_head = b"</" + opening_tag
        # Precomputed once: the per-item wrapper fed to expat.
        self.wrapper_open = b"<" + opening_tag + b">"
        self.wrapper_close = b"</" + opening_tag + b">"
        # Attribute values may hold ">" (only "<" and "&" are illegal
        # there): quoted runs are consumed whole.
        self.opening_tag_pattern: re.Pattern = re.compile(
            rb"<" + re.escape(opening_tag)
            + rb"(\s+(?:[^>\"']|\"[^\"]*\"|'[^']*')*)?>"
        )

        closing_tag: bytes = f"/{self.separator_tag}".encode()
        self.closing_tag_pattern: re.Pattern = re.compile(
            rb"<" + re.escape(closing_tag) + rb"(\s+[^>]*)?>"
        )

    def feed_origin_buffer(self) -> bool:
        """
        Fill BUFFER, always reading at least one chunk: it must be able
        to grow past buffer_size. Consumed bytes compact away here, once
        per refill, and chunks are joined in one pass.
        """
        if self.POS:
            del self.BUFFER[:self.POS]
            # Every absolute position shifts with the buffer.
            self.SCAN -= self.POS
            self.TAG_SCAN -= self.POS
            if self.SECTION_OPEN_AT >= 0:
                self.SECTION_OPEN_AT -= self.POS
            if self.TAG_OPEN_AT >= 0:
                self.TAG_OPEN_AT -= self.POS
            self.POS = 0
        try:
            content_present: bool = False
            while len(self.BUFFER) < self.buffer_size\
                    or content_present is False:
                if self.deadline is not None\
                        and time.monotonic() > self.deadline:
                    # Checked per chunk: one slow source must not
                    # outlive the budget inside a single refill.
                    self.TIMED_OUT = True
                    logger.warning(
                        "> XMLStreamer > Time budget exhausted while "
                        "reading the feed: stopping."
                    )
                    return False
                content = next(self.feed_generator)
                content_present = True
                self.BUFFER += content
        except StopIteration:
            ...

        if content_present is False:
            logger.debug(
                "Feed origin buffer: content present: %s", content_present
            )

        return content_present

    def classify_section(self, start: int) -> tuple | None:
        """
        Classify the opener at `start` as (terminator, header, limit).
        Returns None when it opens no section, and an empty tuple when
        the buffer is too short to tell yet.
        """
        buffer: bytearray = self.BUFFER
        limit: int = self.sections.max_size
        if buffer.startswith(b"<!--", start):
            return b"-->", 4, limit, False
        if buffer.startswith(b"<![CDATA[", start):
            return b"]]>", 9, limit, False
        if buffer.startswith(b"<?", start):
            return b"?>", 2, limit, False
        if buffer.startswith(b"<!DOCTYPE", start):
            # Its end is found by walking, not by matching a string:
            # quotes and the internal subset both hide ">" bytes.
            return b">", 9, limit, True
        if len(buffer) - start < LONGEST_SECTION_PREFIX:
            tail: bytes = bytes(buffer[start:])
            if any(p.startswith(tail) for p in SPECIAL_SECTION_PREFIXES):
                return ()
        return None

    def next_section_opener(self, start: int, endpos: int) -> int:
        """Absolute position of the first "<!" or "<?" in a range."""
        match = SECTION_OPENER_PATTERN.search(self.BUFFER, start, endpos)
        return -1 if match is None else match.start()

    def resume_doctype_scan(self, stop: int) -> int:
        """
        Continue the DOCTYPE walk from SECTION_OPEN_AT and return the
        ">" that closes it, or -1 while it has not arrived before
        `stop`. Quotes and internal-subset depth decide which ">"
        counts; the walk is resumable across refills.
        """
        buffer = self.BUFFER
        pos: int = self.SCAN
        while True:
            if self.DTD_INNER:
                # Inside a comment or instruction of the subset.
                end: int = buffer.find(self.DTD_INNER, pos, stop)
                if end == -1:
                    keep: int = stop - len(self.DTD_INNER) + 1
                    self.SCAN = keep if keep > pos else pos
                    return -1
                pos = end + len(self.DTD_INNER)
                self.DTD_INNER = b""
                continue
            if self.DTD_QUOTE:
                close: int = buffer.find(self.DTD_QUOTE, pos, stop)
                if close == -1:
                    self.SCAN = stop
                    return -1
                pos = close + 1
                self.DTD_QUOTE = b""
                continue
            match = DOCTYPE_STOP_PATTERN.search(buffer, pos, stop)
            if match is None:
                self.SCAN = stop
                return -1
            char: bytes = match.group()
            idx: int = match.start()
            if char == b">":
                if self.DTD_DEPTH <= 0:
                    self.SCAN = idx
                    return idx
                pos = idx + 1
            elif char == b"[":
                self.DTD_DEPTH += 1
                pos = idx + 1
            elif char == b"]":
                self.DTD_DEPTH -= 1
                pos = idx + 1
            elif char == b"<":
                if buffer.startswith(b"<!--", idx):
                    self.DTD_INNER = b"-->"
                    pos = idx + 4
                elif buffer.startswith(b"<?", idx):
                    self.DTD_INNER = b"?>"
                    pos = idx + 2
                elif stop - idx < 4 and b"<!--".startswith(
                    bytes(buffer[idx:stop])
                ):
                    # Split opener: decide when the rest arrives.
                    self.SCAN = idx
                    return -1
                else:
                    pos = idx + 1
            else:
                self.DTD_QUOTE = char
                pos = idx + 1

    def check_delimiter(self, tag: bytes, shape: Callable) -> None:
        """
        A separator tag is located lexically, not parsed: an attribute
        without quotes, a repeated one or leftovers in a closing tag do
        not stop the item (its content is intact and goes through expat
        as always), but they are never passed over in silence.
        """
        if shape(tag):
            return
        self.MALFORMED_TAGS += 1
        if self.MALFORMED_TAGS == 1:
            logger.warning(
                "Malformed separator tag %r: the item is delivered (its "
                "content parses), but the feed is not well-formed here.",
                bytes(tag[:80]),
            )

    def warn_unterminated_at_eof(self) -> None:
        """A section still open when the bytes run out lost its tail."""
        if self.SECTION is None or self.TIMED_OUT:
            return
        logger.warning(
            "Feed ended inside %s that never closed: %s bytes discarded.",
            self.BUFFER[self.SECTION_OPEN_AT:self.SECTION_OPEN_AT + 9],
            len(self.BUFFER) - self.SECTION_OPEN_AT,
        )

    def resume_tag_scan(self) -> int:
        """
        Continue the walk of the tag opened at TAG_OPEN_AT and return
        the position of the ">" that closes it, or -1 while it has not
        arrived. A ">" inside a quoted value closes nothing. The walk
        never re-reads a byte, so a huge tag costs one pass in total.
        """
        buffer: bytearray = self.BUFFER
        pos: int = self.TAG_SCAN
        while True:
            if self.TAG_QUOTE:
                close: int = buffer.find(self.TAG_QUOTE, pos)
                if close == -1:
                    self.TAG_SCAN = len(buffer)
                    return -1
                self.TAG_QUOTE = b""
                pos = close + 1
                continue
            match = self.TAG_STOP.search(buffer, pos)
            if match is None:
                self.TAG_SCAN = len(buffer)
                return -1
            char: bytes = match.group()
            if char == b">":
                self.TAG_SCAN = match.start()
                return match.start()
            self.TAG_QUOTE = char
            pos = match.start() + 1

    def pending_tag_tail(self, floor: int, head: bytes) -> int:
        """ Absolute position where a suffix starts that may still grow
        into the sought tag (`head`) or a section opener; discarding it
        would lose data. Returns len(BUFFER) when nothing needs keeping. """
        idx: int = self.BUFFER.rfind(b"<", floor)
        if idx == -1:
            return len(self.BUFFER)
        tail: bytes = bytes(self.BUFFER[idx:idx + len(head) + 1])
        if len(tail) <= len(head):
            # Too short to tell; a lone "<" may also open a section.
            if head.startswith(tail):
                self.TAG_OPEN_AT = -1
                return idx
            return len(self.BUFFER)
        if not tail.startswith(head) or not tail[-1:].isspace():
            # Not the sought tag: its quotes must not hide what follows.
            return len(self.BUFFER)
        if self.TAG_OPEN_AT != idx:
            # Start (or restart) the walk of this tag.
            self.TAG_OPEN_AT = idx
            self.TAG_SCAN = idx + 1
            self.TAG_QUOTE = b""
            self.TAG_STOP = (
                TAG_STOP_PATTERN if head == self.opening_head
                else TAG_END_PATTERN
            )
        if self.resume_tag_scan() != -1:
            # The last tag is complete and already failed to match.
            self.TAG_OPEN_AT = -1
            return len(self.BUFFER)
        return idx

    def advance_to(self, pattern: re.Pattern, head: bytes) -> re.Match | None:
        """
        Advance the scanner to the next match of `pattern` lying outside
        every special section, or return None when the buffer runs out
        and the caller must feed more. SCAN only moves forward: bytes
        already classified are never scanned again.
        """
        buffer: bytearray = self.BUFFER
        while True:
            if self.TAG_OPEN_AT >= 0:
                # Waiting on a tag whose ">" has not arrived: resuming
                # the walk is cheaper than re-matching the whole tag.
                if self.resume_tag_scan() == -1:
                    return None
                self.TAG_OPEN_AT = -1

            if self.SECTION is not None:
                # The window is measured from the opener over the
                # stream, so the decision cannot depend on chunking.
                window: int = self.SECTION_OPEN_AT + self.SECTION_LIMIT
                # Once given up on ("markup" policy) the section runs
                # until it closes or the feed ends, so does the search.
                stop = len(buffer)
                if not self.SECTION_GIVEN_UP and window < stop:
                    stop = window
                if self.SECTION_IS_DOCTYPE:
                    end: int = self.resume_doctype_scan(stop)
                else:
                    end = buffer.find(self.SECTION, self.SCAN, stop)
                if end == -1:
                    if len(buffer) < window:
                        # Keep the overlap: terminators straddle refills.
                        if not self.SECTION_IS_DOCTYPE:
                            self.SCAN = max(
                                self.SCAN,
                                len(buffer) - len(self.SECTION) + 1,
                            )
                        return None
                    if self.SECTION_GIVEN_UP:
                        # Already discarding: the terminator is simply
                        # not here yet.
                        return None
                    logger.warning(
                        "Unterminated %s after %s bytes: %s.",
                        bytes(buffer[
                            self.SECTION_OPEN_AT:self.SECTION_OPEN_AT + 9
                        ]),
                        self.SECTION_LIMIT,
                        "reading it as text, not markup"
                        if self.sections.on_limit == "text"
                        else "discarding whatever follows it",
                    )
                    if self.sections.on_limit == "text":
                        # Never was a section: re-read from past "<x".
                        self.SCAN = self.SECTION_OPEN_AT + 2
                        self.SECTION = None
                        self.SECTION_IS_DOCTYPE = False
                        self.DTD_DEPTH = 0
                        self.DTD_QUOTE = b""
                        self.DTD_INNER = b""
                        continue
                    # Still markup: keep discarding, never re-read, and
                    # look again right now over what is already here.
                    self.SECTION_GIVEN_UP = True
                    continue
                self.SCAN = end + len(self.SECTION)
                self.SECTION = None
                self.SECTION_IS_DOCTYPE = False
                self.SECTION_GIVEN_UP = False
                self.DTD_DEPTH = 0
                self.DTD_QUOTE = b""
                self.DTD_INNER = b""
                continue

            match = pattern.search(buffer, self.SCAN)
            endpos: int = match.start() if match is not None else len(buffer)
            opener: int = self.next_section_opener(self.SCAN, endpos)
            if opener == -1:
                if match is None:
                    self.SCAN = self.pending_tag_tail(self.SCAN, head)
                return match

            section = self.classify_section(opener)
            if section is None:
                # A "<!" that opens nothing (a stray one): step over it.
                self.SCAN = opener + 2
                continue
            if not section:
                # Opener split at the boundary: decide with more bytes.
                self.SCAN = opener
                return None
            terminator, header, limit, is_doctype = section
            if not is_doctype:
                stop = opener + limit
                if stop > len(buffer):
                    stop = len(buffer)
                end = buffer.find(terminator, opener + header, stop)
                if end != -1:
                    # Whole section already in the buffer: skip it
                    # without the resumable-section bookkeeping.
                    self.SCAN = end + len(terminator)
                    continue
            self.SECTION = terminator
            self.SECTION_OPEN_AT = opener
            self.SECTION_LIMIT = limit
            self.SECTION_IS_DOCTYPE = is_doctype
            self.SECTION_GIVEN_UP = False
            self.DTD_DEPTH = 0
            self.DTD_QUOTE = b""
            self.DTD_INNER = b""
            self.SCAN = opener + header

    def step(self) -> None:
        if self.PARSING_STATE == ParsingState.SEEK_OPEN:
            matching_pattern = self.advance_to(
                self.opening_tag_pattern, self.opening_head
            )

            if matching_pattern is None:
                if self.SECTION is not None and not self.SECTION_GIVEN_UP:
                    # Hold the opener: if it turns out to terminate
                    # nowhere, those bytes are read again as text.
                    self.POS = self.SECTION_OPEN_AT
                else:
                    # SCAN already sits at whatever must be kept (a
                    # pending tag): everything before it is discarded.
                    self.POS = self.SCAN
                feeding_result: bool = self.feed_origin_buffer()
                if feeding_result is False:
                    self.warn_unterminated_at_eof()
                    self.PARSING_STATE = ParsingState.EOF
            else:
                self.POS = self.SCAN = matching_pattern.end()
                # Self-closing separators (<item />) carry no content:
                # skip them and keep seeking the next opening tag.
                opening = matching_pattern.group(0)
                if matching_pattern.group(1):
                    self.check_delimiter(opening, start_tag_is_well_formed)
                if not opening[:-1].rstrip().endswith(b"/"):
                    self.PARSING_STATE = ParsingState.SEEK_CLOSE

        elif self.PARSING_STATE == ParsingState.SEEK_CLOSE:
            matching_pattern = self.advance_to(
                self.closing_tag_pattern, self.closing_head
            )

            if matching_pattern is None:
                # POS stays put: everything from it is item content.
                feeding_result = self.feed_origin_buffer()
                if feeding_result is False:
                    if not self.TIMED_OUT:
                        logger.warning(
                            "Feed ended inside an item: %s bytes discarded "
                            "(the download was cut, or the last item is "
                            "unclosed).",
                            len(self.BUFFER) - self.POS,
                        )
                    self.warn_unterminated_at_eof()
                    self.PARSING_STATE = ParsingState.EOF
            else:
                if matching_pattern.group(1):
                    self.check_delimiter(
                        matching_pattern.group(0), end_tag_is_well_formed
                    )
                content: bytes = bytes(
                    self.BUFFER[self.POS:matching_pattern.start()]
                )
                self.POS = self.SCAN = matching_pattern.end()

                # isspace() instead of strip(): no copy of the item.
                if not content or content.isspace():
                    # An empty item is not a record in either spelling:
                    # <item/> and <item></item> are the same nothing.
                    self.PARSING_STATE = ParsingState.SEEK_OPEN
                    return

                self.actual_item.content = content
                self.actual_item.parse_content(
                    self.wrapper_open, self.wrapper_close
                )

                parsed = self.actual_item.parsed_content
                if parsed is not None and (
                    not parsed
                    or (
                        len(parsed) == 1
                        and not parsed.get(self.separator_tag, "-").strip()
                    )
                ):
                    # Nothing but markup inside (a comment, say): the
                    # same nothing as <item/>, whatever the spelling.
                    self.PARSING_STATE = ParsingState.SEEK_OPEN
                    return

                self.PARSING_STATE = ParsingState.ITEM_PARSED

        elif self.PARSING_STATE == ParsingState.ITEM_PARSED:
            self.actual_item = ParsedItem()
            self.PARSING_STATE = ParsingState.SEEK_OPEN

        elif self.PARSING_STATE == ParsingState.EOF:
            logger.debug("> EOF:")

    def get_item(self) -> ParsedItem | None:
        self.step()
        while self.PARSING_STATE != ParsingState.ITEM_PARSED:
            self.step()

            if self.PARSING_STATE == ParsingState.EOF:
                return None

        return self.actual_item


class FeedRun(Generic[ItemT]):
    """
    One pass over one feed: its own source, tokenizer, counters and
    time budget. Iterating a StreamInterpreter opens one; runs of the
    same interpreter share nothing, so nested loops, retries and one
    interpreter per thread are all ordinary code.
    """

    def __init__(
        self,
        interpreter: "StreamInterpreter[ItemT]",
        tokenizer: Tokenizer,
        budget_start: float,
        budget: float | None,
    ) -> None:
        self.interpreter = interpreter
        self.tokenizer = tokenizer
        self.start_date = datetime.now()
        # Budget and clock are the run's own, taken when it opened. The
        # clock starts before acquisition: a slow download spends it.
        self._budget_start: float = budget_start
        self._budget: float | None = budget
        # What the run needed to open itself is fixed here; what it
        # applies per item (filter, output shape) is read when applied.
        self._stream_mode: bool = (
            interpreter.TRANSPORT.download_mode == "stream"
        )
        self._finished: bool = False
        # The funnel: total >= parsed >= delivered. total-parsed is
        # damage in the FEED, parsed-delivered the CALLER's filter.
        self.stats_total_items: int = 0
        self.stats_parsed_items: int = 0
        self.stats_delivered_items: int = 0
        # Items whose invalid utf-8 had to be replaced (encoding missed).
        self._replaced_items: int = 0

    def __iter__(self) -> Self:
        return self

    def check_terminate(self) -> bool:
        """Whether this run has spent its share of the time budget."""
        budget: float | None = self._budget
        if budget is None:
            return False
        running_time: float = time.monotonic() - self._budget_start
        if running_time > budget:
            logger.warning(
                "> XMLStreamer > Running time exceeded: %.1fs", running_time
            )
            return True
        return False

    def __next__(self) -> ItemT:
        if self._finished:
            raise StopIteration
        if self.check_terminate() is True:
            logger.warning("> XMLStreamer > Get item (RUNTIME EXCEEDED)")
            self.finish()

        if not self._stream_mode:
            return self.get_item()

        try:
            return self.get_item()
        except (requests.exceptions.RequestException, OSError) as exc:
            raise FeedInterruptedError(
                self.stats_delivered_items, exc
            ) from exc

    def close(self) -> None:
        """
        End the run, however far it got: release its source (socket or
        temp file), report the funnel and notify the interpreter. A run
        ends once, and a closed one is over: it delivers nothing more,
        whatever its buffer still held.
        """
        if self._finished:
            return
        self._finished = True
        # A generator that raises on close is closed all the same, so
        # reporting is not skipped and the error still surfaces.
        try:
            self.tokenizer.feed_generator.close()
        finally:
            self.interpreter.forget_run(self)
            logger.info(
                "XMLStreamer stats: total=%s parsed=%s delivered=%s%s%s",
                self.stats_total_items,
                self.stats_parsed_items,
                self.stats_delivered_items,
                f" replaced={self._replaced_items}"
                if self._replaced_items else "",
                f" malformed={self.tokenizer.MALFORMED_TAGS}"
                if self.tokenizer.MALFORMED_TAGS else "",
            )
            try:
                self.interpreter.run_finished(self)
            except Exception:
                # The hook reports: a report that fails is logged, never
                # propagated. Exception, not BaseException: a cancellation
                # is not a failed report.
                logger.error(
                    "run_finished(run) raised: the run ended anyway.",
                    exc_info=True,
                )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def finish(self) -> NoReturn:
        """End the run and end the iteration with it."""
        self.close()
        raise StopIteration

    def get_item(self) -> ItemT:
        interpreter = self.interpreter
        # The end is re-read after anything that reaches caller code
        # (the scan and its logging, the filter): once a run is over,
        # no counter moves again.
        while not self._finished\
                and self.tokenizer.PARSING_STATE != ParsingState.EOF:
            # Checked here too: a filter that drops everything never
            # reaches __next__, and the budget must still hold.
            if self.check_terminate() is True:
                self.finish()

            item: ParsedItem | None = self.tokenizer.get_item()
            if self._finished:
                break  # closed from inside the scan

            if item is not None:
                self.stats_total_items += 1
                if item.parsed_content is not None:
                    self.stats_parsed_items += 1
                if item.replaced:
                    self._replaced_items += 1
                    if self._replaced_items == 1:
                        # Counted before warning: this line reaches
                        # caller code, which can close the run from it.
                        logger.warning(
                            "Invalid utf-8 in the feed: those bytes are "
                            "replaced with U+FFFD. The encoding detected "
                            "from the head of the feed does not match "
                            "what arrived later."
                        )
                        if self._finished:
                            break

            if item is not None and item.parsed_content is not None:
                if interpreter.ITEM_FILTER is None\
                        or interpreter.ITEM_FILTER(item.parsed_content):
                    if self._finished:
                        break  # the filter closed the run: nothing more
                    self.stats_delivered_items += 1
                    # Representation runs AFTER the filter (filters see
                    # the flat dict in every output mode). The casts
                    # state what the __init__ overloads pinned: Raw
                    # yields bytes, everything else dicts.
                    if isinstance(interpreter.OUTPUT, Raw):
                        return cast(ItemT, item.content)
                    if interpreter.OUTPUT is not None:
                        return cast(ItemT, to_nested(
                            item.parsed_content,
                            interpreter.OUTPUT.force_list
                        ))
                    return cast(ItemT, item.parsed_content)

        self.finish()


class StreamInterpreter(Generic[ItemT]):
    # The overloads pin ItemT to what a run will yield, decided by
    # output=. Signatures must mirror the implementation below.
    @overload
    def __init__(
        self: "StreamInterpreter[dict[str, Any]]",
        url: str | None = None,
        separator_tag: str | None = None,
        item_filter: ItemFilterT = None,
        buffer_size: int = 1024 * 128,
        max_running_time: float | None = None,
        output: Nested | None = None,
        transport: Transport | None = None,
        sections: Sections | None = None,
        source: SourceT | None = None,
            ) -> None: ...

    @overload
    def __init__(
        self: "StreamInterpreter[bytes]",
        url: str | None = None,
        separator_tag: str | None = None,
        item_filter: ItemFilterT = None,
        buffer_size: int = 1024 * 128,
        max_running_time: float | None = None,
        output: Raw = ...,
        transport: Transport | None = None,
        sections: Sections | None = None,
        source: SourceT | None = None,
            ) -> None: ...

    def __init__(
        self,
        url: str | None = None,
        separator_tag: str | None = None,
        item_filter: ItemFilterT = None,
        buffer_size: int = 1024 * 128,  # 128Kb
        max_running_time: float | None = None,
        output: Nested | Raw | None = None,
        transport: Transport | None = None,
        sections: Sections | None = None,
        source: SourceT | None = None,
            ) -> None:
        # Every argument is checked here, at the call site, so a bad
        # value can never fail mid-iteration or yield zero items.
        if (url is None) == (source is None):
            raise ValueError(
                "pass exactly one of url or source: url reaches the "
                "network, source is a local path or a binary file-like "
                "object."
            )
        if url is not None and not isinstance(url, str):
            raise TypeError("url must be a str")
        if source is not None and not isinstance(source, (str, os.PathLike)):
            if isinstance(source, io.TextIOBase):
                raise TypeError(
                    "source must be binary: open the file with 'rb' "
                    "(text mode yields str, the tokenizer reads bytes)."
                )
            if not callable(getattr(source, "read", None)):
                raise TypeError(
                    "source must be a filesystem path or a binary "
                    "file-like object with a read() method."
                )
        if not isinstance(separator_tag, str):
            raise TypeError("separator_tag must be a str")
        if not separator_tag.strip():
            raise ValueError(
                "separator_tag must be a non-empty tag name: an empty "
                "separator can never match an item."
            )
        if not is_valid_xml_name(separator_tag):
            raise ValueError(
                f"separator_tag {separator_tag!r} is not a name an XML "
                "tag can have: it would match nothing and deliver zero "
                "items."
            )
        if isinstance(buffer_size, bool) or not isinstance(buffer_size, int):
            raise TypeError("buffer_size must be an int")
        if buffer_size <= 0:
            raise ValueError("buffer_size must be positive")
        if max_running_time is not None:
            if isinstance(max_running_time, bool)\
                    or not isinstance(max_running_time, (int, float)):
                raise TypeError(
                    "max_running_time must be a number of seconds, or "
                    "None for no time budget."
                )
            # NaN passes every comparison below (and infinity means no
            # budget at all): both would make it silently unreachable.
            if max_running_time != max_running_time\
                    or max_running_time == float("inf")\
                    or max_running_time <= 0:
                raise ValueError(
                    "max_running_time must be a positive number of "
                    "seconds, or None for no time budget."
                )
        if output is not None and not isinstance(output, (Nested, Raw)):
            raise TypeError(
                "output must be a Nested(...) or Raw() instance, or "
                "None for the default flat items."
            )
        if transport is not None and not isinstance(transport, Transport):
            raise TypeError(
                "transport must be a Transport(...) instance, or None "
                "for the defaults."
            )
        if transport is not None and source is not None:
            raise ValueError(
                "transport configures how a url is reached (agent, "
                "auth, proxy, timeout, download mode): it does not "
                "apply to a local source."
            )
        if sections is not None and not isinstance(sections, Sections):
            raise TypeError(
                "sections must be a Sections(...) instance, or None for "
                "the defaults."
            )
        if item_filter is not None and not callable(item_filter):
            raise TypeError(
                "item_filter must be a callable taking the flat item "
                "dict and returning a truthy value to keep it, or None."
            )

        self.BUFFER_SIZE = buffer_size
        self.URL: str | None = url
        self.SOURCE: SourceT | None = source
        self.separator_tag = separator_tag
        self.MAX_RUNNING_TIME = max_running_time
        self.OUTPUT = output
        self.TRANSPORT = transport if transport is not None else Transport()
        self.SECTIONS = sections if sections is not None else Sections()

        # Internal.
        self.ITEM_FILTER = item_filter

        # Wall clock for consumers to read; each run measures its own
        # budget on a monotonic clock, immune to NTP steps and DST.
        self.start_date = datetime.now()

        # Runs still holding a source, so close() can release them all,
        # and the guard that keeps that set from growing while it does.
        self._open_runs: "weakref.WeakSet[FeedRun[ItemT]]" = weakref.WeakSet()
        self._closing: bool = False
        # A caller's file-like object is consumed by its first run;
        # a url or a path is reopened instead.
        self._source_consumed: bool = False
        # The last run opened by iterating this interpreter, if any.
        # Counters live on the run; the properties below answer for it.
        self.last_run: FeedRun[ItemT] | None = None

    def __iter__(self) -> "FeedRun[ItemT]":
        """Open a run: its own source, tokenizer and counters."""
        if self._closing:
            # Reachable from an end-of-run hook: a teardown hands out no
            # new sources. Retry from the caller's loop.
            raise RuntimeError(
                "cannot open a run while the interpreter is closing"
            )
        self.start_date = datetime.now()
        budget_start: float = time.monotonic()
        deadline: float | None = (
            None if self.MAX_RUNNING_TIME is None
            else budget_start + self.MAX_RUNNING_TIME
        )
        # Acquisition happens here, so a feed that cannot be reached
        # fails where callers already expect it to.
        if self.SOURCE is None:
            # __init__ enforced exactly one of url/source: no source
            # here means url is a str.
            feed_generator = get_feed_generator(
                url=cast(str, self.URL),
                transport=self.TRANSPORT,
                deadline=deadline,
            )
        else:
            if not isinstance(self.SOURCE, (str, os.PathLike)):
                if self._source_consumed:
                    raise RuntimeError(
                        "a file-like source cannot be re-read: pass a "
                        "path or a fresh object."
                    )
                # Marked before reading: a failed acquisition may have
                # drained part of the object already.
                self._source_consumed = True
            feed_generator = get_source_feed_generator(
                source=self.SOURCE,
                deadline=deadline,
            )
        tokenizer = Tokenizer(
            feed_generator=feed_generator,
            separator_tag=self.separator_tag,
            buffer_size=self.BUFFER_SIZE,
            sections=self.SECTIONS,
            deadline=deadline,
        )
        run = FeedRun(self, tokenizer, budget_start, self.MAX_RUNNING_TIME)
        self.last_run = run
        self._open_runs.add(run)
        return run

    @property
    def stats_total_items(self) -> int:
        """Items the last run cut out of the stream."""
        return self.last_run.stats_total_items if self.last_run else 0

    @property
    def stats_parsed_items(self) -> int:
        """Of those, the ones expat could parse."""
        return self.last_run.stats_parsed_items if self.last_run else 0

    @property
    def stats_delivered_items(self) -> int:
        """Of those, the ones the filter kept and the caller received."""
        return self.last_run.stats_delivered_items if self.last_run else 0

    def forget_run(self, run: "FeedRun[ItemT]") -> None:
        """A run that released its source is no longer this one's to close."""
        self._open_runs.discard(run)

    def close(self) -> None:
        """Release every run this interpreter still has open."""
        if self._closing:
            # A sweep already owns this teardown: a nested close() recurses
            # per run and hides another source's failure inside a hook.
            return
        failures: list[BaseException] = []
        cancellation: BaseException | None = None
        # Held for the whole sweep: only one snapshot is taken, so
        # the set must not grow behind it.
        self._closing = True
        try:
            for run in list(self._open_runs):
                try:
                    run.close()
                except Exception as exc:
                    # One source that will not release must not strand
                    # the others: collect it and keep sweeping.
                    failures.append(exc)
                except BaseException as exc:
                    # Ctrl-C or SystemExit: deferred, never
                    # absorbed. What is left is bounded work, and it
                    # is re-raised below ahead of any mere failure.
                    if cancellation is None:
                        cancellation = exc
            # Reported only now, with every source out: a log handler is
            # caller code, and an interrupt from one must not strand the
            # rest. Inside the guard: reporting is teardown too.
            for failure in failures:
                try:
                    logger.warning("Releasing a run failed: %s", failure)
                except Exception:
                    # A reporter that fails must not become the answer close()
                    # gives: the release that failed is the news.
                    pass
                except BaseException as exc:
                    # Someone wants out: stop reporting. The FIRST
                    # cancellation is still the one re-raised.
                    if cancellation is None:
                        cancellation = exc
                    break
        finally:
            self._closing = False
        if cancellation is not None:
            raise cancellation
        if failures:
            raise failures[0]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def run_finished(self, run: "FeedRun[ItemT]") -> None:
        """
        End-of-run hook: called with the run that ended, once, after it
        released its source and logged its stats. Override it to alert;
        nothing needs to be raised or chained. It reports, so an
        exception raised here is logged at ERROR and goes no further.
        """
