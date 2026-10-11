#!/usr/bin/env python
"""Offline diagnostics for paper-pdf-local-ocr."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


RUNTIME_ROOT = Path(sys.executable).resolve().parent.parent


def resolve_origin(file: str | None) -> str:
    """Report whether an import came from the dedicated runtime or leaked from the base env."""
    if not file:
        return "unknown"
    try:
        Path(file).resolve().relative_to(RUNTIME_ROOT)
    except ValueError:
        return "base-env"
    return "runtime-venv"


def module_info(name: str) -> dict:
    try:
        mod = importlib.import_module(name)
        file = getattr(mod, "__file__", None)
        return {"available": True, "version": getattr(mod, "__version__", "unknown"), "file": file, "origin": resolve_origin(file)}
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def requirement_violations(package: str, extras: tuple[str, ...]) -> list[dict]:
    """Check a package's declared pins against what actually imports.

    The runtime venv is built with --system-site-packages, so optional-extra
    dependencies can silently resolve to incompatible base-env versions. pip
    check does not catch this because extras are not validated.
    """
    from packaging.requirements import Requirement
    from packaging.version import InvalidVersion, Version

    try:
        declared = importlib.metadata.requires(package) or []
    except PackageNotFoundError:
        return [{"error": f"{package} is not installed"}]
    findings: list[dict] = []
    for raw in declared:
        req = Requirement(raw)
        if req.marker is not None and not any(req.marker.evaluate({"extra": extra}) for extra in extras):
            continue
        if not req.specifier:
            continue
        try:
            installed = version(req.name)
        except PackageNotFoundError:
            findings.append({"requirement": raw, "installed": None, "status": "missing", "required_by": package})
            continue
        try:
            satisfied = Version(installed) in req.specifier
        except InvalidVersion:
            continue
        if not satisfied:
            spec = importlib.util.find_spec(req.name.replace("-", "_"))
            findings.append({
                "requirement": raw,
                "installed": installed,
                "status": "version-conflict",
                "origin": resolve_origin(getattr(spec, "origin", None)),
                "required_by": package,
            })
    return findings


def main() -> int:
    mineru = module_info("mineru")
    try:
        mineru["distribution_version"] = version("mineru")
    except PackageNotFoundError:
        mineru["distribution_version"] = None
    out = {
        "python": sys.executable,
        "version": sys.version,
        "modules": {
            "mineru": mineru,
            "fitz": module_info("fitz"),
            "PIL": module_info("PIL"),
            "torch": module_info("torch"),
            "rapidocr_onnxruntime": module_info("rapidocr_onnxruntime"),
        },
        "cuda": None,
        "mineru_models": None,
        "dependency_conflicts": requirement_violations("mineru", ("pipeline",)),
    }
    if out["modules"]["torch"]["available"]:
        import torch  # type: ignore

        out["cuda"] = {
            "available": bool(torch.cuda.is_available()),
            "device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
            "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        }
    if mineru["available"]:
        try:
            from mineru.utils.models_download_utils import get_tools_config_file_path

            config = Path(get_tools_config_file_path())
            info = {"config": str(config), "config_exists": config.exists()}
            if config.exists():
                # MinerU reads this with plain json.load, which rejects a UTF-8 BOM.
                # PowerShell's Out-File/ConvertTo-Json writes one by default.
                head = config.read_bytes()[:3]
                info["utf8_bom"] = head == b"\xef\xbb\xbf"
                if info["utf8_bom"]:
                    info["error"] = "mineru.json has a UTF-8 BOM; MinerU's json.load will fail. Rewrite it as UTF-8 without BOM."
            out["mineru_models"] = info
        except Exception as exc:
            out["mineru_models"] = {"config_error": str(exc)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    missing = [name for name in ("mineru", "fitz", "PIL") if not out["modules"][name]["available"]]
    if missing:
        return 2
    if out["dependency_conflicts"]:
        return 3
    if (out["mineru_models"] or {}).get("utf8_bom"):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
