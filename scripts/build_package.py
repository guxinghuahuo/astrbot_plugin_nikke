"""Package only plugin sources; never include local credentials or runtime state."""

import hashlib
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "__init__.py",
    "main.py",
    "metadata.yaml",
    "logo.png",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "docs/configuration.md",
    "docs/releasing.md",
    "docs/images/qq-preview.jpg",
    "docs/images/character-skills-preview.png",
    "nikke/__init__.py",
    "nikke/config.py",
    "nikke/command_correction.py",
    "nikke/translation.py",
    "nikke/translation_store.py",
    "nikke/prompts.py",
    "nikke/classifier.py",
    "nikke/jev.py",
    "nikke/chat.py",
    "nikke/characters.py",
    "nikke/character_cards.py",
    "nikke/feed.py",
    "nikke/blablalink.py",
    "nikke/network.py",
    "nikke/presentation.py",
    "nikke/service.py",
    "nikke/image_fallback.py",
    "nikke/image_fallback_store.py",
    "nikke/calendar.py",
    "nikke/chinese.py",
    "nikke/_vendor/__init__.py",
    "nikke/_vendor/opencc/__init__.py",
    "nikke/_vendor/opencc/opencc.py",
    "nikke/_vendor/opencc/config/t2s.json",
    "nikke/_vendor/opencc/dictionary/TSCharacters.txt",
    "nikke/_vendor/opencc/dictionary/TSPhrases.txt",
    "nikke/_vendor/opencc/LICENSE.txt",
    "nikke/_vendor/opencc/NOTICE.txt",
    "nikke/_vendor/opencc/UPSTREAM.md",
    "nikke/calendar_store.py",
    "nikke/calendar_service.py",
    "scripts/check_calendar.py",
    "scripts/check_international.py",
    "scripts/diagnose_image_proxy.py",
    "nikke/store.py",
    "examples/nas-config.json",
    "deploy/rsshub/README.md",
    "deploy/rsshub/docker-compose.yml",
    "deploy/rsshub/.env.example",
    "deploy/rsshub/rsshub.env.example",
    "deploy/rsshub/routes-user/nikke-blablalink.mjs",
    "deploy/rsshub/scripts/check_blablalink.mjs",
]


def metadata_value(key):
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8-sig")
    match = re.search(rf"^{re.escape(key)}:\s*([^\r\n]+)$", text, re.MULTILINE)
    if not match:
        raise ValueError(f"metadata.yaml missing {key}")
    return match.group(1).strip().strip("\"'")


def build():
    name, version = metadata_value("name"), metadata_value("version")
    if not re.fullmatch(r"astrbot_plugin_[a-z0-9_]+", name):
        raise ValueError("Invalid plugin identifier")
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", version):
        raise ValueError("Release version must use MAJOR.MINOR.PATCH")
    missing = [relative for relative in FILES if not (ROOT / relative).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing package files: {missing}")
    # Fail when a new runtime module is absent from the allowlist.
    modules = {p.relative_to(ROOT).as_posix() for p in (ROOT / "nikke").rglob("*.py")}
    if modules - set(FILES):
        raise ValueError(f"Unlisted runtime modules: {sorted(modules - set(FILES))}")
    output = ROOT / "dist" / f"{name}-{version}.zip"
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for relative in FILES:
            info = ZipInfo(f"{name}/{relative}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (ROOT / relative).read_bytes())
    if output.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Plugin package exceeds the marketplace 16 MB limit")
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(
        f"{checksum}  {output.name}\n", encoding="utf-8"
    )
    print(f"{output} ({output.stat().st_size} bytes)")
    return output


if __name__ == "__main__":
    build()
