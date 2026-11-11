"""Find and download NBS Price Watch workbooks.

Where the data lives (verified November 2026):

* Since November 2024 NBS publishes the Price Watch series in its microdata
  catalog: entry 157 is petrol (PMS), entry 158 is diesel (AGO). Each month is
  a ZIP (PDF report + Excel tables), or in older months a bare .xlsx next to a
  .pdf.
* Before that, tables were served from nigerianstat.gov.ng/resource/ as
  PMS*.xlsx, DIESEL*.xlsx and AGO*.xlsx. That eLibrary stopped updating in
  December 2024.
* Both NBS hosts are frequently unreachable, so everything can also be fetched
  from the Internet Archive's Wayback Machine, which mirrors the same files.

Only the Excel tables are kept; PDFs are never stored.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("fuelapi.sources")

USER_AGENT = os.environ.get(
    "FUELAPI_USER_AGENT", "nigeria-fuel-prices-ingester/0.1 (open-data script)"
)
CATALOGS = {157: "pms", 158: "ago"}  # NBS microdata catalog entry -> fuel
MICRODATA_HOST = "microdata.nigerianstat.gov.ng"
LEGACY_RESOURCES = (
    "nigerianstat.gov.ng/resource/pms*",
    "nigerianstat.gov.ng/resource/diesel*",
    "nigerianstat.gov.ng/resource/ago*",
)
CDX_API = "https://web.archive.org/cdx/search/cdx"

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_MEMBER_BYTES = 10 * 1024 * 1024
POLITE_DELAY_SECONDS = 1.5


@dataclass(frozen=True)
class Candidate:
    key: str  # stable id, e.g. "microdata-157-1424" or "legacy-pms-oct-2024-report"
    url: str  # where to download it from
    origin_url: str  # the NBS URL it represents
    via: str  # "nbs" or "wayback"
    filename: str | None = None
    title: str | None = None
    wayback_timestamp: str | None = None


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _get(session: requests.Session, url: str, *, params=None, timeout=60, attempts=3) -> requests.Response:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            # (connect, read): a dead NBS host should fail fast, not hang the run
            r = session.get(url, params=params, timeout=(10, timeout))
            if r.status_code >= 500 or r.status_code == 429:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:  # noqa: PERF203
            last = exc
            if attempt + 1 < attempts:
                time.sleep(5 * (attempt + 1))
    assert last is not None
    raise last


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def _catalog_url(catalog_id: int, tail: str = "") -> str:
    return f"https://{MICRODATA_HOST}/index.php/catalog/{catalog_id}{tail}"


def discover_nbs(session: requests.Session) -> list[Candidate]:
    """List resources straight from the NBS microdata catalog pages."""
    out: list[Candidate] = []
    for catalog_id in CATALOGS:
        html = _get(session, _catalog_url(catalog_id, "/related-materials"),
                    timeout=45, attempts=2).text
        out.extend(parse_microdata_listing(html, catalog_id))
    return out


def parse_microdata_listing(html: str, catalog_id: int) -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Candidate] = {}
    for a in soup.select("a.download[href]"):
        m = re.search(r"/catalog/(\d+)/download/(\d+)", a["href"])
        if not m or int(m.group(1)) != catalog_id:
            continue
        filename = a.get("data-filename") or a.get("title") or None
        ext = (a.get("data-extension") or (filename or "").rsplit(".", 1)[-1]).lower()
        if ext == "pdf":
            continue
        rid = m.group(2)
        title = None
        info = soup.find(id=rid)
        if info is not None:
            title = " ".join(info.get_text(" ").split()) or None
        url = _catalog_url(catalog_id, f"/download/{rid}")
        found[rid] = Candidate(
            key=f"microdata-{catalog_id}-{rid}", url=url, origin_url=url,
            via="nbs", filename=filename, title=title,
        )
    return [found[k] for k in sorted(found, key=int)]


def _cdx(session: requests.Session, url_pattern: str) -> list[dict]:
    params = {
        "url": url_pattern,
        "output": "json",
        "filter": "statuscode:200",
        "fl": "timestamp,original,mimetype,length",
        "limit": "5000",
    }
    rows = _get(session, CDX_API, params=params, timeout=90).json()
    if not rows:
        return []
    head, *body = rows
    return [dict(zip(head, r)) for r in body]


def discover_wayback_microdata(session: requests.Session) -> list[Candidate]:
    """List microdata catalog downloads that the Wayback Machine has copies of."""
    out = []
    for catalog_id in CATALOGS:
        best: dict[str, dict] = {}
        names: dict[str, str] = {}
        pattern = f"{MICRODATA_HOST}/index.php/catalog/{catalog_id}/download/*"
        for row in _cdx(session, pattern):
            m = re.search(r"/download/(\d+)(?:/([^?#]+))?", row["original"])
            if not m:
                continue
            rid = m.group(1)
            if m.group(2):
                names[rid] = unquote(m.group(2))
            if rid not in best or row["timestamp"] > best[rid]["timestamp"]:
                best[rid] = row
        for rid in sorted(best, key=int):
            filename = names.get(rid)
            if filename and filename.lower().endswith(".pdf"):
                continue
            row = best[rid]
            out.append(Candidate(
                key=f"microdata-{catalog_id}-{rid}",
                url=f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}",
                origin_url=_catalog_url(catalog_id, f"/download/{rid}"),
                via="wayback", filename=filename, wayback_timestamp=row["timestamp"],
            ))
    return out


def discover_wayback_legacy(session: requests.Session) -> list[Candidate]:
    """List pre-2025 petrol and diesel workbooks from the old NBS eLibrary."""
    best: dict[str, dict] = {}
    rows = [row for pattern in LEGACY_RESOURCES for row in _cdx(session, pattern)]
    for row in rows:
        filename = unquote(row["original"].rsplit("/", 1)[-1]).strip()
        if not filename.lower().endswith(".xlsx"):
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", filename[:-5].lower()).strip("-")
        if slug not in best or row["timestamp"] > best[slug]["timestamp"]:
            best[slug] = {**row, "filename": filename}
    out = []
    for slug in sorted(best):
        row = best[slug]
        out.append(Candidate(
            key=f"legacy-{slug}",
            url=f"https://web.archive.org/web/{row['timestamp']}id_/{row['original']}",
            origin_url=row["original"], via="wayback", filename=row["filename"],
            wayback_timestamp=row["timestamp"],
        ))
    return out


# --------------------------------------------------------------------------
# Download + unpack
# --------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return base or "workbook.xlsx"


def _is_xlsx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "xl/workbook.xml" in z.namelist()
    except zipfile.BadZipFile:
        return False


def extract_workbooks(data: bytes, filename: str | None) -> list[tuple[str, bytes]]:
    """Return [(name, xlsx bytes)] from a download: a bare .xlsx, or a ZIP of them.

    Raises ValueError if the payload is something else entirely (an HTML error
    page, for instance). Returns [] for a PDF or a ZIP with no workbook.
    """
    if data[:4] == b"%PDF":
        return []
    if data[:2] != b"PK":
        raise ValueError(f"unexpected content (starts with {data[:16]!r})")
    if _is_xlsx(data):
        return [(_safe_name(filename or "workbook.xlsx"), data)]
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = info.filename
            if info.is_dir() or not name.lower().endswith(".xlsx"):
                continue
            if name.rsplit("/", 1)[-1].startswith("~$"):
                continue  # Excel lock file
            if info.file_size > MAX_MEMBER_BYTES:
                raise ValueError(f"{name} is implausibly large ({info.file_size} bytes)")
            member = z.read(info)
            if _is_xlsx(member):
                out.append((_safe_name(name), member))
    return out


def download(session: requests.Session, cand: Candidate) -> bytes:
    r = _get(session, cand.url, timeout=120)
    if len(r.content) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"download is implausibly large ({len(r.content)} bytes)")
    return r.content


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def load_manifest(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {e["key"]: e for e in json.loads(path.read_text(encoding="utf-8"))}


def save_manifest(path: Path, manifest: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [manifest[k] for k in sorted(manifest)]
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def ingest(data_dir: Path, *, backfill: bool = False, wayback_only: bool = False,
           limit: int | None = None) -> dict:
    """Download workbooks not yet in the manifest. Returns a small summary."""
    manifest_path = data_dir / "sources.json"
    manifest = load_manifest(manifest_path)
    session = new_session()
    summary = {"new": [], "failed": [], "discovery_errors": []}

    candidates: dict[str, Candidate] = {}

    def add(found: list[Candidate]) -> None:
        for c in found:
            candidates.setdefault(c.key, c)

    nbs_ok = False
    if not wayback_only:
        try:
            add(discover_nbs(session))
            nbs_ok = True
        except Exception as exc:  # noqa: BLE001 - NBS is routinely down
            log.warning("NBS catalog unreachable (%s); falling back to the Wayback Machine", exc)
            summary["discovery_errors"].append(f"nbs: {exc}")
    if not nbs_ok or backfill:
        try:
            add(discover_wayback_microdata(session))
        except Exception as exc:  # noqa: BLE001
            log.warning("Wayback lookup failed: %s", exc)
            summary["discovery_errors"].append(f"wayback-microdata: {exc}")
    if backfill:
        try:
            add(discover_wayback_legacy(session))
        except Exception as exc:  # noqa: BLE001
            log.warning("Wayback legacy lookup failed: %s", exc)
            summary["discovery_errors"].append(f"wayback-legacy: {exc}")

    todo = [c for k, c in sorted(candidates.items()) if k not in manifest]
    if limit is not None:
        todo = todo[:limit]
    log.info("%d candidate files, %d new", len(candidates), len(todo))

    for i, cand in enumerate(todo):
        if i:
            time.sleep(POLITE_DELAY_SECONDS)
        try:
            payload = download(session, cand)
            books = extract_workbooks(payload, cand.filename)
        except Exception as exc:  # noqa: BLE001
            log.warning("skip %s: %s", cand.key, exc)
            summary["failed"].append({"key": cand.key, "error": str(exc)})
            continue
        files = []
        for name, blob in books:
            rel = Path("raw") / "nbs" / f"{cand.key}__{name}"
            (data_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (data_dir / rel).write_bytes(blob)
            files.append({
                "path": rel.as_posix(),
                "name": name,
                "sha256": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob),
            })
        manifest[cand.key] = {
            "key": cand.key,
            "origin_url": cand.origin_url,
            "via": cand.via,
            "wayback_timestamp": cand.wayback_timestamp,
            "filename": cand.filename,
            "title": cand.title,
            "status": "ok" if files else "no_tables",
            "files": files,
            "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        save_manifest(manifest_path, manifest)  # after each file: resumable
        summary["new"].append(cand.key)
        log.info("fetched %s (%s, %d workbook%s)", cand.key, cand.via, len(files),
                 "" if len(files) == 1 else "s")

    return summary
