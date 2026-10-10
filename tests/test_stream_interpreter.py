import base64
import gzip
import logging
import time
from datetime import datetime, timedelta

import pytest

from feedlib import build_feed

import xmlstreamer
from xmlstreamer import (
    ApiKeyAuth,
    BasicAuth,
    BearerAuth,
    DigestAuth,
    FeedInterruptedError,
    Nested,
    StreamInterpreter,
    Transport,
    UnsupportedSchemeError,
    XMLStreamerError,
)

BOTH_MODES = pytest.mark.parametrize("mode", ["temp", "stream"])


def make_interpreter(url: str, **kwargs) -> StreamInterpreter:
    return StreamInterpreter(url=url, separator_tag="item", **kwargs)


@BOTH_MODES
def test_end_to_end_plain_http(http_server, mode):
    url = http_server.serve(
        "/feed.xml",
        build_feed([{"title": "one"}, {"title": "two"}, {"title": "three"}]),
    )
    interpreter = make_interpreter(
        url, transport=Transport(download_mode=mode)
    )
    items = list(interpreter)
    assert [item["title"] for item in items] == ["one", "two", "three"]
    assert interpreter.stats_total_items == 3
    assert interpreter.stats_parsed_items == 3
    assert interpreter.stats_delivered_items == 3


@BOTH_MODES
def test_end_to_end_gzip(http_server, mode):
    body = gzip.compress(build_feed([{"title": "one"}, {"title": "two"}]))
    url = http_server.serve(
        "/feed.xml.gz", body, content_type="application/octet-stream"
    )
    items = list(
        make_interpreter(url, transport=Transport(download_mode=mode))
    )
    assert [item["title"] for item in items] == ["one", "two"]


@BOTH_MODES
def test_end_to_end_multi_member_gzip(http_server, mode):
    part1 = build_feed([{"title": "one"}])
    part2 = build_feed([{"title": "two"}])
    body = gzip.compress(part1) + gzip.compress(part2)
    url = http_server.serve(
        "/multi.xml.gz", body, content_type="application/octet-stream"
    )
    items = list(
        make_interpreter(url, transport=Transport(download_mode=mode))
    )
    assert [item["title"] for item in items] == ["one", "two"]


@BOTH_MODES
def test_end_to_end_gzip_utf16(http_server, mode):
    feed = build_feed(
        [{"title": "programación"}, {"title": "ñandú"}],
        encoding="utf-16",
    )
    url = http_server.serve(
        "/feed16.xml.gz", gzip.compress(feed), content_type="application/octet-stream"
    )
    items = list(
        make_interpreter(url, transport=Transport(download_mode=mode))
    )
    assert [item["title"] for item in items] == ["programación", "ñandú"]


def test_malformed_item_excluded_and_counted(http_server):
    body = (
        b"<feed>"
        b"<item><t>a</t></item>"
        b"<item><t>b</item>"  # malformed: counted but not emitted
        b"<item><t>c</t></item>"
        b"</feed>"
    )
    url = http_server.serve("/feed.xml", body)
    interpreter = make_interpreter(url)
    items = list(interpreter)
    assert [item["t"] for item in items] == ["a", "c"]
    assert interpreter.stats_total_items == 3
    assert interpreter.stats_parsed_items == 2
    assert interpreter.stats_delivered_items == 2


def test_bearer_auth_header_sent(http_server):
    url = http_server.serve("/feed.xml", build_feed([{"t": "x"}]))
    list(make_interpreter(url, transport=Transport(auth=BearerAuth("tok123"))))
    assert http_server.last_headers()["authorization"] == "Bearer tok123"


def test_api_key_header_sent(http_server):
    url = http_server.serve("/feed.xml", build_feed([{"t": "x"}]))
    list(make_interpreter(
        url,
        transport=Transport(auth=ApiKeyAuth(header="X-Api-Key", value="s3cr3t")),
    ))
    assert http_server.last_headers()["x-api-key"] == "s3cr3t"


def test_basic_auth_header_sent(http_server):
    url = http_server.serve("/feed.xml", build_feed([{"t": "x"}]))
    list(make_interpreter(url, transport=Transport(auth=BasicAuth("user", "pass"))))
    expected = "Basic " + base64.b64encode(b"user:pass").decode()
    assert http_server.last_headers()["authorization"] == expected


def test_output_nested_reshapes_items(http_server):
    body = (
        b"<feed><item>"
        b"<title>Atlas</title>"
        b"<location><city>Alba</city><country>AA</country></location>"
        b"</item></feed>"
    )
    url = http_server.serve("/nested.xml", body)
    items = list(make_interpreter(url, output=Nested()))
    assert items == [
        {
            "title": "Atlas",
            "location": {"city": "Alba", "country": "AA"},
        }
    ]


def test_output_nested_with_force_list(http_server):
    body = (
        b"<feed><item>"
        b"<title>Atlas</title>"
        b"<editions>"
        b"<edition><code>AA</code></edition>"
        b"<edition><code>BB</code></edition>"
        b"</editions>"
        b"</item></feed>"
    )
    url = http_server.serve("/forcelist.xml", body)
    items = list(
        make_interpreter(
            url, output=Nested(force_list={"editions/edition"})
        )
    )
    assert items == [
        {
            "title": "Atlas",
            "editions": {
                "edition": [{"code": "AA"}, {"code": "BB"}]
            },
        }
    ]


def test_output_nested_with_force_list_true(http_server):
    body = (
        b"<feed><item>"
        b"<title>Atlas</title>"
        b"<location><city>Alba</city></location>"
        b"</item></feed>"
    )
    url = http_server.serve("/forcelisttrue.xml", body)
    items = list(make_interpreter(url, output=Nested(force_list=True)))
    assert items == [
        {
            "title": ["Atlas"],
            "location": [{"city": ["Alba"]}],
        }
    ]


def test_output_rejects_non_nested_values():
    # force_list only exists inside Nested(); output just validates type.
    with pytest.raises(TypeError, match="Nested"):
        make_interpreter("http://unused.invalid/feed", output=True)


def test_output_nested_filters_still_see_flat_keys(http_server):
    # Conversion happens AFTER the filter: filters see the flat form.
    body = (
        b"<feed>"
        b"<item><t>a</t><meta><keep>yes</keep></meta></item>"
        b"<item><t>b</t><meta><keep>no</keep></meta></item>"
        b"</feed>"
    )
    url = http_server.serve("/nestedfilter.xml", body)

    def keep_flagged(item):
        return item.get("meta/keep") != "no"

    items = list(
        make_interpreter(url, item_filter=keep_flagged, output=Nested())
    )
    assert items == [{"t": "a", "meta": {"keep": "yes"}}]


def test_filter_func_drops_items_and_updates_stats(http_server):
    url = http_server.serve(
        "/feed.xml",
        build_feed([
            {"t": "a", "status": "new"},
            {"t": "b", "status": "old"},
            {"t": "c", "status": "new"},
        ]),
    )

    def keep_new_items(item):
        return item["status"] != "old"

    interpreter = make_interpreter(url, item_filter=keep_new_items)
    items = list(interpreter)
    assert [item["t"] for item in items] == ["a", "c"]
    assert interpreter.stats_total_items == 3
    assert interpreter.stats_parsed_items == 3
    assert interpreter.stats_delivered_items == 2


