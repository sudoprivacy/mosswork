"""Verify the Apple Silicon installer served by the SudoWork download button."""

import argparse
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

PAGE = "https://sudowork.sudoprivacy.com/"
BUTTON = "mosswork-download-macos-arm"
ORIGINS = {
    "sudowork.sudoprivacy.com",
    "sudowork-release-1309794936.cos.ap-beijing.myqcloud.com",
    "sudowork-release-1309794936.cos.accelerate.myqcloud.com",
}


def public_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in ORIGINS:
        raise ValueError("Unexpected origin for the public Mac download")
    return url


class PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, public_url(newurl))


class DownloadPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.base = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(reversed(attrs))
        if tag == "base" and self.base is None and attributes.get("href") is not None:
            self.base = attributes["href"]
        if tag == "a" and attributes.get("id") == BUTTON:
            self.links.append(attributes.get("href"))


def signed_image_digest(image):
    status = json.loads((image.parent / "mosswork-darwin-arm64-build.json").read_text())
    if not (
        status["signing"] == "developer-id"
        and status["native_private_payload_verified"]["darwin-arm64"] is True
        and {"stapled-ticket", "gatekeeper"} <= set(status["macos_distribution"]["checks"])
    ):
        raise ValueError("Missing signed bootstrap distribution evidence")
    with image.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != status["macos_distribution"]["artifacts"][image.name]:
        raise ValueError("Installer differs from the verified distribution")
    return digest


def verify_button(expected_digest, expected_size, opener):
    with opener.open(PAGE, timeout=45) as page:
        parser = DownloadPage()
        parser.feed(page.read().decode("utf-8"))
        parser.close()
        if len(parser.links) != 1 or not parser.links[0]:
            raise ValueError("Expected one Apple Silicon download button with an href")
        base = urljoin(page.geturl(), parser.base or "")
        url = public_url(urljoin(base, parser.links[0]))
    digest = hashlib.sha256()
    count = 0
    with opener.open(url, timeout=90) as response:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            count += len(chunk)
            if count > expected_size:
                raise ValueError("Public installer exceeds the signed file size")
            digest.update(chunk)
    if count != expected_size or digest.hexdigest() != expected_digest:
        raise ValueError("Public Mac download differs from the signed installer")
    return {"page": PAGE, "button": BUTTON, "href": url, "size": count, "sha256": expected_digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--check-button", action="store_true")
    args = parser.parse_args()
    digest = signed_image_digest(args.image)
    if args.check_button:
        result = verify_button(digest, args.image.stat().st_size, build_opener(PublicRedirects()))
        print(json.dumps(result))


if __name__ == "__main__":
    main()
