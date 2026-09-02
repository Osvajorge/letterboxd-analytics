"""Read a remote page without letting it decide how much memory this run uses.

Every page this pipeline reads comes from a host it does not control, over a
connection that offers gzip. httpx decompresses a gzip body transparently and
imposes no size limit, so `response.text` hands the sending host a direct lever
on this process: 0.25 MB on the wire expands to 268 million characters, and the
run dies with the runner out of memory.

That is not a claim about Letterboxd or TMDB turning hostile. It is what a
hijacked edge, a hostile mirror reached through a DNS failure, or a compromised
CDN gets for free, and `fetch_lists.py` walks up to 320 pages per run, so one
bad host gets 320 chances.

The readers below stream the body and stop at a byte budget instead. A page past
the budget raises `ResponseTooLarge`, which fails the step and leaves the last
good data committed and published.
"""

from __future__ import annotations

import httpx

# The budget one page may spend, measured after decompression.
#
# The largest page this pipeline reads is a Letterboxd list page at roughly
# 350 KB, so 10 MB leaves a thirtyfold margin for a page that grows. Nothing
# healthy comes close, and nothing hostile gets to keep going.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class ResponseTooLarge(RuntimeError):
    """Raised when a response body runs past the byte budget.

    This is deliberately not a short read. Truncating the body would hand the
    parsers half a page, and half a list page parses cleanly into a list that
    lost most of its films, which is exactly the silent shrinkage the size gates
    elsewhere in this pipeline exist to catch.
    """


def _decode_within_budget(response: httpx.Response, url: str, max_bytes: int) -> str:
    """Collect a streaming body up to the budget, then decode it."""
    chunks: list[bytes] = []
    total = 0

    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge(
                f"{url} sent more than {max_bytes // (1024 * 1024)} MB of body, "
                f"which no real page on this site approaches. Nothing was "
                f"written and the last good data is still committed."
            )
        chunks.append(chunk)

    return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")


def read_text(
    client: httpx.Client, url: str, *, max_bytes: int = MAX_RESPONSE_BYTES
) -> str:
    """Read one page as text, raising on an error status or an oversized body."""
    with client.stream("GET", url) as response:
        response.raise_for_status()
        return _decode_within_budget(response, url, max_bytes)


def read_text_and_status(
    client: httpx.Client, url: str, *, max_bytes: int = MAX_RESPONSE_BYTES
) -> tuple[int, str]:
    """Read one page, handing back its status instead of raising on it.

    A caller that treats some statuses as answers rather than failures needs the
    number, not an exception. The body of a non-200 response is never read, so a
    hostile error page cannot spend the budget either.
    """
    with client.stream("GET", url) as response:
        if response.status_code != 200:
            return response.status_code, ""
        return response.status_code, _decode_within_budget(response, url, max_bytes)
