#!/usr/bin/env python3

import shutil
import tempfile
import urllib.request
import xml.sax
import zlib
from contextlib import closing
from datetime import datetime

from urllib.parse import urlparse

import defusedxml.sax
import chardet

import requests

import attrs

from attrs_strict import type_validator

import re

from io import BufferedRandom

from enum import Enum

import codecs

from typing import Generator
from typing import Optional
from typing import Dict
from typing import Any

# import prettyprinter
# prettyprinter.install_extras()


# Define the type of the feed_generator
FeedGeneratorT = Generator[bytes, None, None]


USER_AGENT: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWeb"\
    "Kit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.3"


def stream_gzip_decompress(stream: bytes) -> Generator:
    """
    Decompressing GZIP from a stream of bytes.

    It returns a generator.
    """
    # offset 32 to skip the header
    dec = zlib.decompressobj(wbits=32 + zlib.MAX_WBITS)
    for chunk in stream:
        rv = dec.decompress(bytes(chunk))
        if rv:
            yield rv


def download_file(
    url: str,
    user_agent: str
        ) -> BufferedRandom:
    """
    Download file to temporary file.
    """
    print("Downloading file...")
    f = tempfile.TemporaryFile()

    parsed_url = urlparse(url)
    if parsed_url.scheme == "ftp":
        with closing(urllib.request.urlopen(url)) as r:
            shutil.copyfileobj(r, f)

    elif parsed_url.scheme in ["http", "https"]:
        headers = {"User-Agent": user_agent}
        with requests.get(url, stream=True, headers=headers) as r:
            r.raise_for_status()
            r.raw.decode_content = True
            shutil.copyfileobj(r.raw, f)
    else:
        raise Exception(f"Unknown scheme: {url}")

    return f


def is_gzip(
    temp_file: BufferedRandom
        ) -> bool:
    temp_file.seek(0)
    GZIP_MAGIC_NUMBER = b"\x1f\x8b"
    is_gzip_result: bool = temp_file.read(2) == GZIP_MAGIC_NUMBER
    temp_file.seek(0)

    return is_gzip_result


def decode_stream(generator, encoding):
    decoder = codecs.getincrementaldecoder(encoding)()
    buffer = b""
    for chunk in generator:
        buffer += chunk
        try:
            decoded_chunk = decoder.decode(buffer, final=False)
            yield decoded_chunk.encode("utf-8")
            buffer = b""
        except UnicodeDecodeError:
            # Se mantiene el buffer para el siguiente
            # # ciclo en caso de datos incompletos
            ...
    # Decodificar cualquier resto de datos después del último chunk.
    yield decoder.decode(buffer, final=True).encode("utf-8")


def map_encoding_to_utf(encoding: str) -> str:
    encoding = encoding.lower()
    utf8_encodings = ["utf-8", "utf8", "utf_8"]
    utf16_encodings = ["utf-16", "utf16", "utf_16", "utf-16le", "utf-16be"]

    if encoding in utf8_encodings:
        return "utf-8"
    elif encoding in utf16_encodings:
        return "utf-16"
    else:
        # Default to utf-8 if the encoding is not recognized
        return "utf-8"


def buffered_random_to_generator(
    buffered_random: BufferedRandom,
    chunk_size: int = 1024
        ) -> Generator[bytes, None, None]:
    while True:
        chunk = buffered_random.read(chunk_size)
        if not chunk:
            break
        yield chunk


def get_feed_generator(
    url: str,
    user_agent: str
        ) -> FeedGeneratorT:
    temp_file: BufferedRandom = download_file(
        url=url,
        user_agent=user_agent
    )
    temp_file.seek(0)

    is_gzip_result: bool = is_gzip(temp_file=temp_file)
    print("Is gzip:")
    print(is_gzip_result)

    if is_gzip_result is True:
        print("> Opening stream as GZIP...")
        feed_generator = stream_gzip_decompress(
            stream=temp_file
        )

        print("--- ENCODING ---")
        first_sample: bytes = b""
        try:
            first_sample: bytes = next(feed_generator)
        except Exception:
            ...

        encoding_info = chardet.detect(first_sample)
        detected_encoding = encoding_info["encoding"]
        print(f"Detected encoding: {detected_encoding}")

        mapped_encoding: str = map_encoding_to_utf(detected_encoding)
        print(f"Mapped encoding to: {mapped_encoding}")
        # mapped_encoding = detected_encoding

        # Restart the generator because we consumed one item from it
        temp_file.seek(0)
        feed_generator = stream_gzip_decompress(
            stream=temp_file
        )

        if mapped_encoding != "utf-8":
            # Wrap the generator to decode the stream
            # based on the mapped encoding.
            feed_generator = decode_stream(
                feed_generator,
                mapped_encoding
            )
    else:
        feed_generator = temp_file

    if isinstance(feed_generator, BufferedRandom):
        feed_generator = buffered_random_to_generator(
            buffered_random=feed_generator
        )

    return feed_generator


