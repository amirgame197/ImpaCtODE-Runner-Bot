from __future__ import annotations
from urllib.request import urlopen

from pathlib import Path
import tempfile
import tarfile
import config
import os

_DOWNLOAD_CHUNK_SIZE = 1024 * 1024

def _format_size(byte_count: int) -> str:
    """Format a byte count for compact terminal progress messages.
    """
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if byte_count < 1024 or unit == "TiB":
            return f"{byte_count:.1f} {unit}" if unit != "B" else f"{byte_count} {unit}"
        byte_count /= 1024
    raise AssertionError("unreachable")


def _show_progress(label: str, completed: int, total: int | None) -> None:
    """Update one terminal line with byte and, when known, percent progress.
    """
    if total:
        percent = min(completed / total * 100, 100)
        message = (
            f"\r{label}: {_format_size(completed)} / {_format_size(total)} "
            f"({percent:5.1f}%)"
        )
    else:
        message = f"\r{label}: {_format_size(completed)}"
    print(message, end="", flush=True)


def _download_and_extract(url: str, destination: Path, required_image: Path) -> None:
    """Download a tar archive and safely extract its regular files to destination.
    """
    destination.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_archive_name = tempfile.mkstemp(
        prefix="impactode-image-", suffix=".tar.xz", dir=destination
    )
    temporary_archive = Path(temporary_archive_name)
    os.close(file_descriptor)

    try:
        print(f"Downloading {required_image.name}...", flush=True)
        downloaded_bytes = 0
        with urlopen(url) as response, temporary_archive.open("wb") as archive_file:
            content_length = response.headers.get("Content-Length")
            total_download_bytes = int(content_length) if content_length else None
            while chunk := response.read(_DOWNLOAD_CHUNK_SIZE):
                archive_file.write(chunk)
                downloaded_bytes += len(chunk)
                _show_progress("Download", downloaded_bytes, total_download_bytes)
        print()

        destination_root = destination.resolve()
        with tarfile.open(temporary_archive, "r:*") as archive:
            regular_files = []
            for member in archive.getmembers():
                member_path = (destination / member.name).resolve()
                if not member_path.is_relative_to(destination_root):
                    raise RuntimeError(f"Archive contains an unsafe path: {member.name}")
                if not (member.isfile() or member.isdir()):
                    raise RuntimeError(f"Archive contains an unsupported entry: {member.name}")
                if member.isfile():
                    regular_files.append(member)

            total_extract_bytes = sum(member.size for member in regular_files)
            extracted_bytes = 0
            print(f"Extracting {required_image.name}...", flush=True)
            for member in archive.getmembers():
                target = destination / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"Could not extract archive entry: {member.name}")
                with source, target.open("wb") as image_file:
                    while chunk := source.read(_DOWNLOAD_CHUNK_SIZE):
                        image_file.write(chunk)
                        image_file.flush()
                        extracted_bytes += len(chunk)
                        _show_progress("Extract", extracted_bytes, total_extract_bytes)
                target.chmod(member.mode)
            print()

        if not required_image.is_file():
            raise RuntimeError(
                f"Downloaded archive did not contain the expected image: {required_image}"
            )
        print(f"Installed {required_image.name}.", flush=True)
    finally:
        temporary_archive.unlink(missing_ok=True)


def _prompt_for_download(language: str) -> str:
    """Return the user's y/n/a/c decision for a missing language image.
    """
    while True:
        decision = input(
            f"{language} support is not installed. Download it? "
            "[y] Yes, [n] No, [a] All, [c] Cancel: "
        ).strip().lower()
        if decision in {"y", "n", "a", "c"}:
            return decision
        print("Please enter y, n, a, or c.", flush=True)


def ensure_language_support_images() -> None:
    """Ensure the base image and selected language overlays are available locally.
    """
    overlays_dir = Path(config.overlays_dir)
    base_image = overlays_dir.parent / "base.qcow2"

    if not base_image.is_file():
        _download_and_extract(config.base_image_url, overlays_dir.parent, base_image)

    download_all = False
    for language, steps in config.languages_sequence.items():
        language_config = next(
            (step for step in steps if "overlay_path" in step and "image_url" in step),
            None,
        )
        if language_config is None:
            continue

        overlay_path = Path(language_config["overlay_path"])
        if overlay_path.is_file():
            continue

        decision = "a" if download_all else _prompt_for_download(language)
        if decision == "c":
            # raise SystemExit("Language support setup cancelled.")
            break
        if decision == "n":
            continue
        if decision == "a":
            download_all = True

        _download_and_extract(language_config["image_url"], overlays_dir, overlay_path)


if __name__ == "__main__":
    ensure_language_support_images()
