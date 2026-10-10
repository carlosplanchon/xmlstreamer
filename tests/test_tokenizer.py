import logging
import time

import pytest

from feedlib import build_feed, chunked, drain

from xmlstreamer import Sections, Tokenizer, attvalue_is_well_formed


def make_tokenizer(
    data: bytes,
    buffer_size: int = 4096,
    chunk_size: int = 32,
    **kwargs,
) -> Tokenizer:
    return Tokenizer(
        feed_generator=chunked(data, chunk_size),
        separator_tag="item",
        buffer_size=buffer_size,
        **kwargs,
    )


def titles(items) -> list:
    return [item.parsed_content["t"] for item in items]


def test_construction_validates_argument_types():
    # Bad constructor argument types raise TypeError.
    def gen():
        yield b""

    with pytest.raises(TypeError):
        Tokenizer(feed_generator=gen(), separator_tag=123, buffer_size=64)
    with pytest.raises(TypeError):
        Tokenizer(feed_generator=gen(), separator_tag="item", buffer_size="64")
    with pytest.raises(TypeError):
        Tokenizer(feed_generator=[b"x"], separator_tag="item", buffer_size=64)


def test_construction_rejects_nonpositive_buffer_size():
    def gen():
        yield b""

    with pytest.raises(ValueError):
        Tokenizer(feed_generator=gen(), separator_tag="item", buffer_size=0)
    with pytest.raises(ValueError):
        Tokenizer(feed_generator=gen(), separator_tag="item", buffer_size=-1)


def test_construction_rejects_empty_separator_tag():
    def gen():
        yield b""

    with pytest.raises(ValueError):
        Tokenizer(feed_generator=gen(), separator_tag="", buffer_size=64)
    with pytest.raises(ValueError):
        Tokenizer(feed_generator=gen(), separator_tag="  ", buffer_size=64)


def test_yields_every_item():
    data = build_feed([{"t": "one"}, {"t": "two"}, {"t": "three"}])
    items = drain(make_tokenizer(data))
    assert titles(items) == ["one", "two", "three"]
    assert items[0].content == b"<t>one</t>"


def test_separator_tag_with_attributes():
    data = b'<feed><item id="7" kind="entry"><t>x</t></item></feed>'
    items = drain(make_tokenizer(data))
    assert titles(items) == ["x"]


def test_prefix_tag_names_not_confused():
    # Separator "item" must not match <items> or </items>.
    data = b"<items><item><t>x</t></item></items>"
    items = drain(make_tokenizer(data))
    assert titles(items) == ["x"]


def test_many_items_across_small_chunks_and_buffer():
    data = build_feed([{"t": f"entry{i}"} for i in range(10)])
    items = drain(make_tokenizer(data, buffer_size=64, chunk_size=3))
    assert titles(items) == [f"entry{i}" for i in range(10)]


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"<feed><other>hello</other></feed>",
        b"<feed><item><t>x</t>",  # unclosed item at end of stream
    ],
)
def test_feeds_without_complete_items_yield_nothing(data):
    assert drain(make_tokenizer(data, buffer_size=64, chunk_size=8)) == []


def test_self_closing_separator_is_skipped():
    # Self-closing separators carry no content and are skipped.
    data = b"<feed><item/><item><t>x</t></item></feed>"
    items = drain(make_tokenizer(data))
    assert titles(items) == ["x"]


def test_self_closing_with_space_does_not_swallow_next_item():
    data = (
        b"<feed><item />"
        b"<item><t>next</t></item>"
        b"<item><t>last</t></item></feed>"
    )
    items = drain(make_tokenizer(data))
    assert titles(items) == ["next", "last"]


def test_self_closing_with_attributes_is_skipped():
    data = (
        b'<feed><item id="1" empty="true" />'
        b"<item><t>real</t></item></feed>"
    )
    items = drain(make_tokenizer(data))
    assert titles(items) == ["real"]


def test_self_closing_with_inner_whitespace_is_skipped():
    data = b"<feed><item /  ><item><t>real</t></item></feed>"
    items = drain(make_tokenizer(data))
    assert titles(items) == ["real"]