class JobHandler(xml.sax.handler.ContentHandler):
    def __init__(self):
        self.actual_tag: Optional[str] = None
        self.tags = {}
        self.tags_appearances = {}
        # self.tag_start = []

    def startElement(self, tag, attributes):
        # print("--- Start element ---")
        # print(f"Tag: {tag}")
        # self.tag_start.append(tag)
        # print(f"Attributes: {attributes}")
        if tag in self.tags_appearances.keys():
            self.tags_appearances[tag] += 1
            self.actual_tag = f"{tag}{self.tags_appearances[tag]}"
        else:
            self.tags_appearances[tag] = 0
            self.actual_tag = tag

        self.content_buffer = ""

    def endElement(self, tag):
        # print(f"--- End element ---")
        # print(f"End tag: {tag}")

        # print("CONTENT BUFFER")
        # print(self.content_buffer)
        self.tags[self.actual_tag] = self.content_buffer
        # prettyprinter.cpprint(self.tags)

    def characters(self, content):
        # print("> Characters.")
        # print(f"Content: {content}")
        self.content_buffer += content


def parse_item(item_string: str) -> Optional[Dict[str, Any]]:
    decoded_string: bytes = item_string.decode("utf-8", "replace").encode()

    handler = JobHandler()
    try:
        defusedxml.sax.parseString(
            string=decoded_string,
            handler=handler
        )
    except Exception:
        return None

    for k in handler.tags.keys():
        handler.tags[k] = handler.tags[k].rstrip("\n")

    return handler.tags


@attrs.define()
class ParsedItem:
    content: bytes = attrs.field(
        validator=type_validator(),
        default=b""
    )
    parsed_content: Optional[Dict[str, Any]] = attrs.field(
        validator=type_validator(),
        default=None
    )

    def parse_content(self):
        self.parsed_content: Optional[Dict[str, Any]] = parse_item(
            b"<item>" + self.content + b"</item>"
        )


class ParsingState(Enum):
    SEEK_OPEN: str = "seek_open"
    SEEK_CLOSE: str = "seek_close"
    ITEM_PARSED: str = "item_parsed"
    EOF: str = "EOF"


@attrs.define()
class Tokenizer:
    feed_generator: FeedGeneratorT = attrs.field(
        validator=type_validator()
    )
    separator_tag: str = attrs.field(
        validator=type_validator()
    )
    buffer_size: int = attrs.field(
        validator=type_validator()
    )

    BUFFER: bytes = attrs.field(
        validator=type_validator(),
        default=b""
    )

    actual_item: ParsedItem = attrs.field(
        validator=type_validator(),
        init=False
    )

    # --- Internal variables. --- #
    PARSING_STATE: ParsingState = attrs.field(
        validator=type_validator(),
        default=ParsingState.SEEK_OPEN
    )

    opening_tag_pattern: re.Pattern = attrs.field(
        validator=type_validator(),
        init=False
    )
    closing_tag_pattern: re.Pattern = attrs.field(
        validator=type_validator(),
        init=False
    )

    def __attrs_post_init__(self):
        self.actual_item = ParsedItem()
        self.initialize_tag_patterns()

    def initialize_tag_patterns(self):
        opening_tag: bytes = self.separator_tag.encode()
        self.opening_tag_pattern: str = re.compile(
            rb"<" + re.escape(opening_tag) + rb"(\s+[^>]*)?>"
        )

        closing_tag: str = f"/{self.separator_tag}".encode()
        self.closing_tag_pattern: str = re.compile(
            rb"<" + re.escape(closing_tag) + rb"(\s+[^>]*)?>"
        )

    def feed_origin_buffer(self) -> bool:
        """ Fill buffer. """
        try:
            content_present: bool = False
            while len(self.BUFFER) < self.buffer_size:
                content = next(self.feed_generator)
                content_present = True
                self.BUFFER += content
        except StopIteration:
            ...
            # print("Feed origin buffer result: False")

        if content_present is False:
            print(f"Feed origin buffer: content present: {content_present}")
        # print(content_present)

        return content_present

    def seek_opening_separator_tag(self) -> Optional[re.Match]:
        matching_pattern: Optional[re.Match] = self.opening_tag_pattern.search(
            self.BUFFER
        )
        return matching_pattern

    def seek_closing_separator_tag(self) -> Optional[re.Match]:
        matching_pattern: Optional[re.Match] = self.closing_tag_pattern.search(
            self.BUFFER
        )
        return matching_pattern

    def step(self):
        if self.PARSING_STATE == ParsingState.SEEK_OPEN:
            # print("> SEEK OPEN:")
            matching_pattern: Optional[re.Match] =\
                self.seek_opening_separator_tag()
            # prettyprinter.cpprint(matching_pattern)

            if matching_pattern is None:
                self.BUFFER = b""
                feeding_result: bool = self.feed_origin_buffer()
                if feeding_result is False:
                    self.PARSING_STATE = ParsingState.EOF
            else:
                self.BUFFER = self.BUFFER[matching_pattern.end():]
                self.PARSING_STATE = ParsingState.SEEK_CLOSE

        elif self.PARSING_STATE == ParsingState.SEEK_CLOSE:
            # print("> SEEK CLOSE:")
            matching_pattern: Optional[re.Match] =\
                self.seek_closing_separator_tag()
            # prettyprinter.cpprint(matching_pattern)

            if matching_pattern is None:
                feeding_result: bool = self.feed_origin_buffer()
                if feeding_result is False:
                    self.PARSING_STATE = ParsingState.EOF
            else:
                self.actual_item.content = self.BUFFER[
                    :matching_pattern.start()
                ]
                self.actual_item.parse_content()

                self.BUFFER = self.BUFFER[matching_pattern.end():]

                self.PARSING_STATE = ParsingState.ITEM_PARSED

        elif self.PARSING_STATE == ParsingState.ITEM_PARSED:
            # print("> ITEM_PARSED:")
            self.actual_item = ParsedItem()
            self.PARSING_STATE = ParsingState.SEEK_OPEN

        elif self.PARSING_STATE == ParsingState.EOF:
            print("> EOF:")

    def get_item(self) -> Optional[ParsedItem]:
        self.step()
        while self.PARSING_STATE != ParsingState.ITEM_PARSED:
            self.step()

            if self.PARSING_STATE == ParsingState.EOF:
                return None

        return self.actual_item