def test_closing_log_reports_the_funnel(http_server, caplog):
    # The end-of-run log names every counter truthfully: total,
    # parsed and delivered are three different numbers.
    url = http_server.serve(
        "/funnel.xml",
        build_feed([
            {"t": "a", "status": "new"},
            {"t": "b", "status": "old"},
            {"t": "c", "status": "new"},
        ]),
    )
    with caplog.at_level(logging.INFO, logger="xmlstreamer"):
        items = list(
            make_interpreter(url, item_filter=lambda i: i["status"] != "old")
        )
    assert len(items) == 2
    assert "total=3 parsed=3 delivered=2" in caplog.text


def test_item_filter_rejects_non_callable():
    with pytest.raises(TypeError, match="item_filter"):
        make_interpreter("http://unused.invalid/feed", item_filter=object())


def test_url_rejects_non_string():
    # None is no longer a bad type: it means "use source instead", and
    # with neither given the constructor asks for exactly one.
    with pytest.raises(ValueError, match="url or source"):
        StreamInterpreter(url=None, separator_tag="item")
    with pytest.raises(TypeError, match="url"):
        StreamInterpreter(
            url=b"http://unused.invalid/feed", separator_tag="item"
        )


def test_separator_tag_rejects_non_string_and_empty():
    # An empty or whitespace tag can never match a real XML tag: the
    # run would scan the whole feed and yield zero items in silence.
    with pytest.raises(TypeError, match="separator_tag"):
        StreamInterpreter(url="http://unused.invalid/feed", separator_tag=123)
    with pytest.raises(ValueError, match="separator_tag"):
        StreamInterpreter(url="http://unused.invalid/feed", separator_tag="")
    with pytest.raises(ValueError, match="separator_tag"):
        StreamInterpreter(url="http://unused.invalid/feed", separator_tag="  ")


def test_buffer_size_rejects_non_int_and_nonpositive():
    with pytest.raises(TypeError, match="buffer_size"):
        make_interpreter("http://unused.invalid/feed", buffer_size="64")
    with pytest.raises(ValueError, match="buffer_size"):
        make_interpreter("http://unused.invalid/feed", buffer_size=0)
    with pytest.raises(ValueError, match="buffer_size"):
        make_interpreter("http://unused.invalid/feed", buffer_size=-1)


def test_max_running_time_rejects_non_number_and_nonpositive():
    # A negative or zero budget ends every run at the first check with
    # zero items and no error; a str would raise mid-iteration instead.
    with pytest.raises(TypeError, match="max_running_time"):
        make_interpreter("http://unused.invalid/feed", max_running_time="60")
    with pytest.raises(ValueError, match="max_running_time"):
        make_interpreter("http://unused.invalid/feed", max_running_time=0)
    with pytest.raises(ValueError, match="max_running_time"):
        make_interpreter("http://unused.invalid/feed", max_running_time=-5)
    # Construction does no IO: accepting these is safe to assert.
    make_interpreter("http://unused.invalid/feed", max_running_time=30.5)
    make_interpreter("http://unused.invalid/feed", max_running_time=None)


def test_item_filter_can_mutate_in_place(http_server):
    url = http_server.serve("/mut.xml", build_feed([{"t": "a"}]))

    def stamp(item):
        item["flag"] = "seen"
        return True

    items = list(make_interpreter(url, item_filter=stamp))
    assert items == [{"t": "a", "flag": "seen"}]


def test_filter_exceptions_bubble_up_raw(http_server):
    # A filter failure is a bug in caller code, not damage in the data:
    # it must propagate untouched, never be absorbed like a broken item.
    url = http_server.serve(
        "/bubble.xml",
        build_feed([{"t": "a"}, {"t": "boom"}, {"t": "c"}]),
    )

    def fragile(item):
        if item["t"] == "boom":
            raise KeyError("price")
        return True

    interpreter = make_interpreter(url, item_filter=fragile)
    iterator = iter(interpreter)
    assert next(iterator)["t"] == "a"
    with pytest.raises(KeyError, match="price"):
        next(iterator)
    # The fatal item was consumed and counted as parsed. A caller that
    # catches and keeps iterating resumes at the NEXT item.
    assert next(iterator)["t"] == "c"
    assert interpreter.stats_parsed_items == 3
    assert interpreter.stats_delivered_items == 2


def test_stats_reset_between_iterations(http_server):
    url = http_server.serve("/feed.xml", build_feed([{"t": "a"}, {"t": "b"}]))
    interpreter = make_interpreter(url)
    assert len(list(interpreter)) == 2
    assert len(list(interpreter)) == 2
    assert interpreter.stats_total_items == 2
    assert interpreter.stats_parsed_items == 2
    assert interpreter.stats_delivered_items == 2


def test_running_time_budget_starts_with_the_run(http_server):
    url = http_server.serve("/feed.xml", build_feed([{"t": "a"}]))
    interpreter = make_interpreter(url, max_running_time=3600)
    after_construction = time.monotonic()
    run = iter(interpreter)
    # The clock belongs to the run and starts with it, not with the
    # interpreter that may have been configured much earlier.
    assert run._budget_start >= after_construction
    assert run.check_terminate() is False


def test_budget_ignores_wall_clock_jumps(http_server, monkeypatch):
    # A DST shift or an NTP step moves datetime.now() by hours; the budget
    # is measured with a monotonic clock and must not notice.
    url = http_server.serve("/jump.xml", build_feed([{"t": "a"}]))
    run = iter(make_interpreter(url, max_running_time=60))

    class JumpedClock:
        @staticmethod
        def now():
            return datetime.now() + timedelta(hours=2)

    monkeypatch.setattr(xmlstreamer, "datetime", JumpedClock)
    assert run.check_terminate() is False


def test_timeout_param_reaches_the_download(http_server):
    url = http_server.serve("/slow.xml", b"<feed></feed>", delay=2.0)
    interpreter = make_interpreter(url, transport=Transport(timeout=(5, 0.3)))
    import requests as _requests

    with pytest.raises(_requests.exceptions.Timeout):
        list(interpreter)


def test_check_terminate_within_limit(http_server):
    url = http_server.serve("/ct1.xml", build_feed([{"t": "a"}]))
    run = iter(make_interpreter(url, max_running_time=3600))
    assert run.check_terminate() is False


def test_check_terminate_exceeded(http_server):
    url = http_server.serve("/ct2.xml", build_feed([{"t": "a"}]))
    run = iter(make_interpreter(url, max_running_time=3))
    run._budget_start -= 10
    assert run.check_terminate() is True


def test_check_terminate_without_limit(http_server):
    url = http_server.serve("/ct3.xml", build_feed([{"t": "a"}]))
    run = iter(make_interpreter(url, max_running_time=None))
    run._budget_start -= 5 * 86400
    assert run.check_terminate() is False


def test_check_terminate_past_a_day_boundary(http_server):
    url = http_server.serve("/ct4.xml", build_feed([{"t": "a"}]))
    run = iter(make_interpreter(url, max_running_time=3600))
    run._budget_start -= 86400 + 30
    assert run.check_terminate() is True