def test_malformed_item_yields_unparsed_content():
    data = (
        b"<feed>"
        b"<item><t>a</t></item>"
        b"<item><t>b</item>"  # unclosed <t>: parse fails for this item
        b"<item><t>c</t></item>"
        b"</feed>"
    )
    items = drain(make_tokenizer(data))
    assert [item.parsed_content for item in items] == [{"t": "a"}, None, {"t": "c"}]


def test_item_larger_than_buffer_size():
    big = "X" * 1000
    data = build_feed([{"d": big}, {"d": "small"}])
    items = drain(make_tokenizer(data, buffer_size=256, chunk_size=128))
    assert [item.parsed_content["d"] for item in items] == [big, "small"]


def test_opening_tag_straddling_buffer_boundary():
    def feed():
        yield b"A" * 60 + b"<ite"  # fills the 64-byte buffer exactly
        yield b"m><t>one</t></item><item><t>two</t></item>"

    tokenizer = Tokenizer(feed_generator=feed(), separator_tag="item", buffer_size=64)
    assert titles(drain(tokenizer)) == ["one", "two"]


def test_opening_tag_inside_comment_is_ignored():
    data = (
        b"<feed>"
        b"<!-- plantilla: <item><t>fake</t></item> -->"
        b"<item><t>real</t></item>"
        b"</feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["real"]


def test_opening_tag_inside_cdata_is_ignored():
    data = (
        b"<feed>"
        b"<![CDATA[ <item><t>fantasma</t></item> ]]>"
        b"<item><t>real</t></item>"
        b"</feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["real"]


def test_closing_tag_inside_cdata_stays_in_content():
    data = (
        b"<feed>"
        b"<item><d><![CDATA[one </item> two]]></d></item>"
        b"<item><t>b</t></item>"
        b"</feed>"
    )
    items = drain(make_tokenizer(data))
    assert [item.parsed_content for item in items] == [
        {"d": "one </item> two"},
        {"t": "b"},
    ]


def test_closing_tag_inside_comment_stays_in_item():
    data = (
        b"<feed>"
        b"<item><t>a</t><!-- ojo: </item> --></item>"
        b"<item><t>b</t></item>"
        b"</feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["a", "b"]


def test_cdata_section_larger_than_buffer():
    filler = b"x" * 3000
    data = (
        b"<feed>"
        b"<item><d><![CDATA[" + filler + b" </item> " + filler + b"]]></d></item>"
        b"<item><t>b</t></item>"
        b"</feed>"
    )
    items = drain(make_tokenizer(data, buffer_size=256, chunk_size=128))
    assert len(items) == 2
    assert items[0].parsed_content["d"].count("x") == 6000
    assert items[1].parsed_content == {"t": "b"}


def test_comment_opener_split_across_chunks():
    def feed():
        yield b"<feed>" + b"A" * 55 + b"<!-"
        yield b"- <item><t>fake</t></item> -->"
        yield b"<item><t>real</t></item></feed>"

    tokenizer = Tokenizer(feed_generator=feed(), separator_tag="item", buffer_size=64)
    assert titles(drain(tokenizer)) == ["real"]


def test_stray_lt_and_quote_in_text_never_swallow_next_item():
    # "<b'c" in text is no tag: its quote must not hide what follows,
    # wherever a refill splits it.
    data = b"<feed><item><t>a<b'c</t></item><item><t>d</t></item></feed>"
    for buffer_size in (1, 16, 4096):
        for chunk_size in (1, 2, 3, 5, 7, 64):
            items = drain(make_tokenizer(
                data, buffer_size=buffer_size, chunk_size=chunk_size
            ))
            assert [item.parsed_content for item in items] == [
                None, {"t": "d"},
            ], (buffer_size, chunk_size)


def test_comment_with_quote_split_at_boundary_keeps_next_item():
    # A "<" left at a boundary that grows into "<!--" opens a comment;
    # an apostrophe inside it quotes nothing.
    data = (
        b"<feed><item><t>x</t></item><!-- don't <item><t>fake</t></item> -->"
        b"<item><t>real</t></item></feed>"
    )
    for buffer_size in (1, 16, 4096):
        for chunk_size in (1, 2, 3, 5, 7, 64):
            items = drain(make_tokenizer(
                data, buffer_size=buffer_size, chunk_size=chunk_size
            ))
            assert titles(items) == ["x", "real"], (buffer_size, chunk_size)


def test_unterminated_comment_reaches_eof_cleanly():
    data = b"<feed><item><t>a</t></item><!-- sin cierre <item><t>x</t></item>"
    items = drain(make_tokenizer(data, buffer_size=64, chunk_size=16))
    assert titles(items) == ["a"]


def test_unterminated_cdata_in_item_reaches_eof_cleanly():
    data = b"<feed><item><d><![CDATA[abierto </item>"
    assert drain(make_tokenizer(data, buffer_size=64, chunk_size=16)) == []


# --- Sections: comments, CDATA, processing instructions, DOCTYPE --- #

def test_processing_instruction_never_yields_a_ghost_item():
    # A PI is markup, not content: items written inside one do not
    # exist, and inventing them would fabricate a record.
    data = (
        b"<feed><item><t>real</t></item>"
        b"<?pi <item><t>ghost</t></item> ?>"
        b"<item><t>real2</t></item></feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["real", "real2"]


def test_processing_instruction_with_opening_tag_only():
    # An opening separator tag alone inside a processing
    # instruction: markup, so the next item arrives intact.
    data = (
        b"<feed><item><t>one</t></item>"
        b'<?php echo "<item>"; ?>'
        b"<item><t>two</t></item></feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["one", "two"]


def test_doctype_internal_subset_never_yields_a_ghost_item():
    data = (
        b'<!DOCTYPE feed [<!ENTITY ghost "<item><t>ghost</t></item>">]>'
        b"<feed><item><t>real</t></item></feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["real"]


def test_plain_doctype_without_subset():
    data = (
        b'<!DOCTYPE feed SYSTEM "feed.dtd">'
        b"<feed><item><t>real</t></item></feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["real"]


@pytest.mark.parametrize("chunk_size", [1, 5, 4096])
def test_sections_split_across_chunks(chunk_size):
    # Openers and terminators cut at any boundary must still be seen.
    data = (
        b"<feed><!-- <item>c</item> -->"
        b"<?pi <item>p</item> ?>"
        b"<![CDATA[<item>d</item>]]>"
        b"<item><t>real</t></item></feed>"
    )
    items = drain(make_tokenizer(data, buffer_size=16, chunk_size=chunk_size))
    assert titles(items) == ["real"]


def test_unterminated_processing_instruction_is_read_as_text(caplog):
    # A stray "<?" in text (pasted code, word-processor markup) is not
    # a section: it must not swallow the rest of the feed.
    data = (
        b"<feed><item><t>one</t></item>"
        b"<?php never closed" + b"x" * 4096 +
        b"<item><t>two</t></item></feed>"
    )
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(make_tokenizer(data, sections=Sections(max_size=512)))
    assert titles(items) == ["one", "two"]
    assert "nterminated" in caplog.text


@pytest.mark.parametrize("opener", [b"<?", b"<!--", b"<![CDATA["])
def test_a_section_opened_inside_an_item_is_not_contained_damage(opener):
    # The counterpart of item isolation, and its documented limit: a
    # corruption that opens markup and never closes it is not damage
    # inside one item. Everything from the opener is inside the section
    # once the feed ends there, neighbours included - re-reading it
    # would be inventing records out of markup, which is the one thing
    # this library never does. The size-bounded case (a feed that keeps
    # going) is the "read_as_text" test above.
    data = (
        b"<feed><item><t>one</t></item>"
        b"<item><t>" + opener + b"</t></item>"
        b"<item><t>three</t></item></feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["one"]


def test_unterminated_comment_is_bounded_and_loud(caplog):
    data = (
        b"<feed><item><t>one</t></item>"
        b"<!-- never closed" + b"x" * 4096 +
        b"<item><t>two</t></item></feed>"
    )
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(make_tokenizer(data, sections=Sections(max_size=512)))
    assert titles(items) == ["one", "two"]
    assert "nterminated" in caplog.text


# --- Attribute values may hold ">" (only "<" and "&" are illegal) --- #

def test_separator_attribute_may_contain_gt_with_text():
    data = b'<feed><item note="1 > 0">actual</item></feed>'
    items = drain(make_tokenizer(data))
    assert [i.parsed_content for i in items] == [{"item": "actual"}]


def test_separator_attribute_may_contain_gt_with_children():
    data = b"<feed><item note='a > b'><t>x</t><u>y</u></item></feed>"
    items = drain(make_tokenizer(data))
    assert [i.parsed_content for i in items] == [{"t": "x", "u": "y"}]


def test_separator_attribute_may_contain_markup_lookalikes():
    data = (
        b'<feed><item note="<!-- ]]> ?>"><t>x</t></item></feed>'
    )
    assert titles(drain(make_tokenizer(data))) == ["x"]


def test_large_section_scan_is_linear():
    # Re-searching a growing buffer from the section start on every
    # refill is quadratic and blows well past this budget at this size;
    # the bound is coarse on purpose, not a micro-benchmark.
    body = (
        b"<feed><!--" + b"x" * (24 * 1024 * 1024) + b"-->"
        b"<item><t>real</t></item></feed>"
    )
    start = time.perf_counter()
    items = drain(
        make_tokenizer(body, chunk_size=65536,
                       sections=Sections(max_size=64 * 1024 * 1024))
    )
    elapsed = time.perf_counter() - start
    assert titles(items) == ["real"]
    assert elapsed < 3.0, f"scan took {elapsed:.1f}s: rescanning?"


@pytest.mark.parametrize(
    "empty",
    [b"<item/>", b"<item />", b"<item></item>", b"<item>   </item>"],
)
def test_empty_items_carry_no_record_in_any_spelling(empty):
    # <item/> and <item></item> are the same non-record: neither may
    # reach the consumer, and neither may crash the run.
    data = b"<feed>" + empty + b"<item><t>real</t></item></feed>"
    assert titles(drain(make_tokenizer(data))) == ["real"]


def test_text_only_items_are_keyed_by_the_separator():
    # An item with no child elements is all text: the key is the tag
    # the feed actually uses, not the parser's internal wrapper name.
    data = b"<catalog><isbn>978-0-1</isbn><isbn>978-0-2</isbn></catalog>"
    tokenizer = Tokenizer(
        feed_generator=chunked(data, 16), separator_tag="isbn",
        buffer_size=64,
    )
    assert [i.parsed_content for i in drain(tokenizer)] == [
        {"isbn": "978-0-1"},
        {"isbn": "978-0-2"},
    ]


def test_text_only_items_with_item_separator_are_unchanged():
    data = b"<feed><item>plain</item></feed>"
    items = drain(make_tokenizer(data))
    assert [i.parsed_content for i in items] == [{"item": "plain"}]


def test_field_named_item_is_untouched_by_the_wrapper():
    # A field literally called "item" keeps its bare key, whatever
    # the separator tag is.
    data = b"<catalog><isbn><item>x</item><code>y</code></isbn></catalog>"
    tokenizer = Tokenizer(
        feed_generator=chunked(data, 8), separator_tag="isbn",
        buffer_size=64,
    )
    assert [i.parsed_content for i in drain(tokenizer)] == [
        {"item": "x", "code": "y"}
    ]


def test_separator_with_namespace_prefix_wraps_correctly():
    data = b"<catalog><ns:isbn>978-0-1</ns:isbn></catalog>"
    tokenizer = Tokenizer(
        feed_generator=chunked(data, 8), separator_tag="ns:isbn",
        buffer_size=64,
    )
    assert [i.parsed_content for i in drain(tokenizer)] == [
        {"ns:isbn": "978-0-1"}
    ]


def test_construction_rejects_impossible_xml_names():
    def gen():
        yield b""

    with pytest.raises(ValueError):
        Tokenizer(feed_generator=gen(), separator_tag="a/b", buffer_size=64)


# --- The scan must not depend on how the bytes arrive --- #

@pytest.mark.parametrize("buffer_size", [8, 16, 32, 4096, 131072])
@pytest.mark.parametrize("chunk_size", [1, 8, 4096])
def test_attribute_with_gt_survives_any_buffer(buffer_size, chunk_size):
    # A ">" inside a quoted value is not the end of the tag, at any
    # buffer size: the result cannot depend on where the reads fall.
    data = (
        b'<feed><item note="1 > 0 and 2 > 1 and 3 > 2">actual</item></feed>'
    )
    items = drain(make_tokenizer(data, buffer_size, chunk_size))
    assert [i.parsed_content for i in items] == [{"item": "actual"}]


@pytest.mark.parametrize("buffer_size", [8, 64, 4096])
def test_long_attribute_value_survives_small_buffers(buffer_size):
    value = b"x > y " * 500
    data = b'<feed><item note="' + value + b'"><t>real</t></item></feed>'
    items = drain(make_tokenizer(data, buffer_size, chunk_size=7))
    assert titles(items) == ["real"]


def test_huge_tag_scan_is_linear():
    # Re-running the match over the whole pending tag on every refill
    # is quadratic; the walk resumes where it stopped.
    data = (
        b'<feed><item note="' + b"x" * (12 * 1024 * 1024)
        + b'"><t>real</t></item></feed>'
    )
    start = time.perf_counter()
    items = drain(make_tokenizer(data, buffer_size=65536, chunk_size=65536))
    elapsed = time.perf_counter() - start
    assert titles(items) == ["real"]
    assert elapsed < 3.0, f"scan took {elapsed:.1f}s: rescanning?"


# --- DOCTYPE: quotes and bracket depth decide where it ends --- #

def test_doctype_entity_value_holding_the_subset_terminator():
    # "]>" inside a quoted entity value does not end the DOCTYPE, so
    # what follows it inside the subset is still markup, not a record.
    data = (
        b'<!DOCTYPE feed [<!ENTITY x "]> <item><t>ghost</t></item>">]>'
        b"<feed><item><t>real</t></item></feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["real"]


def test_doctype_subset_closing_with_whitespace():
    data = (
        b'<!DOCTYPE feed [<!ENTITY x "y"> ]  >'
        b"<feed><item><t>real</t></item></feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["real"]


def test_doctype_with_gt_inside_a_quoted_system_id():
    data = (
        b'<!DOCTYPE feed SYSTEM "weird>name.dtd">'
        b"<feed><item><t>real</t></item></feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["real"]


def test_doctype_nested_brackets():
    data = (
        b'<!DOCTYPE feed [<!ENTITY x "[nested]"> <!ENTITY y "z">]>'
        b"<feed><item><t>real</t></item></feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["real"]


# --- Unterminated sections: the policy is the caller's --- #

def test_unterminated_section_read_as_text_by_default(caplog):
    ghost = b"<?target " + b"y" * 300 + b" <item><t>inside</t></item>"
    data = b"<feed><item><t>real</t></item>" + ghost + b"</feed>"
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(
            make_tokenizer(data, sections=Sections(max_size=128))
        )
    # Past the limit it was never markup: its content is ordinary text,
    # so the item written inside it is a real one.
    assert titles(items) == ["real", "inside"]
    assert "nterminated" in caplog.text


def test_unterminated_section_kept_as_markup_when_asked(caplog):
    ghost = b"<?target " + b"y" * 300 + b" <item><t>inside</t></item>"
    data = b"<feed><item><t>real</t></item>" + ghost + b"</feed>"
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(
            make_tokenizer(
                data,
                sections=Sections(max_size=128, on_limit="markup"),
            )
        )
    # Nothing after the opener is ever re-read: no record can come out
    # of it, at the price of everything that followed.
    assert titles(items) == ["real"]
    assert "nterminated" in caplog.text


def test_valid_section_larger_than_the_default_limit_is_still_markup():
    # The default limit is generous on purpose: a real processing
    # instruction of any plausible size stays markup.
    pi = b"<?target " + b"x" * 200_000 + b" <item><t>ghost</t></item> ?>"
    data = b"<feed><item><t>real</t></item>" + pi + b"</feed>"
    items = drain(make_tokenizer(data, buffer_size=8192, chunk_size=4096))
    assert titles(items) == ["real"]


@pytest.mark.parametrize("chunk_size", [1, 97, 4096])
def test_limit_decision_does_not_depend_on_chunking(chunk_size):
    # The window is measured from the opener over the stream, not over
    # whatever happens to be buffered.
    pi = b"<?target " + b"x" * 300 + b" ?>"
    data = b"<feed>" + pi + b"<item><t>real</t></item></feed>"
    items = drain(
        make_tokenizer(
            data, buffer_size=16, chunk_size=chunk_size,
            sections=Sections(max_size=1024),
        )
    )
    assert titles(items) == ["real"]


def test_section_limit_at_eof_warns(caplog):
    data = b"<feed><item><t>real</t></item><!-- sin cerrar"
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(make_tokenizer(data))
    assert titles(items) == ["real"]
    assert "ended inside" in caplog.text


def test_sections_rejects_invalid_policy():
    with pytest.raises(ValueError, match="on_limit"):
        Sections(on_limit="whatever")


def test_sections_rejects_invalid_size():
    with pytest.raises(ValueError):
        Sections(max_size=0)
    with pytest.raises(TypeError):
        Sections(max_size=True)


# --- An item is empty by what it means, not by how it is spelled --- #

def test_item_holding_only_a_comment_is_not_a_record():
    data = (
        b"<feed><item> <!-- nothing --> </item>"
        b"<item><t>real</t></item></feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["real"]


# --- Inside a DOCTYPE, markup is markup too --- #

def test_doctype_internal_comment_holding_the_terminator():
    data = (
        b"<!DOCTYPE feed [\n  <!-- ]> <item><t>ghost</t></item> -->\n]>"
        b"<feed><item><t>real</t></item></feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["real"]


def test_doctype_internal_instruction_holding_the_terminator():
    data = (
        b'<!DOCTYPE feed [<?pi ]> <item><t>ghost</t></item> ?>]>'
        b"<feed><item><t>real</t></item></feed>"
    )
    assert titles(drain(make_tokenizer(data))) == ["real"]


@pytest.mark.parametrize("chunk_size", [1, 7, 64, 4096])
def test_doctype_state_survives_refills(chunk_size):
    # Depth, quotes and inner markup have to outlive both the refills
    # and the buffer compaction that comes with them.
    data = (
        b'<!DOCTYPE feed [<!ENTITY a "]>"> <!-- ]> --> <!ENTITY b "[">'
        b" <?pi ]> ?> ]>"
        b"<feed><item><t>real</t></item></feed>"
    )
    items = drain(make_tokenizer(data, buffer_size=8, chunk_size=chunk_size))
    assert titles(items) == ["real"]


# --- Crossing max_size must not lose what was already read --- #

@pytest.mark.parametrize("buffer_size", [64, 512, 4096, 131072])
@pytest.mark.parametrize("policy", ["text", "markup"])
def test_section_crossing_the_limit_keeps_later_items(buffer_size, policy):
    # The section is valid and closes well past the limit: whichever
    # policy is in force, the item after it is still delivered and the
    # answer cannot depend on the buffer.
    pi = b"<?target " + b"x" * 3000 + b" ?>"
    data = b"<feed>" + pi + b"<item><t>real</t></item></feed>"
    items = drain(
        make_tokenizer(
            data, buffer_size=buffer_size, chunk_size=64,
            sections=Sections(max_size=1024, on_limit=policy),
        )
    )
    assert titles(items) == ["real"]


def test_markup_policy_never_yields_records_from_a_section():
    ghost = b"<?target " + b"y" * 3000 + b" <item><t>inside</t></item> ?>"
    data = b"<feed>" + ghost + b"<item><t>real</t></item></feed>"
    for buffer_size in (64, 4096, 131072):
        items = drain(
            make_tokenizer(
                data, buffer_size=buffer_size, chunk_size=64,
                sections=Sections(max_size=1024, on_limit="markup"),
            )
        )
        assert titles(items) == ["real"], f"buffer_size={buffer_size}"


# --- Growth stays linear when the reads are small --- #

def test_huge_tag_is_linear_with_small_chunks():
    data = (
        b'<feed><item note="' + b"x" * (8 << 20)
        + b'"><t>real</t></item></feed>'
    )
    start = time.perf_counter()
    items = drain(make_tokenizer(data, buffer_size=65536, chunk_size=4096))
    elapsed = time.perf_counter() - start
    assert titles(items) == ["real"]
    assert elapsed < 2.0, f"{elapsed:.2f}s: still quadratic?"


def test_huge_doctype_is_linear_with_small_chunks():
    data = (
        b'<!DOCTYPE feed [<!ENTITY x "' + b"y" * (4 << 20) + b'">]>'
        b"<feed><item><t>real</t></item></feed>"
    )
    start = time.perf_counter()
    items = drain(make_tokenizer(data, buffer_size=65536, chunk_size=4096))
    elapsed = time.perf_counter() - start
    assert titles(items) == ["real"]
    assert elapsed < 2.0, f"{elapsed:.2f}s: still quadratic?"


# --- A malformed delimiter is reported, never swallowed --- #

@pytest.mark.parametrize(
    "data",
    [
        b'<feed><item note=noquotes><t>x</t></item></feed>',
        b'<feed><item a="1" a="2"><t>x</t></item></feed>',
        b"<feed><item garbage><t>x</t></item></feed>",
        b"<feed><item><t>x</t></item garbage></feed>",
    ],
)
def test_malformed_separator_tag_is_reported(data, caplog):
    # The content is intact, so the item is still delivered; what the
    # feed got wrong is said out loud instead of passing unnoticed.
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(make_tokenizer(data))
    assert titles(items) == ["x"]
    assert "separator tag" in caplog.text


def test_well_formed_separator_tags_stay_quiet(caplog):
    data = (
        b'<feed><item id="7" kind=\'entry\' data-x="a>b" /><item id="8">'
        b"<t>x</t></item></feed>"
    )
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(make_tokenizer(data))
    assert titles(items) == ["x"]
    assert caplog.text == ""


@pytest.mark.parametrize(
    "data",
    [
        b'<feed><item a="x"b="y"><t>x</t></item></feed>',     # no space
        b'<feed><item foo bar="x"><t>x</t></item></feed>',    # bare word
        b'<feed><item 1bad="x"><t>x</t></item></feed>',       # not a name
        b'<feed><item a="x<y"><t>x</t></item></feed>',        # "<" in value
        b'<feed><item a="x" a="y"><t>x</t></item></feed>',    # repeated
        b"<feed><item a=bare><t>x</t></item></feed>",         # unquoted
        b"<feed><item><t>x</t></item\tgarbage></feed>",       # tabbed junk
        b"<feed><item / ><item><t>x</t></item></feed>",       # "/ >" is not "/>"
    ],
)
def test_delimiter_validation_covers_xml_syntax(data, caplog):
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(make_tokenizer(data))
    assert titles(items) == ["x"]
    assert "separator tag" in caplog.text


@pytest.mark.parametrize(
    "data",
    [
        b'<feed><item a="x" b="y"><t>x</t></item></feed>',
        b"<feed><item a='x'\tb='y'><t>x</t></item></feed>",
        b'<feed><item a="x>y" b=\'c"d\'><t>x</t></item></feed>',
        b'<feed><item xml:lang="es" data-id="7"><t>x</t></item></feed>',
        b'<feed><item a="1"/><item><t>x</t></item></feed>',
        b"<feed><item\n  a='1'\n><t>x</t></item></feed>",
        b"<feed><item><t>x</t></item ></feed>",
    ],
)
def test_valid_delimiters_stay_quiet(data, caplog):
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(make_tokenizer(data))
    assert titles(items) == ["x"]
    assert caplog.text == ""


def test_tokenizer_rejects_a_non_numeric_deadline():
    def gen():
        yield b""

    with pytest.raises(TypeError):
        Tokenizer(
            feed_generator=gen(), separator_tag="item", buffer_size=64,
            deadline="tomorrow",
        )


@pytest.mark.parametrize(
    "data",
    [
        b'<feed><item !bad="x"><t>x</t></item></feed>',
        b'<feed><item a&b="x"><t>x</t></item></feed>',
        b'<feed><item a\x01b="x"><t>x</t></item></feed>',
        b'<feed><item .lead="x"><t>x</t></item></feed>',
        b"<feed><item><t>x</t></item\x0bgarbage></feed>",
        b"<feed><item><t>x</t></item\x0cgarbage></feed>",
        b'<feed><item a="x&y"><t>x</t></item></feed>',
        b'<feed><item a="&#;"><t>x</t></item></feed>',
        b'<feed><item a="&#xZZ;"><t>x</t></item></feed>',
        b'<feed><item a="&bad name;"><t>x</t></item></feed>',
        b'<feed><item a="&unclosed"><t>x</t></item></feed>',
    ],
)
def test_delimiter_names_and_values_follow_xml(data, caplog):
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(make_tokenizer(data))
    assert titles(items) == ["x"]
    assert "separator tag" in caplog.text


@pytest.mark.parametrize(
    "data",
    [
        b'<feed><item a="x&amp;y"><t>x</t></item></feed>',
        b'<feed><item a="&#65;&#x41;"><t>x</t></item></feed>',
        b'<feed><item _a.b-c="x" ns:d="y"><t>x</t></item></feed>',
        "<feed><item añejo=\"x\"><t>x</t></item></feed>".encode(),
        b'<feed><item a="&quot;quoted&quot;"><t>x</t></item></feed>',
    ],
)
def test_valid_names_and_entity_references_stay_quiet(data, caplog):
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(make_tokenizer(data))
    assert titles(items) == ["x"]
    assert caplog.text == ""


@pytest.mark.parametrize(
    "value",
    [
        b"&#0;",          # not a Char
        b"&#xD800;",      # surrogate
        b"&#xFFFE;",      # permanently unassigned
        b"&#x110000;",    # past the last code point
        b"\x01",          # literal control
        b"\xff\xfe",      # not utf-8 at all
    ],
)
def test_attribute_values_must_hold_xml_chars(value, caplog):
    data = b'<feed><item a="' + value + b'"><t>x</t></item></feed>'
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(make_tokenizer(data))
    assert titles(items) == ["x"]
    assert "separator tag" in caplog.text


@pytest.mark.parametrize(
    "value",
    [
        "&#\u00b2;".encode(),        # superscript two: no value
        "&#\u0663\u0662;".encode(),  # arabic-indic 32: not XML digits
        b"&#" + b"1" * 5000 + b";",   # longer than int() converts
        b"&#x" + b"1" * 5000 + b";",
    ],
)
def test_numeric_references_are_ascii_digits_in_range(value, caplog):
    # A reference XML cannot spell is malformed markup: never an
    # exception out of the scan, and never a silent pass either.
    data = b'<feed><item a="' + value + b'"><t>x</t></item></feed>'
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(make_tokenizer(data))
    assert titles(items) == ["x"]
    assert "separator tag" in caplog.text


def test_long_numeric_references_still_read_their_value():
    # Leading zeros are legal XML: giving up past the last code point
    # must not give up on a reference that never passes it.
    assert attvalue_is_well_formed(b"&#" + b"0" * 5000 + b"65;")
    assert attvalue_is_well_formed(b"&#x" + b"0" * 5000 + b"41;")


@pytest.mark.parametrize(
    "value",
    [b"&#65;", b"&#x41;", b"&#x10FFFF;", b"tab\there", "acento".encode()],
)
def test_valid_chars_and_references_stay_quiet(value, caplog):
    data = b'<feed><item a="' + value + b'"><t>x</t></item></feed>'
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = drain(make_tokenizer(data))
    assert titles(items) == ["x"]
    assert caplog.text == ""
