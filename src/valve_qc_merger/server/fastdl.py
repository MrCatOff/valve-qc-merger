"""Check a FastDL mirror: is every file there, with the right size?

Clients download what the server precaches from ``sv_downloadurl`` +
``/<path>`` (the precache path, as is — a Linux web server is
case-sensitive). A file the mirror lacks falls back to the slow in-game
download (or fails, with ``sv_allowdownload 0``); a stale copy of another
size is a broken model on the client. :func:`check` asks the mirror for
each file of a local folder (``HEAD``; a ranged ``GET`` where ``HEAD`` is
refused) a few at a time and compares sizes.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from valve_qc_merger.server.scan import scan_folder

STATES = ("missing", "size", "error", "ok")  # worst first
USER_AGENT = "valve-qc-merger FastDL check"


@dataclass
class Result:
    path: str
    state: str  # one of STATES
    local_size: int
    remote_size: int | None = None
    detail: str = ""


def file_url(base: str, path: str) -> str:
    """``base`` + ``/`` + the URL-quoted ``path`` (one slash between)."""
    return base.rstrip("/") + "/" + urllib.parse.quote(path.replace("\\", "/"))


def local_files(folder: Path) -> dict[str, int]:
    """The files a client may download from ``folder`` (a mod folder or an
    exported package's ``cstrike/``): resource folders and top-level WADs."""
    return dict(scan_folder(folder).files)


def _remote_size(url: str, timeout: float) -> tuple[int | None, int | None]:
    """(HTTP status, size) of ``url``; status None when nothing answered."""
    for method, headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        request = urllib.request.Request(url, method=method,
                                         headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                status = response.status
                if method == "GET" and status == 206:
                    total = response.headers.get("Content-Range", "").rsplit("/", 1)[-1]
                    return 200, int(total) if total.isdigit() else None
                length = response.headers.get("Content-Length")
                return status, int(length) if length and length.isdigit() else None
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in (403, 405, 501):
                continue  # some hosts refuse HEAD: try a one-byte GET
            return exc.code, None
    return None, None  # pragma: no cover - the GET branch always returns


def check_one(base: str, path: str, local_size: int, timeout: float = 10.0) -> Result:
    url = file_url(base, path)
    try:
        status, size = _remote_size(url, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return Result(path, "error", local_size, None, f"no answer: {reason}")
    if status == 404 or status == 410:
        return Result(path, "missing", local_size, None, f"not on the mirror ({status})")
    if status is None or status >= 400:
        return Result(path, "error", local_size, None, f"HTTP {status}")
    if size is not None and size != local_size:
        return Result(path, "size", local_size, size,
                      f"{size} bytes on the mirror, {local_size} here (stale copy?)")
    return Result(path, "ok", local_size, size, "" if size is not None
                  else "there (the server did not say its size)")


def check(base: str, files: dict[str, int], *, workers: int = 8, timeout: float = 10.0,
          progress: Callable[[int, int, str], bool | None] | None = None) -> list[Result]:
    """Every file of ``files`` (path -> local size) on the mirror at
    ``base``, worst first. ``progress(done, total, path)`` returning True
    stops early (the files not asked yet are left out)."""
    results: list[Result] = []
    paths = sorted(files, key=str.lower)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(check_one, base, p, files[p], timeout): p for p in paths}
        for done, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            if progress is not None and progress(done, len(paths), futures[future]):
                for other in futures:
                    other.cancel()
                break
    order = {s: i for i, s in enumerate(STATES)}
    return sorted(results, key=lambda r: (order[r.state], r.path.lower()))


def summary(results: list[Result]) -> dict[str, int]:
    counts = dict.fromkeys(STATES, 0)
    for result in results:
        counts[result.state] += 1
    return counts


def report(base: str, results: list[Result]) -> str:
    lines = [f"FastDL check of {base}"]
    lines += [f"[{r.state}] {r.path}" + (f" — {r.detail}" if r.detail else "")
              for r in results if r.state != "ok"]
    counts = summary(results)
    lines.append(", ".join(f"{counts[s]} {s}" for s in STATES))
    return "\n".join(lines) + "\n"


__all__ = ["Result", "STATES", "check", "check_one", "file_url", "local_files", "report",
           "summary"]
