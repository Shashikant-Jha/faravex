"""Check the faravex environment without installing anything.

    python scripts/check_deps.py requirements.txt [--torch any|cuda|cu130|cu128|cu126] [-v]

Exit code is a bit mask, so the caller installs only what is broken:
    0  everything OK
    1  a requirement is missing or at the wrong version
    2  torch / torchvision do not import together, or no CUDA when the GPU is wanted

Packages count wherever the interpreter finds them: the venv, or the system site-packages
when the venv was created with --system-site-packages.
"""

import argparse
import re
import sys
from importlib import metadata


def check_requirements(path: str) -> list[str]:
    try:
        from packaging.requirements import Requirement  # ships with transformers / huggingface-hub
    except ImportError:
        return ["missing: packaging (dependencies not installed)"]
    problems = []
    for line in open(path, encoding="utf-8"):
        line = re.sub(r"\s+#.*$", "", line).strip()
        if not line or line.startswith(("#", "-")):
            continue
        req = Requirement(line)
        try:
            have = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            problems.append(f"missing: {req}")
            continue
        if req.specifier and not req.specifier.contains(have.split("+")[0], prereleases=True):
            problems.append(f"version: {req.name} {have} does not satisfy {req.specifier}")
    return problems


def check_torch(want: str, verbose: bool) -> list[str]:
    # Import both: a torch that does not match torchvision fails here (e.g. "operator torchvision::nms does not exist").
    try:
        import torch
        import torchvision
    except Exception as e:  # noqa: BLE001 - any import failure means "reinstall torch"
        return [f"torch: {type(e).__name__}: {e}"]
    cuda = torch.cuda.is_available()
    if verbose:
        device = torch.cuda.get_device_name(0) if cuda else "cpu"
        print(f"torch {torch.__version__} + torchvision {torchvision.__version__} | CUDA {cuda} | {device}")
        print(f"torch from {torch.__file__}")
    if want == "any":
        return []
    if not cuda:
        return [f"torch: {torch.__version__} cannot use the GPU (CPU build, or driver too old for it)"]
    if want != "cuda" and not torch.__version__.endswith("+" + want):
        return [f"torch: {torch.__version__} is not the requested {want} build"]
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("requirements", nargs="?", default="requirements.txt")
    ap.add_argument("--torch", default="skip", help="any | cuda | cu130 | cu128 | cu126 | skip")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    code = 0
    for p in check_requirements(args.requirements):
        print(p)
        code |= 1
    if args.torch != "skip":
        for p in check_torch(args.torch, args.verbose):
            print(p)
            code |= 2
    return code


if __name__ == "__main__":
    sys.exit(main())
