# xmlstreamer

**xmlstreamer** is a Python library designed for efficient, memory-friendly streaming and parsing of large XML feeds from various sources, including compressed formats. It supports decompression, character encoding detection, tag-based itemization, and optional item filtering, making it ideal for handling real-time XML data feeds, large datasets, and complex structured content.

## Features

- **Streamed Data Parsing**: Parses XML data directly from HTTP, HTTPS, or FTP sources without loading the entire file into memory.
- **GZIP Decompression Support**: Seamlessly handles GZIP-compressed XML feeds for bandwidth efficiency.
- **Encoding Detection**: Automatically detects and decodes character encodings, ensuring compatibility with varied XML data formats.
- **Customizable Item Tokenization**: Uses SAX-based parsing with user-defined tags to parse only relevant items from XML streams, reducing parsing overhead.
- **Configurable Runtime and Buffering**: Includes configurable buffer sizes and runtime limits, allowing you to tailor performance to your application’s needs.
- **Flexible Filtering**: Supports custom item filtering based on tag attributes and content, ideal for targeted data extraction.

## Example Use Cases

- Parsing large RSS/Atom feeds in real-time, with item-by-item streaming and filtering.
- Handling compressed XML datasets for applications in web scraping, data aggregation, and news syndication.
- Memory-efficient parsing of large or continuous XML data streams without requiring the entire document in memory.

### DeepWiki Docs: [https://deepwiki.com/carlosplanchon/xmlstreamer](https://deepwiki.com/carlosplanchon/xmlstreamer)

## Installation

To install `xmlstreamer`, use uv:

```bash
uv add xmlstreamer
```

## Usage

```python
from xmlstreamer import StreamInterpreter

import pprint

url = "https://example.com/large-feed.xml"
separator_tag = "item"

interpreter = StreamInterpreter(
    url=url,
    separator_tag=separator_tag,
    buffer_size=1024 * 128,
    max_running_time=600  # 10 minutes
)

for item in interpreter:
    pprint.pprint(item)  # Process each parsed item as a dictionary
```

Define custom filters, encoding mappings, or buffer sizes as needed for optimal performance.

## Filtering Usage

To enable item filtering and alerts, create a subclass of `xmlstreamer.StreamInterpreter` with custom methods for filtering and alerting. Below is an example of setting up an item filter based on date and creating a zero-items alert.

### Step 1: Define an `ItemFilter`

The `ItemFilter` class specifies which items to keep based on date filtering criteria:

```python
import attrs
from attrs_strict import type_validator
from typing import Optional

@attrs.define
class ItemFilter:
    attrib: str = attrs.field(
        kw_only=True,
        validator=type_validator()
    )
    fmt: Optional[str] = attrs.field(
        validator=type_validator(),
        default=None
    )
    max_item_age_in_days: int = attrs.field(
        kw_only=True,
        validator=type_validator()
    )
```

- attrib: The XML tag or attribute to filter by.
- fmt: Optional date format for parsing dates within the attribute. If not provided, dateparser will be used for parsing.
- max_item_age_in_days: The maximum allowable age of items in days.

### Step 2: Define Helper Functions for Parsing and Filtering Dates
Functions to parse dates and evaluate if an item should be kept based on the specified date limit:

```python
from datetime import datetime, timedelta
from typing import Optional
import dateparser

def parse_date(string: str, fmt: str) -> Optional[datetime]:
    try:
        return datetime.strptime(string, fmt)
    except ValueError:
        return None

def eval_keep_date_item(string: str, fmt: str, limit_date: datetime) -> Optional[bool]:
    string = string.strip()
    parsed_date = parse_date(string, fmt) if fmt else dateparser.parse(string)
    return parsed_date.timestamp() > limit_date.timestamp() if parsed_date else None
```

- parse_date: Parses the date string with the specified format.
- eval_keep_date_item: Checks if the item’s date is within the allowable age limit.

### Step 3: Define the Filtering Function
The filter_parsed_item function applies the date filter to each parsed item:

```python
from typing import Optional, Dict, Any

def filter_parsed_item(ITEM_FILTER: ItemFilter, parsed_item: ParsedItem) -> Optional[ParsedItem]:
    attrib = ITEM_FILTER.attrib
    fmt = ITEM_FILTER.fmt
    max_item_age_in_days = ITEM_FILTER.max_item_age_in_days
    limit_date = datetime.now() - timedelta(days=max_item_age_in_days)
    item_content: Dict[str, Any] = parsed_item.parsed_content

    if attrib in item_content:
        dt = item_content[attrib]
        keep_item = eval_keep_date_item(dt, fmt, limit_date) if isinstance(dt, str) else None
        if keep_item is None:
            item_content[attrib] = None
            return parsed_item
        elif keep_item:
            return parsed_item

    return None
```

### Step 4: Extend StreamInterpreter for Filtering and Alerts
Create a subclass that enables filtering with filter_parsed_item and alerts if no items are parsed.

```python
from xmlstreamer import StreamInterpreter
from datetime import datetime
from pathlib import Path
import inspect

class CustomStreamInterpreter(StreamInterpreter):
    def __init__(self, **kwargs):
        kwargs["max_running_time"] = 3600  # Set max runtime to 1 hour
        super().__init__(**kwargs)

        stack = inspect.stack()
        fname = stack[1].filename
        fname_path = Path(fname)
        self.called_from = fname_path.stem

        self.alerts_enabled = True
        self.filter_parsed_item_func = filter_parsed_item

    def raise_stop_iteration(self):
        print(f"XMLSTREAMER STATS ITEMS PARSED: {self.stats_parsed_items}")
        if self.stats_parsed_items == 0:
            self.raise_zero_items_alert()
        raise StopIteration

    def raise_zero_items_alert(self):
        print("--- ZERO ITEMS ALERT ---")
        actual_date = datetime.now()
        running_time = actual_date - self.start_date
        print(f"Running time exceeded with no items parsed: {running_time}")
```

- CustomStreamInterpreter: Initializes with filter_parsed_item_func for item filtering.
- raise_zero_items_alert: Triggered if no items are parsed, printing a warning.

### Step 5: Run with Custom Filtering
To use filtering and alerting with your subclass:

```python
url = "https://example.com/large-feed.xml"
separator_tag = "item"
item_filter = ItemFilter(
    attrib="pubDate",
    fmt="%a, %d %b %Y %H:%M:%S %z",
    max_item_age_in_days=7
)

interpreter = CustomStreamInterpreter(
    url=url,
    separator_tag=separator_tag,
    item_filter=item_filter,
    buffer_size=1024 * 128,
)

for item in interpreter:
    print(item)  # Process each filtered item as a dictionary
```

This example demonstrates setting a pubDate filter that removes items older than 7 days. The CustomStreamInterpreter will also trigger an alert if no items are parsed within the set runtime.