class StreamInterpreter:
    def __init__(
        self,
        url: str,
        separator_tag: str,
        item_filter: Optional[Any] = None,
        buffer_size: int = 1024 * 128,  # 128Kb
        max_running_time: Optional[int] = None,
        user_agent: str = USER_AGENT,
            ):
        self.BUFFER_SIZE = buffer_size
        self.URL: str = url
        self.separator_tag = separator_tag

        self.MAX_RUNNING_TIME = max_running_time

        self.USER_AGENT = user_agent

        # Internal.
        self.ITEM_FILTER = item_filter

        self.filter_parsed_item_func = None

        self.stats_total_items: int = 0
        self.stats_parsed_items: int = 0
        self.stats_filtered_items: int = 0

        self.start_date = datetime.now()

    def __iter__(self):
        self.tokenizer = Tokenizer(
            feed_generator=get_feed_generator(
                url=self.URL,
                user_agent=self.USER_AGENT
            ),
            separator_tag=self.separator_tag,
            buffer_size=1024 * 128
        )

        return self

    def __next__(self):
        if self.check_terminate() is True:
            print("> XMLStreamer > Get item (RUNTIME EXCEEDED)")
            self.raise_stop_iteration()

        return self.get_item()

    def raise_stop_iteration(self):
        print(f"XMLSTREAMER STATS ITEMS PARSED: {self.stats_filtered_items}")
        raise StopIteration

    def check_terminate(self) -> bool:
        actual_date = datetime.now()
        running_time = actual_date - self.start_date
        if self.MAX_RUNNING_TIME is not None:
            if running_time.seconds > self.MAX_RUNNING_TIME:
                print(f"> XMLStreamer > Running time exceeded: {running_time}")
                return True
        return False

    def get_item(self):
        # print("--- GET ITEM ---")
        while self.tokenizer.PARSING_STATE != ParsingState.EOF:
            item: Optional[ParsedItem] = self.tokenizer.get_item()

            if item is not None:
                self.stats_total_items += 1

            if item is not None and item.parsed_content is not None:
                self.stats_parsed_items += 1

                # print("-> Item has contnet and will be evaluated.")
                if self.ITEM_FILTER is not None and\
                        self.filter_parsed_item_func is not None:
                    # print("-> Filter enabled.")
                    filtered_item: Optional[ParsedItem] =\
                        self.filter_parsed_item_func(
                            ITEM_FILTER=self.ITEM_FILTER,
                            parsed_item=item
                        )
                else:
                    filtered_item = item

                if filtered_item is not None:
                    # prettyprinter.cpprint(filtered_item.parsed_content)

                    self.stats_filtered_items += 1
                    return filtered_item.parsed_content
                else:
                    ...
                    # print("FILTERED ITEM!!!")

        self.raise_stop_iteration()