def test_buffer_size_reaches_tokenizer(http_server):
    url = http_server.serve("/feed.xml", build_feed([{"t": "x"}]))
    interpreter = make_interpreter(url, buffer_size=999_999)
    iter(interpreter)
    assert iter(interpreter).tokenizer.buffer_size == 999_999


def test_item_bigger_than_default_buffer(http_server):
    big = "X" * (200 * 1024)
    url = http_server.serve("/big.xml", build_feed([{"d": big}, {"d": "small"}]))
    interpreter = make_interpreter(url, buffer_size=1024 * 1024)
    items = list(interpreter)
    assert [item["d"] for item in items] == [big, "small"]


def test_gzip_latin1_feed_keeps_accents(http_server):
    items_src = [{"title": f"programación región café {i}"} for i in range(10)]
    feed = build_feed(items_src, encoding="latin-1")
    url = http_server.serve(
        "/latin1.xml.gz", gzip.compress(feed), content_type="application/octet-stream"
    )
    items = list(make_interpreter(url))
    assert [item["title"] for item in items] == [i["title"] for i in items_src]


def test_transport_rejects_unknown_download_mode():
    with pytest.raises(ValueError, match="download_mode"):
        Transport(download_mode="x")


def test_interpreter_rejects_non_transport_values():
    with pytest.raises(TypeError, match="Transport"):
        make_interpreter("http://unused.invalid/feed", transport="stream")


def test_transport_user_agent_reaches_request(http_server):
    url = http_server.serve("/ua.xml", build_feed([{"t": "x"}]))
    list(make_interpreter(url, transport=Transport(user_agent="XSBot/1.0")))
    assert http_server.last_headers()["user-agent"] == "XSBot/1.0"


def test_stream_mode_mid_feed_cut_raises_feed_interrupted(http_server):
    body = build_feed([{"t": f"item {i}", "pad": "x" * 40} for i in range(2000)])
    url = http_server.serve(
        "/cut.xml", body, truncate_after=len(body) // 2
    )
    interpreter = make_interpreter(
        url, buffer_size=2048, transport=Transport(download_mode="stream")
    )
    delivered = []
    with pytest.raises(FeedInterruptedError) as excinfo:
        for item in interpreter:
            delivered.append(item)
    assert len(delivered) > 0
    assert excinfo.value.items_delivered == len(delivered)
    assert excinfo.value.__cause__ is not None


