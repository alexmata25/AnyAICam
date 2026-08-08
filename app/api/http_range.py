"""Strict single-range parsing for browser-compatible media responses."""

from __future__ import annotations

from dataclasses import dataclass


class InvalidRangeHeader(ValueError):
    """The Range header is malformed or uses an unsupported format."""


class UnsatisfiableRange(ValueError):
    """The requested range cannot be satisfied for the resource."""


@dataclass(frozen=True, slots=True)
class ByteRange:
    """An inclusive byte range within a resource."""

    start: int
    end: int
    resource_size: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.resource_size}"


def parse_single_range(range_header: str, resource_size: int) -> ByteRange:
    """Parse one RFC 7233-style byte range.

    Supported forms are ``bytes=start-end``, ``bytes=start-`` and
    ``bytes=-suffix_length``. Multiple ranges are deliberately rejected because
    AnyAICam serves one contiguous media response at a time.
    """

    if resource_size < 0:
        raise ValueError("resource_size must not be negative")
    if resource_size == 0:
        raise UnsatisfiableRange("empty resources have no satisfiable byte range")
    if not isinstance(range_header, str) or not range_header.strip():
        raise InvalidRangeHeader("Range header is required")

    unit, separator, value = range_header.strip().partition("=")
    if separator != "=" or unit.lower() != "bytes":
        raise InvalidRangeHeader("only the bytes range unit is supported")
    if "," in value:
        raise InvalidRangeHeader("multiple byte ranges are not supported")

    start_text, dash, end_text = value.strip().partition("-")
    if dash != "-" or (not start_text and not end_text):
        raise InvalidRangeHeader("malformed byte range")

    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise InvalidRangeHeader("suffix length must be positive")
            suffix_length = min(suffix_length, resource_size)
            return ByteRange(resource_size - suffix_length, resource_size - 1, resource_size)

        start = int(start_text)
        if start < 0:
            raise InvalidRangeHeader("range start must not be negative")
        if start >= resource_size:
            raise UnsatisfiableRange("range starts beyond the resource")

        if end_text:
            end = int(end_text)
            if end < start:
                raise InvalidRangeHeader("range end precedes range start")
            end = min(end, resource_size - 1)
        else:
            end = resource_size - 1
    except ValueError as exc:
        if isinstance(exc, (InvalidRangeHeader, UnsatisfiableRange)):
            raise
        raise InvalidRangeHeader("range bounds must be integers") from exc

    return ByteRange(start, end, resource_size)
