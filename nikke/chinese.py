"""Local OpenCC conversion with bundled dictionaries, independent of model/API locale."""

from functools import lru_cache

from ._vendor.opencc import OpenCC

_converter = OpenCC("t2s")


@lru_cache(maxsize=1024)
def simplified(text: str) -> str:
    return _converter.convert(text)