def test_a_run_keeps_the_mode_it_opened_with(http_server):
    # A run acquired its source in one mode: retuning the transport
    # afterwards must not change how its errors are reported.
    body = build_feed([{"t": f"item {i}", "pad": "x" * 40} for i in range(2000)])
    url = http_server.serve("/cutmode.xml", body, truncate_after=len(body) // 2)
    interpreter = make_interpreter(
        url, buffer_size=2048, transport=Transport(download_mode="stream")
    )
    run = iter(interpreter)
    next(run)
    interpreter.TRANSPORT = Transport(download_mode="temp")
    with pytest.raises(FeedInterruptedError):
        for _ in run:
            pass


def test_stream_mode_connect_phase_errors_stay_raw(http_server):
    # Failures before the first item (connect, sample) keep their raw
    # exception type; FeedInterruptedError only exists mid-iteration.
    import requests as _requests

    url = http_server.serve("/slowstream.xml", b"<feed></feed>", delay=2.0)
    interpreter = make_interpreter(
        url, transport=Transport(download_mode="stream", timeout=(5, 0.3))
    )
    with pytest.raises(_requests.exceptions.Timeout):
        list(interpreter)


def test_stream_mode_http_error_stays_raw(http_server):
    import requests as _requests

    interpreter = make_interpreter(
        http_server.serve("/gone.xml", b"x").replace("/gone.xml", "/missing.xml"),
        transport=Transport(download_mode="stream"),
    )
    with pytest.raises(_requests.exceptions.HTTPError):
        list(interpreter)


def test_utf8_feed_pipeline_never_runs_chardet(http_server, monkeypatch):
    # End to end: a utf-8 feed must not pay the chardet startup cost.
    import chardet as _chardet

    def boom(sample):
        raise AssertionError("chardet.detect ran for a utf-8 feed")

    monkeypatch.setattr(_chardet, "detect", boom)
    url = http_server.serve("/u8.xml", build_feed([{"t": "a"}, {"t": "b"}]))
    items = list(make_interpreter(url))
    assert [item["t"] for item in items] == ["a", "b"]


def test_plain_utf16_feed_parses(http_server):
    # Plain (non-gzip) UTF-16 feeds must be detected and transcoded.
    feed = build_feed(
        [{"title": "programación"}, {"title": "ñandú"}],
        encoding="utf-16",
    )
    url = http_server.serve("/plain16.xml", feed)
    items = list(make_interpreter(url))
    assert [item["title"] for item in items] == ["programación", "ñandú"]


def test_plain_latin1_feed_keeps_accents(http_server):
    items_src = [{"title": f"programación región café {i}"} for i in range(10)]
    url = http_server.serve("/plain1.xml", build_feed(items_src, encoding="latin-1"))
    items = list(make_interpreter(url))
    assert [item["title"] for item in items] == [i["title"] for i in items_src]


def test_plain_utf8_accents_never_transcoded(http_server):
    # Valid utf-8 is never transcoded, whatever chardet claims.
    items_src = [
        {"title": f"programación coração descripción melodía {i}"}
        for i in range(30)
    ]
    url = http_server.serve("/utf8acc.xml", build_feed(items_src))
    items = list(make_interpreter(url))
    assert [item["title"] for item in items] == [i["title"] for i in items_src]


def test_empty_gzip_feed_yields_nothing(http_server):
    # Valid gzip of empty content: chardet returns None, must not crash.
    url = http_server.serve(
        "/empty.gz", gzip.compress(b""), content_type="application/octet-stream"
    )
    assert list(make_interpreter(url)) == []


def _big_feed() -> bytes:
    # Much bigger than buffer_size=2048 so the source outlives early breaks.
    return build_feed([{"t": "x" * 100, "n": str(i)} for i in range(3000)])


def test_close_before_iteration_is_noop():
    interpreter = make_interpreter("http://unused.invalid/feed")
    interpreter.close()
    interpreter.close()
    with make_interpreter("http://unused.invalid/feed") as managed:
        assert isinstance(managed, StreamInterpreter)


@BOTH_MODES
def test_context_manager_closes_source_on_break(http_server, mode):
    url = http_server.serve("/cm.xml", _big_feed())
    with make_interpreter(
        url, buffer_size=2048, transport=Transport(download_mode=mode)
    ) as interpreter:
        for _ in interpreter:
            break
        assert interpreter.last_run.tokenizer.feed_generator.gi_frame\
            is not None
    assert interpreter.last_run.tokenizer.feed_generator.gi_frame is None


def _record_response_closes(monkeypatch) -> list:
    """Patch requests.get so every response records its close() call."""
    import requests as _requests

    closes = []
    real_get = _requests.get

    def spying_get(*args, **kwargs):
        response = real_get(*args, **kwargs)
        real_close = response.close

        def recording_close():
            closes.append(True)
            real_close()

        response.close = recording_close
        return response

    monkeypatch.setattr(_requests, "get", spying_get)
    return closes


def test_close_releases_stream_socket(http_server, monkeypatch):
    url = http_server.serve("/sock.xml", _big_feed())
    closes = _record_response_closes(monkeypatch)
    interpreter = make_interpreter(
        url, buffer_size=2048, transport=Transport(download_mode="stream")
    )
    for _ in interpreter:
        break
    assert closes == []
    interpreter.close()
    assert closes == [True]


def test_runs_hold_their_own_source(http_server, monkeypatch):
    # Runs are independent: opening a second one does not reach into
    # the first, and each releases what it opened.
    url = http_server.serve("/reiter.xml", _big_feed())
    closes = _record_response_closes(monkeypatch)
    interpreter = make_interpreter(
        url, buffer_size=2048, transport=Transport(download_mode="stream")
    )
    first = iter(interpreter)
    next(first)
    second = iter(interpreter)
    next(second)
    assert closes == []
    first.close()
    assert closes == [True]
    assert first.tokenizer.feed_generator.gi_frame is None
    assert second.tokenizer.feed_generator.gi_frame is not None
    second.close()
    assert closes == [True, True]


def test_close_releases_temp_file(http_server, monkeypatch):
    import tempfile as _tempfile

    url = http_server.serve("/tmpf.xml", _big_feed())
    created = []
    real_temporary_file = _tempfile.TemporaryFile

    def spying_temporary_file(*args, **kwargs):
        f = real_temporary_file(*args, **kwargs)
        created.append(f)
        return f

    monkeypatch.setattr(_tempfile, "TemporaryFile", spying_temporary_file)
    interpreter = make_interpreter(url, buffer_size=2048)
    for _ in interpreter:
        break
    assert created and not created[0].closed
    interpreter.close()
    assert all(f.closed for f in created)


def test_close_mid_iteration_ends_cleanly(http_server):
    url = http_server.serve("/drain.xml", _big_feed())
    interpreter = make_interpreter(url, buffer_size=2048)
    run = iter(interpreter)
    next(run)
    interpreter.close()
    # A closed run is over: nothing left in its buffer comes out, so the
    # count never depends on where the reads happened to fall. list() is
    # safe on the run itself (iter(run) is run; it never re-downloads).
    assert list(run) == []


@BOTH_MODES
def test_closing_a_run_ends_it(http_server, mode):
    url = http_server.serve("/closerun.xml", _big_feed())
    interpreter = make_interpreter(
        url, buffer_size=2048, transport=Transport(download_mode=mode)
    )
    run = iter(interpreter)
    next(run)
    run.close()
    for _ in range(3):
        with pytest.raises(StopIteration):
            next(run)


def test_closing_a_run_reports_its_funnel_once(http_server, caplog):
    # However a run ends, it ends once: source released, funnel logged,
    # hook called - and closing again is a no-op.
    seen = []

    class Alerting(StreamInterpreter):
        def run_finished(self, run):
            seen.append(run.stats_delivered_items)

    url = http_server.serve("/closehook.xml", _big_feed())
    interpreter = Alerting(url=url, separator_tag="item", buffer_size=2048)
    run = iter(interpreter)
    next(run)
    with caplog.at_level(logging.INFO, logger="xmlstreamer"):
        run.close()
        run.close()
        interpreter.close()
    assert seen == [1]
    assert caplog.text.count("XMLStreamer stats:") == 1


def test_runtime_exceeded_closes_source(http_server):
    url = http_server.serve("/rt.xml", _big_feed())
    interpreter = make_interpreter(
        url, max_running_time=3600, buffer_size=2048
    )
    iterator = iter(interpreter)
    next(iterator)
    iterator._budget_start -= 2 * 3600
    with pytest.raises(StopIteration):
        next(iterator)
    assert interpreter.last_run.tokenizer.feed_generator.gi_frame is None


def test_feed_interrupted_error_belongs_to_the_error_family():
    assert issubclass(FeedInterruptedError, XMLStreamerError)


def test_stream_mode_unknown_scheme_raises_at_iter():
    # The stream path has its own raise site; it fires eagerly at iter().
    interpreter = make_interpreter(
        "gopher://example.com/feed",
        transport=Transport(download_mode="stream"),
    )
    with pytest.raises(UnsupportedSchemeError, match="gopher"):
        iter(interpreter)


def test_invalid_utf8_warns_once_with_a_count(http_server, caplog):
    # Encoding detection samples the head of the feed: legacy bytes
    # arriving later are replaced. That must never be silent, and must
    # not shout once per item either.
    late = "café".encode("latin-1")
    body = (
        b"<feed><pad>" + b"x" * 70000 + b"</pad>"
        + b"".join(
            b"<item><t>" + late + b"</t></item>" for _ in range(5)
        )
        + b"</feed>"
    )
    url = http_server.serve("/late.xml", body)
    with caplog.at_level(logging.INFO, logger="xmlstreamer"):
        items = list(make_interpreter(url))
    assert len(items) == 5
    # One warning for the run, and the total in the closing stats line.
    assert caplog.text.lower().count("invalid utf-8") == 1
    assert "replaced=5" in caplog.text


def test_feed_ending_inside_an_item_warns(http_server, caplog):
    body = b"<feed><item><t>one</t></item><item><t>trunc"
    url = http_server.serve("/cut.xml", body)
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        items = list(make_interpreter(url))
    assert [item["t"] for item in items] == ["one"]
    assert "ended inside an item" in caplog.text


def test_complete_feed_does_not_warn_about_truncation(http_server, caplog):
    url = http_server.serve("/ok.xml", build_feed([{"t": "one"}]))
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        list(make_interpreter(url))
    assert "ended inside an item" not in caplog.text


def test_time_budget_is_checked_while_items_are_filtered_out(http_server):
    # A filter that drops everything never reaches __next__: the budget
    # has to be checked inside the item loop too.
    body = (
        b"<feed>"
        + b"".join(b"<item><n>%d</n></item>" % i for i in range(200000))
        + b"</feed>"
    )
    url = http_server.serve("/budget.xml", body)
    interpreter = make_interpreter(
        url, max_running_time=1, item_filter=lambda item: False
    )
    start = time.monotonic()
    assert list(interpreter) == []
    assert time.monotonic() - start < 1.5


def test_nested_force_list_is_frozen_at_construction(http_server):
    # A generator would be consumed by the first item and change the
    # shape of the second: the config is frozen when Nested is built.
    body = (
        b"<feed>"
        b"<item><tags><tag>a</tag></tags></item>"
        b"<item><tags><tag>b</tag></tags></item>"
        b"</feed>"
    )
    url = http_server.serve("/frozen.xml", body)
    paths = (p for p in ["tags/tag"])
    items = list(make_interpreter(url, output=Nested(force_list=paths)))
    assert items == [
        {"tags": {"tag": ["a"]}},
        {"tags": {"tag": ["b"]}},
    ]


@pytest.mark.parametrize("bad", [123, 4.5, object()])
def test_nested_rejects_non_path_force_list(bad):
    with pytest.raises(TypeError, match="force_list"):
        Nested(force_list=bad)


def test_nested_rejects_non_string_paths():
    with pytest.raises(TypeError, match="force_list"):
        Nested(force_list=["ok", 7])


@pytest.mark.parametrize("tag", ["a/b", "a b", "a>b", "<a", "1a", "a&b"])
def test_separator_tag_rejects_impossible_xml_names(tag):
    # A name no XML tag can have would scan the whole feed and deliver
    # nothing: say so at the call site instead.
    with pytest.raises(ValueError, match="separator_tag"):
        StreamInterpreter(url="http://unused.invalid/feed", separator_tag=tag)


@pytest.mark.parametrize("tag", ["item", "ns:item", "a-b", "a.b", "_a", "ítem"])
def test_separator_tag_accepts_valid_xml_names(tag):
    StreamInterpreter(url="http://unused.invalid/feed", separator_tag=tag)


def test_max_running_time_rejects_nan():
    # NaN passes every "<= 0" check and then never triggers: the run
    # would silently have no budget at all.
    with pytest.raises(ValueError, match="max_running_time"):
        make_interpreter("http://unused.invalid/feed", max_running_time=float("nan"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": "30"},
        {"timeout": (30,)},
        {"timeout": -1},
        {"proxy": "http://proxy:8080"},
        {"user_agent": 123},
        {"auth": "a-token"},
    ],
)
def test_transport_rejects_invalid_settings(kwargs):
    with pytest.raises((TypeError, ValueError)):
        Transport(**kwargs)


def test_transport_accepts_valid_settings():
    Transport(timeout=30)
    Transport(timeout=(5, 60))
    Transport(timeout=None)
    Transport(proxy={"http": "http://proxy:8080"})
    Transport(auth=BearerAuth("tok"))


def test_time_budget_bounds_a_slow_source(http_server, caplog):
    # The budget is checked per chunk, not per refill: a source that
    # trickles must not outlive it inside a single read.
    body = b"<feed>" + b"<other>x</other>" * 20000 + b"</feed>"
    url = http_server.serve("/slow.xml", body, delay=0.2)
    interpreter = make_interpreter(
        url,
        max_running_time=0.01,
        transport=Transport(download_mode="stream"),
    )
    start = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        assert list(interpreter) == []
    assert time.monotonic() - start < 2.0
    assert "exceeded" in caplog.text.lower()


@pytest.mark.parametrize(
    "build",
    [
        lambda: BasicAuth(None, "p"),
        lambda: BasicAuth("u", 7),
        lambda: BearerAuth(7),
        lambda: ApiKeyAuth(header="X", value=object()),
    ],
)
def test_credentials_reject_non_string_fields(build):
    # Checked where they are written, not when a request is built.
    with pytest.raises(TypeError):
        build()


@pytest.mark.parametrize("proxy", [{"https": 7}, {7: "http://p"}])
def test_transport_rejects_malformed_routes(proxy):
    with pytest.raises(TypeError):
        Transport(proxy=proxy)


def test_malformed_delimiters_reach_the_closing_stats(http_server, caplog):
    body = (
        b"<feed>"
        b'<item a=bare><t>one</t></item>'
        b'<item b=bare><t>two</t></item>'
        b"</feed>"
    )
    url = http_server.serve("/delims.xml", body)
    with caplog.at_level(logging.INFO, logger="xmlstreamer"):
        items = list(make_interpreter(url))
    assert len(items) == 2
    assert "malformed=2" in caplog.text


def test_time_budget_covers_the_download(http_server, caplog):
    # The budget is the run's, not the parser's: a slow acquisition
    # cannot spend it before the first item is even sought.
    url = http_server.serve(
        "/slowdl.xml", build_feed([{"t": "a"}] * 50), delay=0.4
    )
    interpreter = make_interpreter(url, max_running_time=0.01)
    start = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        assert list(interpreter) == []
    assert time.monotonic() - start < 2.0
    assert "exceeded" in caplog.text.lower()


@pytest.mark.parametrize(
    "build",
    [
        lambda: ApiKeyAuth("", "v"),
        lambda: ApiKeyAuth("X\nInjected: 1", "v"),
        lambda: ApiKeyAuth("X Key", "v"),
        lambda: ApiKeyAuth("X-Key", "v\nInjected: 1"),
        lambda: BearerAuth("tok\r\nInjected: 1"),
        lambda: BasicAuth("user\n", "p"),
    ],
)
def test_credentials_reject_header_injection(build):
    # Every credential ends up in a header: a newline in one would
    # forge a second header.
    with pytest.raises(ValueError):
        build()


def test_time_budget_covers_the_stream_head(caplog):
    # Stream mode peeks the head and samples the encoding before
    # yielding anything. The budget is checked between reads, so a slow
    # source is abandoned after one of them instead of feeding the
    # whole sample. What it cannot do is interrupt a read in flight.
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    piece = b"<other>x</other>" * 5000  # bigger than one read
    rounds = 8

    class Trickle(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(piece) * rounds + 13))
            self.end_headers()
            self.wfile.write(b"<feed>")
            for _ in range(rounds):
                self.wfile.write(piece)
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"</feed>")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        interpreter = make_interpreter(
            f"http://{host}:{port}/head.xml",
            max_running_time=0.01,
            transport=Transport(download_mode="stream"),
        )
        start = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
            assert list(interpreter) == []
        elapsed = time.monotonic() - start
    finally:
        server.shutdown()
    # The whole trickle is 8 x 50ms: stopping early is the point.
    assert elapsed < 0.25, f"{elapsed:.2f}s: the sample loop ignored it"


