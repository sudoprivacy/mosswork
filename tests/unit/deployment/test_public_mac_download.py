import hashlib
import io
import json
from urllib.request import Request

import pytest
from tools.verify_public_mac_download import (
    BUTTON,
    PAGE,
    PublicRedirects,
    signed_image_digest,
    verify_button,
)

DMG = b"a qualified disk image"
SHA = hashlib.sha256(DMG).hexdigest()
ORIGIN = "https://sudowork-release-1309794936.cos.ap-beijing.myqcloud.com"
LINK = f'<base href="{ORIGIN}/latest/"><a id="{BUTTON}" href="apple.dmg">Mac</a>'


class Responses:
    def __init__(self, html, dmg):
        self.html = html
        self.dmg = dmg
        self.urls = []

    def open(self, url, timeout):
        self.urls.append(url)
        response = io.BytesIO(self.html.encode() if url == PAGE else self.dmg)
        response.geturl = lambda: url
        return response


def test_matches_signed_file_through_the_actual_link():
    site = Responses(LINK, DMG)
    result = verify_button(SHA, len(DMG), site)
    assert site.urls == [PAGE, f"{ORIGIN}/latest/apple.dmg"]
    assert result["sha256"] == SHA
    assert result["size"] == len(DMG)


@pytest.mark.parametrize("html", ["", f'<a id="{BUTTON}"></a>', LINK + LINK])
def test_missing_or_ambiguous_link_stops_before_download(html):
    site = Responses(html, DMG)
    with pytest.raises(ValueError, match="Expected one"):
        verify_button(SHA, len(DMG), site)
    assert site.urls == [PAGE]


@pytest.mark.parametrize("dmg", [b"x" * len(DMG), DMG[:-1], DMG + b"!"])
def test_stale_or_partial_download_refused(dmg):
    with pytest.raises(ValueError, match="signed"):
        verify_button(SHA, len(DMG), Responses(LINK, dmg))


def test_link_and_redirect_origins_are_checked_before_fetch():
    site = Responses(f'<a id="{BUTTON}" href="https://other.example/file">Mac</a>', DMG)
    with pytest.raises(ValueError, match="origin"):
        verify_button(SHA, len(DMG), site)
    assert site.urls == [PAGE]
    with pytest.raises(ValueError, match="origin"):
        PublicRedirects().redirect_request(
            Request(PAGE), None, 302, "", {}, "http://other.example/"
        )


def test_expected_digest_comes_from_macs_verified_bootstrap_build(tmp_path):
    image = tmp_path / "mosswork-company-macos-arm64-1.2.3.dmg"
    image.write_bytes(DMG)
    status = {
        "signing": "developer-id",
        "native_private_payload_verified": {"darwin-arm64": True},
        "macos_distribution": {
            "checks": ["stapled-ticket", "gatekeeper"],
            "artifacts": {image.name: SHA},
        },
    }
    record = tmp_path / "mosswork-darwin-arm64-build.json"
    record.write_text(json.dumps(status))
    assert signed_image_digest(image) == SHA
    image.write_bytes(b"x" * len(DMG))
    with pytest.raises(ValueError, match="verified distribution"):
        signed_image_digest(image)
    status["native_private_payload_verified"]["darwin-arm64"] = False
    record.write_text(json.dumps(status))
    with pytest.raises(ValueError, match="distribution evidence"):
        signed_image_digest(image)
