"""Stage installed engines as local packaging candidates with file hashes."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare(gs: Path, tess: Path, output: Path) -> None:
    required = [gs / "bin/gswin64c.exe", gs / "bin/gsdll64.dll", tess / "tesseract.exe"]
    required += [tess / f"tessdata/{name}.traineddata" for name in ("kor", "eng", "osd")]
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(path)
    output.mkdir(parents=True, exist_ok=False)
    for name in ("bin", "Resource", "lib", "iccprofiles", "doc"):
        shutil.copytree(gs / name, output / "ghostscript" / name)
    target = output / "tesseract"
    target.mkdir()
    if (tess / "doc").is_dir():
        shutil.copytree(tess / "doc", target / "doc")
    shutil.copy2(tess / "tesseract.exe", target)
    for path in tess.glob("*.dll"):
        shutil.copy2(path, target)
    data = target / "tessdata"
    data.mkdir()
    for name in ("kor.traineddata", "eng.traineddata", "osd.traineddata", "pdf.ttf"):
        shutil.copy2(tess / "tessdata" / name, data)
    for name in ("configs", "tessconfigs"):
        if (tess / "tessdata" / name).is_dir():
            shutil.copytree(tess / "tessdata" / name, data / name)
    inventory = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in output.rglob("*") if p.is_file()}
    (output / "inventory.json").write_text(json.dumps({"status":"LOCAL_CANDIDATE_LICENSE_REVIEW_PENDING", "files":inventory},indent=2),encoding="utf-8")
    print(f"Staged {len(inventory)} files; {sum(p.stat().st_size for p in output.rglob('*') if p.is_file())} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ghostscript", type=Path, required=True)
    parser.add_argument("--tesseract", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "vendor/store-engines")
    args = parser.parse_args()
    prepare(args.ghostscript, args.tesseract, args.output)