# --- The interpreter is configuration; iterating it opens a run --- #

def test_iter_returns_an_independent_run(http_server):
    url = http_server.serve("/runs.xml", build_feed([{"t": "a"}, {"t": "b"}]))
    interpreter = make_interpreter(url)
    run = iter(interpreter)
    assert run is not interpreter
    assert [item["t"] for item in run] == ["a", "b"]
    assert len(http_server.requests) == 1


def test_asking_for_an_iterator_twice_downloads_once(http_server):
    # iter() opens a run over one acquisition: list(iter(x))
    # fetches the feed once.
    url = http_server.serve("/once.xml", build_feed([{"t": "a"}, {"t": "b"}]))
    interpreter = make_interpreter(url)
    assert [item["t"] for item in list(iter(interpreter))] == ["a", "b"]
    assert len(http_server.requests) == 1


def test_two_runs_of_the_same_interpreter_are_independent(http_server):
    url = http_server.serve("/two.xml", build_feed([{"t": "a"}, {"t": "b"}]))
    interpreter = make_interpreter(url)
    outer = iter(interpreter)
    assert next(outer)["t"] == "a"
    # A second run reads the feed from the top without disturbing the
    # first: nested loops are ordinary code.
    assert [item["t"] for item in interpreter] == ["a", "b"]
    assert next(outer)["t"] == "b"
    assert len(http_server.requests) == 2


