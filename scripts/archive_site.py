#!/usr/bin/env python3
"""Capture public source bytes, then rebuild a portable static archive offline."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import html
import json
import posixpath
from pathlib import Path
import re
import shutil
import time
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://nachtmensch-oder-fruehaufsteher.de"
HOST = urlsplit(ORIGIN).hostname
SNAPSHOT = ROOT / "snapshot"
SITE = ROOT / "docs"
ASSET_EXT = r"(?:css|js|png|jpe?g|webp|gif|svg|ico|woff2?|ttf|eot|otf|pdf|mp4|webm|mp3|ogg|zip)"
ABS_ASSET = re.compile(r'https?://[^\s<>"\'\\]+?\.' + ASSET_EXT + r'(?![a-zA-Z0-9])(?:\?[^\s<>"\'\\]*)?', re.I)
CSS_URL = re.compile(r'url\(\s*([\'\"]?)(.*?)\1\s*\)', re.I)
REMOVED = ("contact-form-7", "wpforms", "strato-assistant", "koko", "3d-flip-book", "pdfemb")
PRESS_PDF = ORIGIN + "/wp-content/uploads/2025/05/RoadshowPressemappe-1.pdf"
# Verified in Elementor's AssetsLoader (URLs are assembled at runtime).
RUNTIME_ASSETS = [ORIGIN + "/wp-content/plugins/elementor/assets/" + x for x in (
    "lib/dialog/dialog.min.js", "lib/share-link/share-link.min.js",
    "lib/swiper/v8/swiper.min.js", "lib/swiper/v8/css/swiper.min.css",
    "css/conditionals/lightbox.min.css", "css/conditionals/dialog.min.css",
)] + [ORIGIN + "/wp-content/uploads/elementor/css/custom-lightbox.min.css"]
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def canonical(url: str, base: str = ORIGIN + "/") -> str | None:
    url = html.unescape(url.strip()).replace("\\/", "/")
    if not url or url.startswith(("#", "data:", "blob:", "mailto:", "tel:", "javascript:")):
        return None
    u = urlsplit(urljoin(base, url))
    if u.scheme not in ("http", "https") or u.hostname not in (HOST, "www." + HOST):
        return None
    path = quote(unquote(u.path or "/"), safe="/%:@!$&'()*+,;=-._~")
    # Query strings on assets are only cache versions. Public content uses clean URLs.
    if u.query and not re.search(r"\." + ASSET_EXT + r"$", path, re.I):
        return None
    if any(x in path for x in ("/wp-admin", "/wp-json", "/xmlrpc.php", "/feed/", "/comments/")):
        return None
    return ORIGIN + path


def local_path(url: str) -> str:
    path = unquote(urlsplit(url).path).lstrip("/")
    if ".." in Path(path).parts:
        raise ValueError("Unsafe path: " + url)
    if not path or path.endswith("/"):
        path += "index.html"
    elif not Path(path).suffix:
        path += "/index.html"
    return path


def request(url: str) -> tuple[bytes, dict]:
    req = Request(url, headers={"User-Agent": "NachtmenschStaticArchive/1.0 (public preservation)", "Accept-Encoding": "identity"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=45) as response:
                return response.read(), {"status": response.status,
                    "content_type": response.headers.get("Content-Type", ""),
                    "last_modified": response.headers.get("Last-Modified"),
                    "final_url": response.url,
                    "captured_at": datetime.now(timezone.utc).isoformat()}
        except Exception:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def strip_services(soup: BeautifulSoup) -> None:
    for tag in list(soup.find_all("script")):
        identity = " ".join([tag.get("id", ""), tag.get("src", ""), tag.string or ""]).lower()
        if any(word in identity for word in REMOVED) or tag.get("type") == "application/ld+json":
            tag.decompose()
    for tag in list(soup.find_all("link")):
        if tag.decomposed:
            continue
        rel = tag.get("rel", [])
        if any(x in rel for x in ("alternate", "EditURI", "shortlink", "https://api.w.org/", "profile")):
            tag.decompose()


def discover(data: bytes, url: str, kind: str) -> set[str]:
    text = data.decode("utf-8", errors="replace")
    refs = set()
    if kind == "page":
        soup = BeautifulSoup(text, "html5lib")
        strip_services(soup)
        for tag in soup.find_all(True):
            for attr in ("href", "src", "poster", "data-src", "data-lazy-src"):
                value = tag.get(attr)
                if value and not (tag.name == "link" and "canonical" in tag.get("rel", [])):
                    refs.add(value)
            for attr in ("srcset", "data-srcset", "data-lazy-srcset"):
                if tag.get(attr):
                    refs.update(x.strip().split()[0] for x in tag[attr].split(",") if x.strip())
        text = str(soup)
        if soup.select("div._3d-flip-book"):
            refs.add(PRESS_PDF)
    if kind in ("page", "css", "js"):
        decoded = html.unescape(text).replace("\\/", "/")
        refs.update(ABS_ASSET.findall(decoded))
        if kind != "js":
            refs.update(m.group(2) for m in CSS_URL.finditer(decoded))
        if kind == "js":
            # Elementor loads these files lazily; plain page mirroring misses them.
            refs.update(re.findall(r'["\']([\w.\-]+\.bundle\.min\.js)["\']', text))
    result = set()
    for ref in refs:
        normalized = canonical(ref, url)
        if normalized and not any(x in normalized for x in ("/author/", "/category/", "/tag/")):
            result.add(normalized)
    return result


def capture() -> dict:
    SNAPSHOT.mkdir(exist_ok=True)
    manifest_path = SNAPSHOT / "manifest.json"
    # A resumed capture reuses successful downloads and never silently changes them.
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {
        "source": ORIGIN + "/", "started_at": datetime.now(timezone.utc).isoformat(),
        "files": [], "failures": [], "scope": "Public pages, posts, flipbook pages, and referenced same-origin assets; excludes author/category/tag indexes, feeds, APIs and administration."}
    files = {x["url"]: x for x in manifest["files"]}
    seeds = {ORIGIN + "/", *RUNTIME_ASSETS}
    if not files:
        for name in ("page-sitemap.xml", "post-sitemap.xml", "3d-flip-book-sitemap.xml"):
            data, _ = request(ORIGIN + "/" + name)
            (SNAPSHOT / name).write_bytes(data)
            tree = ET.fromstring(data)
            seeds.update(el.text for el in tree.findall("s:url/s:loc", NS))
        # Preserve the public attachment inventory used to resolve the press viewer.
        data, _ = request(ORIGIN + "/wp-json/wp/v2/media?per_page=100&media_type=application")
        (SNAPSHOT / "document-inventory.json").write_bytes(data)
        seeds.update(x["source_url"] for x in json.loads(data))
    else:
        for path in SNAPSHOT.glob("*-sitemap.xml"):
            seeds.update(el.text for el in ET.fromstring(path.read_bytes()).findall("s:url/s:loc", NS))
        inventory = SNAPSHOT / "document-inventory.json"
        if inventory.exists():
            seeds.update(x["source_url"] for x in json.loads(inventory.read_text()))
    for record in files.values():
        seeds.update(discover((SNAPSHOT / "files" / record["path"]).read_bytes(), record["url"], record["kind"]))
    failed = {}
    seen = set(files)
    pending = {canonical(x) for x in seeds} - seen - {None}

    def fetch(url):
        try:
            data, meta = request(url)
            path = local_path(url)
            suffix = Path(path).suffix.lower()
            kind = "page" if "text/html" in meta["content_type"] else {".css": "css", ".js": "js"}.get(suffix, "asset")
            if kind == "page" and suffix not in (".html", ".htm"):
                raise ValueError("Expected asset; received HTML")
            dest = SNAPSHOT / "files" / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return {"url": url, "path": path, "kind": kind, "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(), **meta}, discover(data, url, kind), None
        except Exception as error:
            return None, set(), {"url": url, "error": str(error)}

    while pending:
        batch = sorted(pending)
        seen.update(batch)
        pending = set()
        with ThreadPoolExecutor(max_workers=4) as pool:
            for record, refs, error in pool.map(fetch, batch):
                if error:
                    failed[error["url"]] = error
                    print("UNAVAILABLE", error["url"], error["error"], flush=True)
                else:
                    files[record["url"]] = record
                    pending.update(refs - seen)
        manifest["files"] = sorted(files.values(), key=lambda x: x["url"])
        manifest["failures"] = list(failed.values())
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        print(f"Captured {len(files)} files; {len(pending)} remaining references", flush=True)
        if len(seen) > 2500:
            raise RuntimeError("Capture exceeded the expected site size; inspect before continuing")
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def relative(target: str, current: str) -> str:
    return quote(posixpath.relpath(target, posixpath.dirname(current) or "."), safe="/.-_~")


def build() -> dict:
    manifest = json.loads((SNAPSHOT / "manifest.json").read_text())
    archive_style_version = hashlib.sha256((ROOT / "assets/archive.css").read_bytes()).hexdigest()[:12]
    archive_script_version = hashlib.sha256((ROOT / "assets/archive.js").read_bytes()).hexdigest()[:12]
    files = {x["url"]: x for x in manifest["files"]}
    date = manifest["started_at"][:10]
    modifications = []
    SITE.mkdir(exist_ok=True)

    def localize(value: str, url: str, path: str) -> str:
        if value.startswith("#"):
            return value
        key = canonical(value, url)
        if key in files:
            fragment = urlsplit(html.unescape(value)).fragment
            return relative(files[key]["path"], path) + ("#" + fragment if fragment else "")
        return value

    def css(text: str, url: str, path: str) -> str:
        def replace(match):
            key = canonical(match.group(2), url)
            if key and any(x["url"] == key for x in manifest["failures"]):
                modifications.append({"page": path, "change": "Unavailable source CSS image omitted: " + key})
                return "none"
            return 'url("' + localize(match.group(2), url, path) + '")'
        return CSS_URL.sub(replace, text)

    for url, rec in files.items():
        source = SNAPSHOT / "files" / rec["path"]
        dest = SITE / rec["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != rec["sha256"]:
            raise ValueError("Snapshot checksum mismatch: " + rec["path"])
        if rec["kind"] == "page":
            soup = BeautifulSoup(data.decode("utf-8"), "html5lib")
            strip_services(soup)
            for tag in soup.find_all(True):
                for attr in ("href", "src", "poster", "data-src", "data-lazy-src", "data-pdf"):
                    if tag.get(attr) and not (tag.name == "link" and "canonical" in tag.get("rel", [])):
                        tag[attr] = localize(tag[attr], url, rec["path"])
                for attr in ("srcset", "data-srcset", "data-lazy-srcset"):
                    if tag.get(attr):
                        pieces = []
                        for entry in tag[attr].split(","):
                            parts = entry.strip().split()
                            if parts:
                                pieces.append(" ".join([localize(parts[0], url, rec["path"]), *parts[1:]]))
                        tag[attr] = ", ".join(pieces)
                if tag.get("style"):
                    tag["style"] = css(tag["style"], url, rec["path"])
                # Elementor stores images/lightbox URLs in JSON data attributes.
                for attr in list(tag.attrs):
                    if attr.startswith("data-") and isinstance(tag[attr], str):
                        text = tag[attr].replace("\\/", "/")
                        tag[attr] = ABS_ASSET.sub(lambda m: localize(m.group(0), url, rec["path"]), text)
            for tag in soup.find_all("style"):
                if tag.string:
                    tag.string.replace_with(css(str(tag.string), url, rec["path"]))
            prefix = relative("index.html", rec["path"]).removesuffix("index.html") or "./"
            for tag in soup.find_all("script", src=False):
                if tag.string:
                    text = str(tag.string)
                    for origin in (ORIGIN, ORIGIN.replace("https:", "http:")):
                        text = text.replace((origin + "/").replace("/", "\\/"), prefix)
                        text = text.replace(origin + "/", prefix)
                    tag.string.replace_with(text)
            # Replace the server-backed flipbook with the preserved public PDF.
            for book in soup.select("div._3d-flip-book"):
                pdf = localize(PRESS_PDF, url, rec["path"])
                replacement = BeautifulSoup(f'<section class="archive-pdf"><p><a href="{pdf}">Pressemappe als PDF öffnen oder herunterladen</a></p><iframe src="{pdf}" title="Roadshow – Pressemappe" loading="lazy"></iframe></section>', "html.parser")
                book.replace_with(replacement)
                modifications.append({"page": rec["path"], "change": "WordPress flipbook replaced with local PDF"})
            for iframe in list(soup.find_all("iframe")):
                if urlsplit(iframe.get("src", "")).scheme in ("http", "https"):
                    link = soup.new_tag("a", href=iframe["src"])
                    link.string = "Externen Inhalt auf der Originalplattform öffnen"
                    iframe.replace_with(link)
            for form in soup.find_all("form"):
                for attr in ("action", "method", "onsubmit"):
                    form.attrs.pop(attr, None)
                form["data-archive-disabled"] = "true"
                form["aria-label"] = "Archiviertes Formular – Versand deaktiviert"
                note = soup.new_tag("p", attrs={"class": "archive-form-note", "role": "note"})
                note.string = "Archiviertes Formular: Über diese Archivseite können keine Nachrichten oder Anmeldungen versendet werden."
                form.insert(0, note)
                for field in form.find_all(["input", "select", "textarea", "button"]):
                    field["disabled"] = ""
                    field.attrs.pop("required", None)
                modifications.append({"page": rec["path"], "change": "Form submission disabled"})
            for widget in soup.select(".elementor-widget-wpforms"):
                if not widget.find("form"):
                    note = soup.new_tag("p", attrs={"class": "archive-form-note", "role": "note"})
                    note.string = "Archivhinweis: Das Kontaktformular war beim Abruf nicht verfügbar. Über diese Archivseite können keine Nachrichten versendet werden."
                    widget.append(note)
                    modifications.append({"page": rec["path"], "change": "Empty original contact-form widget labelled unavailable"})
            if not soup.head or not soup.body:
                raise ValueError("Incomplete HTML document: " + url)
            if not soup.title:
                raise ValueError("Missing original title: " + url)
            soup.title.string = soup.title.get_text() + " | Archiv"
            for meta in soup.select('meta[name="robots"]'):
                meta.decompose()
            soup.head.append(soup.new_tag("meta", attrs={"name": "robots", "content": "noindex, follow"}))
            soup.head.append(soup.new_tag("meta", attrs={"name": "archive-source", "content": url}))
            soup.head.append(soup.new_tag("meta", attrs={"name": "archive-captured", "content": date}))
            policy = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; frame-src 'self'; worker-src 'self' blob:; connect-src 'none'; form-action 'none'; base-uri 'none'"
            csp = soup.new_tag("meta", attrs={"http-equiv": "Content-Security-Policy", "content": policy})
            soup.head.insert(1, csp)
            soup.head.append(soup.new_tag("link", rel="stylesheet", href=relative("archive-assets/archive.css", rec["path"]) + "?v=" + archive_style_version))
            script = soup.new_tag("script", src=relative("archive-assets/archive.js", rec["path"]) + "?v=" + archive_script_version, defer="")
            soup.head.append(script)
            banner = BeautifulSoup(f'<aside class="archive-banner" aria-label="Projektabschluss und Archivhinweis / Project completion and archive notice"><div class="archive-banner-copy"><p lang="de"><strong>Das Projekt ist abgeschlossen.</strong> Diese Website bleibt zu Dokumentations- und Archivzwecken erhalten.</p><p lang="en"><strong>This project has ended.</strong> This website is preserved for documentation and archival purposes.</p></div><div class="archive-banner-meta"><span>Archivstand / Snapshot: <time datetime="{date}">{date[8:10]}.{date[5:7]}.{date[:4]}</time></span><a href="{relative("archive/index.html", rec["path"])}"><span lang="de">Über dieses Archiv</span> / <span lang="en">About this archive</span></a></div></aside>', "html.parser")
            soup.body.insert(0, banner)
            dest.write_text(str(soup), encoding="utf-8")
        elif rec["kind"] == "css":
            dest.write_text(css(data.decode("utf-8"), url, rec["path"]), encoding="utf-8")
        else:
            shutil.copyfile(source, dest)
    # The site's custom lightbox CSS returns 404. Use the original plugin's
    # standard stylesheet at the URL the original loader expects.
    lightbox = SITE / "wp-content/uploads/elementor/css/custom-lightbox.min.css"
    lightbox.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SITE / "wp-content/plugins/elementor/assets/css/conditionals/lightbox.min.css", lightbox)
    modifications.append({"page": str(lightbox.relative_to(SITE)),
        "change": "Missing custom lightbox stylesheet replaced with captured Elementor standard lightbox stylesheet"})
    (SITE / "archive-assets").mkdir(exist_ok=True)
    for path in (ROOT / "assets").iterdir():
        shutil.copyfile(path, SITE / "archive-assets" / path.name)
    (SITE / ".nojekyll").touch()
    (SITE / "404.html").write_text('<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Seite nicht im Archiv</title><body style="font:18px system-ui;max-width:42rem;margin:10vh auto;padding:2rem"><h1>Diese Seite ist nicht im Archiv.</h1><p>Bitte verwenden Sie die Zurück-Funktion Ihres Browsers oder die Navigation einer archivierten Seite.</p></body></html>')
    report = {"source": manifest["source"], "captured_at": manifest["started_at"],
              "page_count": sum(x["kind"] == "page" for x in files.values()),
              "file_count": len(files), "source_bytes": sum(x["bytes"] for x in files.values()),
              "modifications": modifications, "capture_failures": manifest["failures"]}
    (ROOT / "build-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def validate() -> dict:
    """Check every emitted static reference, including CSS, srcsets and lazy chunks."""
    errors, external = [], set()
    checked = 0
    def check(value: str, source: Path):
        nonlocal checked
        if not value or value.startswith(("#", "data:", "blob:", "mailto:", "tel:", "javascript:")):
            return
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            external.add(value)
            return
        checked += 1
        if value.startswith("/"):
            errors.append(f"Root-relative reference breaks project Pages: {source.relative_to(SITE)} → {value}")
            return
        target = (source.parent / unquote(parsed.path)).resolve()
        if not target.is_relative_to(SITE.resolve()):
            errors.append(f"Reference escapes site: {value}")
        elif not target.exists():
            errors.append(f"Missing: {source.relative_to(SITE)} → {value}")

    for path in SITE.rglob("*"):
        if not path.is_file() or "archive" in path.relative_to(SITE).parts:
            continue
        if path.suffix == ".html":
            soup = BeautifulSoup(path.read_text(), "html5lib")
            if not soup.title or not soup.title.get_text().strip():
                errors.append(f"Missing page title: {path.relative_to(SITE)}")
            if path.name != "404.html" and not soup.select('link[rel="stylesheet"]'):
                errors.append(f"Missing stylesheets: {path.relative_to(SITE)}")
            for tag in soup.find_all(True):
                for attr in ("href", "src", "poster", "data-src", "data-lazy-src"):
                    if tag.get(attr) and not (tag.name == "link" and "canonical" in tag.get("rel", [])):
                        check(tag[attr], path)
                for attr in ("srcset", "data-srcset", "data-lazy-srcset"):
                    if tag.get(attr):
                        for part in tag[attr].split(","):
                            if part.strip():
                                check(part.strip().split()[0], path)
                for match in CSS_URL.finditer(tag.get("style", "")):
                    check(match.group(2), path)
            for tag in soup.find_all("style"):
                for match in CSS_URL.finditer(tag.get_text()):
                    check(match.group(2), path)
            for form in soup.find_all("form"):
                if form.get("action") or any(not x.has_attr("disabled") for x in form.find_all(["input", "button", "select", "textarea"])):
                    errors.append(f"Enabled form: {path}")
        elif path.suffix == ".css":
            for match in CSS_URL.finditer(path.read_text()):
                check(match.group(2), path)
        elif path.name.startswith("webpack") and path.suffix == ".js":
            for name in re.findall(r'["\']([\w.\-]+\.bundle\.min\.js)["\']', path.read_text()):
                check(name, path)
    pages_verified = 0
    manifest = json.loads((SNAPSHOT / "manifest.json").read_text())
    for record in manifest["files"]:
        if record["kind"] != "page":
            continue
        source = BeautifulSoup((SNAPSHOT / "files" / record["path"]).read_text(), "html5lib")
        archived = BeautifulSoup((SITE / record["path"]).read_text(), "html5lib")
        headings = lambda s: [x.get_text(" ", strip=True) for x in s.select("h1,h2,h3,h4,h5,h6")]
        if headings(source) != headings(archived):
            errors.append("Original headings changed: " + record["path"])
        if len(source.find_all("img")) != len(archived.find_all("img")):
            errors.append("Original image count changed: " + record["path"])
        original_styles = len(source.select('link[rel="stylesheet"]'))
        if len(archived.select('link[rel="stylesheet"]')) < original_styles:
            errors.append("Original stylesheets lost: " + record["path"])
        for tag in archived.select('script[src], img[src], iframe[src], link[rel="stylesheet"][href]'):
            value = tag.get("src") or tag.get("href")
            if value.startswith(("http:", "https:", "//")):
                errors.append("Active external dependency: " + value)
        # Check visible source text independently of the transformation logic.
        # Added archive notices may interleave words, but no original words may disappear.
        def visible_words(soup):
            for tag in list(soup.select('script,style,noscript,.archive-banner,.archive-form-note,.archive-pdf')):
                if not tag.decomposed:
                    tag.decompose()
            return soup.body.get_text(" ", strip=True).split()
        original_words = visible_words(source)
        archived_words = iter(visible_words(archived))
        for word in original_words:
            if not any(word == other for other in archived_words):
                errors.append("Original visible text lost: " + record["path"])
                break
        pages_verified += 1
    for url in RUNTIME_ASSETS:
        check(relative(local_path(url), "index.html"), SITE / "index.html")
    result = {"checked_local_references": checked, "pages_verified_against_original": pages_verified,
              "errors": sorted(set(errors)), "external_links": sorted(external)}
    (ROOT / "validation-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture", "build", "validate"))
    args = parser.parse_args()
    result = {"capture": capture, "build": build, "validate": validate}[args.command]()
    if args.command != "capture":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "validate" and result["errors"]:
        raise SystemExit(1)
