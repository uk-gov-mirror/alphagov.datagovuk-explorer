#!/usr/bin/env python3
"""Download the bge-base-en-v1.5 embedding model (GGUF q8_0) into llm/.

The model is a community GGUF conversion of BAAI/bge-base-en-v1.5 hosted at
https://huggingface.co/CompendiumLabs/bge-base-en-v1.5-gguf — the file the
embedding pipeline expects (see scripts/embeddings.py, LLAMA_SERVER). The
llm/ directory is gitignored working data, so it's not shipped with the repo.

Skips the download if the file is already there (pass --force to re-fetch).

Usage: python scripts/download_llm.py [--force]
"""

import hashlib
import pathlib
import sys

import httpx
import typer

REPO = "CompendiumLabs/bge-base-en-v1.5-gguf"
FILENAME = "bge-base-en-v1.5-q8_0.gguf"
URL = f"https://huggingface.co/{REPO}/resolve/main/{FILENAME}"

# sha256 of the q8_0 GGUF as published at the URL above. If upstream
# re-uploads the file this will differ — the error message says so.
SHA256 = "ad1afe72cd6654a558667a3db10878b049a75bfd72912e1dabb91310d671173c"

DEST = pathlib.Path(__file__).resolve().parent.parent / "llm" / FILENAME

app = typer.Typer(add_completion=False)


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with httpx.stream("GET", URL, follow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    print(f"\rDownloading {FILENAME}: {pct:3d}%", end="", flush=True)
                else:
                    print(
                        f"\rDownloading {FILENAME}: {done / 1e6:.0f} MB",
                        end="",
                        flush=True,
                    )
    print()
    tmp.replace(dest)


@app.command()
def main(
    *,
    force: bool = typer.Option(
        False,  # noqa: FBT003 — typer.Option's default is the first positional
        "--force",
        help="Re-download even if the model file already exists.",
    ),
) -> None:
    """Download the bge-base-en-v1.5-q8_0.gguf model into llm/."""

    if DEST.exists() and not force:
        print(f"{DEST} already exists — nothing to do. (Pass --force to re-download.)")
        return

    try:
        _download(DEST)
    except httpx.HTTPError as e:
        print(f"Error downloading model: {e}", file=sys.stderr)
        raise typer.Exit(1) from None

    actual = _sha256(DEST)
    if actual != SHA256:
        print(
            f"sha256 mismatch — expected {SHA256}, got {actual}.\n"
            "The file upstream has probably changed. Delete it and re-run, or "
            "update SHA256 in this script if the change is expected.",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    print(f"OK — {DEST} ({DEST.stat().st_size / 1e6:.0f} MB, sha256 {actual[:12]}…)")


if __name__ == "__main__":
    app()