def test_run_carries_its_own_stats(http_server):
    url = http_server.serve("/stats.xml", build_feed([{"t": "a"}, {"t": "b"}]))
    interpreter = make_interpreter(url)
    run = iter(interpreter)
    list(run)
    assert run.stats_total_items == 2
    assert run.stats_delivered_items == 2
    # The interpreter still answers for the last run, so code that
    # reads stats after the loop keeps working.
    assert interpreter.last_run is run
    assert interpreter.stats_total_items == 2


def test_stats_before_any_run_are_zero(http_server):
    interpreter = make_interpreter("http://unused.invalid/feed")
    assert interpreter.last_run is None
    assert interpreter.stats_total_items == 0


def test_end_of_run_hook_fires_once_with_its_own_run(http_server):
    # The hook is told WHICH run ended, and only once.
    seen = []

    class Alerting(StreamInterpreter):
        def run_finished(self, run):
            seen.append(run.stats_delivered_items)

    url = http_server.serve("/hook.xml", build_feed([{"t": "a"}]))
    interpreter = Alerting(url=url, separator_tag="item")
    run = iter(interpreter)
    assert [item["t"] for item in run] == ["a"]
    assert seen == [1]
    # Asking an exhausted run for more is a plain StopIteration.
    for _ in range(3):
        with pytest.raises(StopIteration):
            next(run)
    assert seen == [1]


def test_hook_reports_the_run_that_ended_not_the_newest(http_server):
    seen = []

    class Alerting(StreamInterpreter):
        def run_finished(self, run):
            seen.append(run.stats_delivered_items)

    url = http_server.serve(
        "/hooks.xml", build_feed([{"t": "a"}, {"t": "b"}])
    )
    interpreter = Alerting(url=url, separator_tag="item")
    outer = iter(interpreter)
    assert [next(outer)["t"], next(outer)["t"]] == ["a", "b"]
    iter(interpreter)  # a newer run exists, still untouched
    with pytest.raises(StopIteration):
        next(outer)
    assert seen == [2]


def test_each_run_owns_its_time_budget(http_server):
    # Opening a second run must not rejuvenate the first one's clock.
    url = http_server.serve(
        "/budget.xml", build_feed([{"t": str(i)} for i in range(50)])
    )
    interpreter = make_interpreter(url, max_running_time=0.02)
    outer = iter(interpreter)
    assert next(outer)["t"] == "0"
    time.sleep(0.05)
    iter(interpreter)  # fresh run, fresh clock: only for itself
    with pytest.raises(StopIteration):
        next(outer)


@pytest.mark.parametrize("verdict", [True, False])
def test_a_filter_that_closes_the_run_ends_it(http_server, verdict):
    # The filter is caller code running inside the run: closing it from
    # there ends it like any other close, and the funnel it reported
    # stays true (the item under the filter is not delivered).
    reported = []

    class Alerting(StreamInterpreter):
        def run_finished(self, run):
            reported.append((
                run.stats_total_items,
                run.stats_parsed_items,
                run.stats_delivered_items,
            ))

    url = http_server.serve(f"/filterclose{verdict}.xml", _big_feed())
    holder = {}

    def closing_filter(item):
        holder["run"].close()
        return verdict

    interpreter = Alerting(
        url=url, separator_tag="item", buffer_size=2048,
        item_filter=closing_filter,
    )
    run = iter(interpreter)
    holder["run"] = run
    assert list(run) == []
    # It stops scanning too: the item under the filter is the last one
    # the run ever counted, whatever the filter answered.
    assert run.stats_total_items == 1
    assert run.stats_delivered_items == 0
    assert reported == [(1, 1, 0)]


def _feed_with_a_malformed_item() -> bytes:
    # The scan warns about this one while cutting it out.
    return b"<feed><item><t>a</item><item><t>b</t></item></feed>"


def _feed_with_late_invalid_utf8() -> bytes:
    # Detection samples the head: the legacy bytes arrive after it, so
    # the run warns about the replacement between two counters.
    late = "café".encode("latin-1")
    return (
        b"<feed><pad>" + b"x" * 70000 + b"</pad>"
        + b"".join(b"<item><t>" + late + b"</t></item>" for _ in range(5))
        + b"</feed>"
    )


@pytest.mark.parametrize(
    "build_feed_bytes",
    [_feed_with_a_malformed_item, _feed_with_late_invalid_utf8],
)
def test_a_log_handler_that_closes_the_run_keeps_the_funnel_true(
    http_server, build_feed_bytes,
):
    # A log handler is caller code too, and the library logs while it
    # scans. Whatever it closes, the funnel the run reported is the one
    # it ends with: after the end, no counter moves.
    reported = []

    class Alerting(StreamInterpreter):
        def run_finished(self, run):
            reported.append((
                run.stats_total_items,
                run.stats_parsed_items,
                run.stats_delivered_items,
            ))

    holder = {}

    class ClosingHandler(logging.Handler):
        def emit(self, record):
            run = holder.get("run")
            if run is not None and not run._finished:
                run.close()

    url = http_server.serve(
        f"/handlerclose{build_feed_bytes.__name__}.xml", build_feed_bytes()
    )
    handler = ClosingHandler()
    logger = logging.getLogger("xmlstreamer")
    logger.addHandler(handler)
    try:
        interpreter = Alerting(
            url=url, separator_tag="item", buffer_size=512
        )
        run = iter(interpreter)
        holder["run"] = run
        assert list(run) == []
    finally:
        logger.removeHandler(handler)
    final = (
        run.stats_total_items,
        run.stats_parsed_items,
        run.stats_delivered_items,
    )
    assert reported == [final]


def test_a_hook_cannot_open_a_run_while_the_interpreter_closes(
    http_server, caplog,
):
    # Ending a run calls the hook, and close() takes one snapshot: a
    # retry opened from there would survive the teardown that asked for
    # everything to be released.
    class Retrying(StreamInterpreter):
        def run_finished(self, run):
            self.retry = iter(self)

    url = http_server.serve("/retryonclose.xml", _big_feed())
    interpreter = Retrying(
        url=url, separator_tag="item", buffer_size=2048
    )
    runs = [iter(interpreter), iter(interpreter)]
    for run in runs:
        next(run)
    with caplog.at_level(logging.ERROR, logger="xmlstreamer"):
        interpreter.close()
    assert len(interpreter._open_runs) == 0
    assert all(
        run.tokenizer.feed_generator.gi_frame is None for run in runs
    )
    assert "closing" in caplog.text


