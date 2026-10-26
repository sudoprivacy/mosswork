# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow==12.3.0", "resvg-py==0.5.0"]
# ///
"""Regenerate application artwork from docs/assets/brand-mark.svg with uv run."""

from __future__ import annotations

import io
from pathlib import Path

import resvg_py
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "dashboard/public"
MARK = (ROOT / "docs/assets/brand-mark.svg").read_text()


def render(svg: str, width: int, height: int) -> Image.Image:
    return Image.open(
        io.BytesIO(resvg_py.svg_to_bytes(svg_string=svg, width=width, height=height))
    ).convert("RGBA")


def icon(size: int, artwork: int, color: str = "#F59E0B", background=None) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), background or (0, 0, 0, 0))
    glyph = render(MARK.replace("#F59E0B", color), artwork * 21 // 24, artwork)
    canvas.alpha_composite(glyph, ((size - glyph.width) // 2, (size - glyph.height) // 2))
    return canvas


def wordmark(name: str, dark: bool) -> str:
    color = "#ffffff" if dark else "#111827"
    glyph = MARK.replace("<svg ", '<svg x="30" y="45" width="174" height="199" ')
    size = 82 if name == "mosswork" else 74
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="753" height="289" '
        'viewBox="0 0 753 289">'
        + glyph
        + f'<text x="236" y="174" font-family="Arial, sans-serif" font-weight="700" '
        f'font-size="{size}" fill="{color}">{name}</text></svg>\n'
    )


def main() -> None:
    (PUBLIC / "logo_vertical_dark.svg").write_text(MARK)
    (PUBLIC / "logo_vertical_white.svg").write_text(MARK.replace("#F59E0B", "#ffffff"))
    (PUBLIC / "favico.svg").write_text(MARK)
    for name in ("mosswork", "moss-server"):
        for dark, suffix in ((False, "dark"), (True, "white")):
            image = render(wordmark(name, dark), 753, 289)
            image.save(PUBLIC / f"{name}_logo_{suffix}.png")
            if name == "moss-server":
                image.save(PUBLIC / f"logo_horizontal_{suffix}.png")
    (ROOT / "docs/assets/mosswork-logo.svg").write_text(wordmark("mosswork", False))
    for name, size in (("pwa-192.png", 192), ("pwa-512.png", 512), ("apple-touch-icon.png", 180)):
        icon(size, int(size * 0.7), background="#12141c").save(PUBLIC / name)
    build = ROOT / "desktop/src/build"
    icon(512, 470).save(build / "appicon.png")
    icon(1024, 824).save(build / "appicon-macos.png")
    assets = ROOT / "desktop/src/assets"
    icon(96, 84).save(assets / "tray-icon.png")
    icon(88, 64, color="#000000").save(assets / "tray-icon-template.png")
    for name, dimensions in (
        ("mosswork-mascot-peek.webp", (352, 320)),
        ("mosswork-mascot-type.webp", (856, 812)),
        ("mosswork-mascot-empty.png", (2048, 2048)),
        ("mosswork-mascot-tasks.png", (600, 600)),
    ):
        size = min(dimensions)
        canvas = Image.new("RGBA", dimensions)
        canvas.alpha_composite(icon(size, int(size * 0.72)), ((dimensions[0] - size) // 2, 0))
        options = {"lossless": True} if name.endswith("webp") else {}
        canvas.save(PUBLIC / name, **options)
        if (assets / name).exists():
            canvas.save(assets / name, **options)


if __name__ == "__main__":
    main()