class _ClosingHook(StreamInterpreter):
    """A hook that redundantly closes the interpreter it belongs to."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.depth = 0
        self.max_depth = 0

    def run_finished(self, run):
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        try:
            self.close()
        finally:
            self.depth -= 1


def test_a_hook_that_closes_the_interpreter_does_not_recurse(http_server):
    # A close() from inside a teardown is redundant by construction:
    # the sweep already running owns it. Otherwise every run's hook
    # opened another sweep, one level deeper than the last.
    url = http_server.serve("/reentrantclose.xml", _big_feed())
    interpreter = _ClosingHook(
        url=url, separator_tag="item", buffer_size=2048
    )
    runs = [iter(interpreter) for _ in range(6)]
    for run in runs:
        next(run)
    interpreter.close()
    assert interpreter.max_depth == 1
    assert all(
        run.tokenizer.feed_generator.gi_frame is None for run in runs
    )


def test_a_reentrant_close_cannot_disguise_a_release_failure(
    http_server, caplog,
):
    # A release that failed reaches the caller as such, never as a
    # broken hook. Every source fails here, so the WeakSet's order
    # cannot make it pass.
    attempts = []

    class FailingSource:
        def __init__(self, name):
            self.name = name

        def close(self):
            attempts.append(self.name)
            raise OSError("cannot release")

    url = http_server.serve("/reentrantfail.xml", _big_feed())
    interpreter = _ClosingHook(
        url=url, separator_tag="item", buffer_size=2048
    )
    runs = [iter(interpreter) for _ in range(3)]
    for number, run in enumerate(runs):
        next(run)
        run.tokenizer.feed_generator = FailingSource(str(number))
    with caplog.at_level(logging.ERROR, logger="xmlstreamer"):
        with pytest.raises(OSError, match="cannot release"):
            interpreter.close()
    assert sorted(attempts) == ["0", "1", "2"]
    assert "run_finished" not in caplog.text


def test_the_hook_can_open_a_retry_run_at_a_normal_end(http_server):
    # Only a teardown refuses: a run that ended by itself may be
    # replaced from the hook.
    class Retrying(StreamInterpreter):
        retried = False

        def run_finished(self, run):
            if not Retrying.retried:
                Retrying.retried = True
                self.retry = iter(self)

    url = http_server.serve("/retryonend.xml", build_feed([{"t": "a"}]))
    interpreter = Retrying(url=url, separator_tag="item")
    assert [item["t"] for item in interpreter] == ["a"]
    assert [item["t"] for item in interpreter.retry] == ["a"]


def test_get_item_on_a_closed_run_is_stop_iteration(http_server):
    # Terminality is the run's, not just __next__'s.
    url = http_server.serve("/getitemclosed.xml", _big_feed())
    interpreter = make_interpreter(url, buffer_size=2048)
    run = iter(interpreter)
    next(run)
    run.close()
    with pytest.raises(StopIteration):
        run.get_item()


class _BrokenHook(StreamInterpreter):
    def run_finished(self, run):
        raise RuntimeError("the hook is broken")


def test_a_broken_hook_does_not_break_the_run(http_server, caplog):
    # The hook reports: a report that fails is logged with its
    # traceback and goes no further, so work already delivered stands.
    url = http_server.serve("/brokenhook.xml", build_feed([{"t": "a"}]))
    with caplog.at_level(logging.ERROR, logger="xmlstreamer"):
        items = list(_BrokenHook(url=url, separator_tag="item"))
    assert [item["t"] for item in items] == ["a"]
    assert "run_finished" in caplog.text
    assert "the hook is broken" in caplog.text


def test_a_broken_hook_does_not_strand_other_runs(http_server):
    url = http_server.serve("/brokenhooks.xml", _big_feed())
    interpreter = _BrokenHook(
        url=url, separator_tag="item", buffer_size=2048
    )
    runs = [iter(interpreter) for _ in range(3)]
    for run in runs:
        next(run)
    interpreter.close()
    assert all(
        run.tokenizer.feed_generator.gi_frame is None for run in runs
    )


def test_a_broken_hook_does_not_mask_the_caller_error(http_server):
    url = http_server.serve("/brokenhookexc.xml", _big_feed())
    with pytest.raises(ValueError, match="the real error"):
        with _BrokenHook(
            url=url, separator_tag="item", buffer_size=2048
        ) as interpreter:
            for _ in interpreter:
                raise ValueError("the real error")


def test_a_source_that_will_not_release_does_not_strand_the_others(
    http_server,
):
    # close() releases everything it can, then reports the failure.
    # Every source fails here, so the count of attempts tells whether
    # the loop went on - the WeakSet's order cannot make it pass.
    attempts = []

    class FailingSource:
        def __init__(self, name):
            self.name = name

        def close(self):
            attempts.append(self.name)
            raise OSError("cannot release")

    url = http_server.serve("/failrelease.xml", _big_feed())
    interpreter = make_interpreter(url, buffer_size=2048)
    runs = [iter(interpreter) for _ in range(3)]
    for number, run in enumerate(runs):
        next(run)
        run.tokenizer.feed_generator = FailingSource(str(number))
    with pytest.raises(OSError, match="cannot release"):
        interpreter.close()
    assert sorted(attempts) == ["0", "1", "2"]
    # Reported and forgotten anyway: a source that will not release is
    # not a run that stays open.
    assert len(interpreter._open_runs) == 0


class _RaisingSource:
    def __init__(self, name, attempts, error):
        self.name = name
        self.attempts = attempts
        self.error = error

    def close(self):
        self.attempts.append(self.name)
        raise self.error


def _runs_with_sources(http_server, path, errors):
    """Open one run per error, each holding a source that raises it."""
    attempts = []
    url = http_server.serve(path, _big_feed())
    interpreter = make_interpreter(url, buffer_size=2048)
    runs = [iter(interpreter) for _ in errors]
    for number, (run, error) in enumerate(zip(runs, errors)):
        next(run)
        run.tokenizer.feed_generator = _RaisingSource(
            str(number), attempts, error
        )
    return interpreter, runs, attempts


def test_a_cancellation_still_releases_every_run(http_server):
    # Ctrl-C while closing: what is left is bounded work, so the
    # sweep finishes it and re-raises.
    interpreter, runs, attempts = _runs_with_sources(
        http_server, "/cancelclose.xml", [KeyboardInterrupt()] * 3
    )
    with pytest.raises(KeyboardInterrupt):
        interpreter.close()
    assert sorted(attempts) == ["0", "1", "2"]
    assert len(interpreter._open_runs) == 0
    assert interpreter._closing is False


def test_a_cancellation_outranks_a_release_that_failed(http_server):
    # Whichever order the sweep happens to take, the interrupt is what
    # reaches the caller: a release that merely failed must never be
    # the reason a Ctrl-C goes missing.
    errors = [OSError("cannot release")] * 5 + [KeyboardInterrupt()]
    interpreter, runs, attempts = _runs_with_sources(
        http_server, "/cancelrank.xml", errors
    )
    with pytest.raises(KeyboardInterrupt):
        interpreter.close()
    assert sorted(attempts) == ["0", "1", "2", "3", "4", "5"]
    assert len(interpreter._open_runs) == 0


def test_a_cancellation_while_reporting_still_releases_everything(
    http_server,
):
    # Reporting a failed release is a callback too: a log handler is
    # caller code, so nothing is reported until every source is out.
    class InterruptingHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.fired = 0

        def emit(self, record):
            if "Releasing a run failed" in record.getMessage():
                self.fired += 1
                if self.fired == 1:
                    raise KeyboardInterrupt

    interpreter, runs, attempts = _runs_with_sources(
        http_server, "/cancelreport.xml", [OSError("cannot release")] * 3
    )
    handler = InterruptingHandler()
    logger = logging.getLogger("xmlstreamer")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        with pytest.raises(KeyboardInterrupt):
            interpreter.close()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    assert handler.fired == 1
    assert sorted(attempts) == ["0", "1", "2"]
    assert len(interpreter._open_runs) == 0
    assert all(run._finished for run in runs)


def test_no_run_can_be_opened_while_the_teardown_reports(http_server):
    # Reporting is still the teardown: a run opened from a log handler
    # there would outlive the close() that was releasing everything.
    class OpeningHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.errors = []

        def emit(self, record):
            if "Releasing a run failed" in record.getMessage():
                try:
                    iter(interpreter)
                except RuntimeError as exc:
                    self.errors.append(exc)

    interpreter, runs, attempts = _runs_with_sources(
        http_server, "/reportopen.xml", [OSError("cannot release")] * 3
    )
    handler = OpeningHandler()
    logger = logging.getLogger("xmlstreamer")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        with pytest.raises(OSError, match="cannot release"):
            interpreter.close()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    assert len(handler.errors) == 3
    assert sorted(attempts) == ["0", "1", "2"]
    assert len(interpreter._open_runs) == 0


def test_the_first_cancellation_survives_a_reporter_that_cancels_too(
    http_server,
):
    # Reporting a failed release runs caller code, so a second
    # cancellation can arrive there. The one that reaches the caller is
    # still the first: the policy is that a cancellation is never lost,
    # not that the last one wins.
    class CancellingHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.fired = 0

        def emit(self, record):
            if "Releasing a run failed" in record.getMessage():
                self.fired += 1
                raise KeyboardInterrupt("from the handler")

    errors = [OSError("cannot release"), OSError("cannot release either"),
              KeyboardInterrupt("the first one")]
    interpreter, runs, attempts = _runs_with_sources(
        http_server, "/reportcancel.xml", errors
    )
    handler = CancellingHandler()
    logger = logging.getLogger("xmlstreamer")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            interpreter.close()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    assert str(excinfo.value) == "the first one"
    assert handler.fired == 1  # reporting stops: someone wants out
    assert sorted(attempts) == ["0", "1", "2"]
    assert len(interpreter._open_runs) == 0


def test_a_release_failure_is_reported_even_under_a_cancellation(
    http_server, caplog,
):
    # Deliberate: the cancellation is what gets raised, so skipping the
    # report would leave the releases that failed with no trace at all
    # - neither raised nor logged. Losing anything costs a WARNING.
    errors = [OSError("cannot release"), OSError("cannot release either"),
              KeyboardInterrupt("the first one")]
    interpreter, runs, attempts = _runs_with_sources(
        http_server, "/reportunder.xml", errors
    )
    with caplog.at_level(logging.WARNING, logger="xmlstreamer"):
        with pytest.raises(KeyboardInterrupt):
            interpreter.close()
    assert caplog.text.count("Releasing a run failed") == 2


def test_a_run_keeps_the_budget_it_opened_with(http_server):
    # Retuning the interpreter mid-run neither extends nor shortens a
    # budget already granted: the run holds the value, not a reference.
    url = http_server.serve(
        "/budgetsnap.xml", build_feed([{"t": str(i)} for i in range(50)])
    )
    interpreter = make_interpreter(url, max_running_time=0.02)
    run = iter(interpreter)
    assert next(run)["t"] == "0"
    time.sleep(0.05)
    interpreter.MAX_RUNNING_TIME = 600
    with pytest.raises(StopIteration):
        next(run)


def test_closing_the_interpreter_closes_every_open_run(http_server):
    url = http_server.serve("/closeall.xml", build_feed([{"t": "a"}] * 20))
    interpreter = make_interpreter(url)
    first, second = iter(interpreter), iter(interpreter)
    next(first)
    next(second)
    interpreter.close()
    assert first.tokenizer.feed_generator.gi_frame is None
    assert second.tokenizer.feed_generator.gi_frame is None


def test_the_interpreter_is_not_an_iterator(http_server):
    # Deliberate break: it is configuration, so it has no __next__.
    url = http_server.serve("/noiter.xml", build_feed([{"t": "a"}]))
    with pytest.raises(TypeError):
        next(make_interpreter(url))


@pytest.mark.parametrize(
    "build",
    [
        lambda: ApiKeyAuth("X-Key", " leading"),
        lambda: ApiKeyAuth("X-Key", "trailing "),
        lambda: Transport(user_agent="agent\nInjected: 1"),
        # Latin-1 is all a header carries: refused where it is written,
        # not as a UnicodeEncodeError from inside requests.
        lambda: ApiKeyAuth("X-Key", "\u4e2d\u6587"),
        lambda: BearerAuth("\u4e2d\u6587"),
        lambda: Transport(user_agent="agent \u4e2d\u6587"),
    ],
)
def test_header_values_reject_what_requests_would(build):
    with pytest.raises(ValueError):
        build()


def test_credentials_are_kept_as_the_caller_wrote_them():
    # A credential is not a header value: Basic encodes it and FTP
    # sends it verbatim, so padding and non-latin-1 are the caller's
    # to spell. Only line breaks (which forge commands) are refused.
    assert BasicAuth("user", " pass ").password == " pass "
    assert DigestAuth(" user ", "pass\t").username == " user "
    assert BasicAuth("user", "\u4e2d\u6587").password == "\u4e2d\u6587"
    with pytest.raises(ValueError):
        BasicAuth("user", "pass\nX-Injected: 1")


def test_non_latin1_credentials_fail_clearly_over_http(http_server):
    # Requests encodes them as latin-1: say which field and why, before
    # the request, instead of a UnicodeEncodeError from its internals.
    url = http_server.serve("/authl1.xml", build_feed([{"t": "a"}]))
    interpreter = make_interpreter(
        url, transport=Transport(auth=BasicAuth("user", "\u4e2d\u6587"))
    )
    with pytest.raises(ValueError, match="BasicAuth.password must be latin-1"):
        list(interpreter)


def test_only_what_travels_literally_must_be_latin1(http_server):
    # Digest hashes the password, so only its username binds to what a
    # header carries; refusing the password would refuse a credential
    # that works.
    url = http_server.serve("/digest.xml", build_feed([{"t": "a"}]))
    working = make_interpreter(
        url, transport=Transport(auth=DigestAuth("user", "\u4e2d\u6587"))
    )
    assert [item["t"] for item in working] == ["a"]
    refused = make_interpreter(
        url, transport=Transport(auth=DigestAuth("\u4e2d\u6587", "pass"))
    )
    with pytest.raises(ValueError, match="DigestAuth.username must be latin-1"):
        list(refused)


def test_requests_still_hashes_the_digest_password():
    # Why the test above is safe, pinned against requests itself: the
    # password is hashed as utf-8 and never reaches the header.
    import requests.auth

    auth = requests.auth.HTTPDigestAuth("user", "\u4e2d\u6587")
    auth.init_per_thread_state()
    auth._thread_local.chal = {"realm": "r", "nonce": "n", "qop": "auth"}
    header = auth.build_digest_header("GET", "http://example.invalid/f.xml")
    assert "\u4e2d\u6587" not in header
    header.encode("latin-1")
