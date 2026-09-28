"""Automated package and dependency auto-installer for self-healing runs.

When a curation recipe or validation script fails due to a missing Python package,
Node module, or system command, this module detects the missing requirement from
the error trace and installs it on the fly, enabling the pipeline to resume without
human intervention.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

__all__ = [
    "auto_install_from_error",
    "detect_missing_python_packages",
    "detect_missing_node_packages",
    "install_python_packages",
    "install_node_packages",
]

#: Maps common Python import names to their actual PyPI distribution names.
PYTHON_PACKAGE_MAP: Dict[str, str] = {
    "yaml": "pyyaml",
    "dateutil": "python-dateutil",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "google.genai": "google-genai",
    "typesafe": "typesafe-sdk",
    "pydantic_ai": "pydantic-ai",
    "requests": "requests",
    "jsonschema": "jsonschema",
}

#: Regex patterns for missing Python dependencies.
PYTHON_IMPORT_ERRORS = [
    re.compile(r"ModuleNotFoundError:\s+No module named ['\"]?([a-zA-Z0-9_\.\-]+)['\"]?", re.IGNORECASE),
    re.compile(r"ImportError:\s+cannot import name .* from ['\"]?([a-zA-Z0-9_\.\-]+)['\"]?", re.IGNORECASE),
    re.compile(r"No module named ['\"]?([a-zA-Z0-9_\.\-]+)['\"]?", re.IGNORECASE),
]

#: Regex patterns for missing Node.js packages.
NODE_MODULE_ERRORS = [
    re.compile(r"Cannot find module ['\"]?([@a-zA-Z0-9_\.\-/]+)['\"]?", re.IGNORECASE),
    re.compile(r"Cannot find package ['\"]?([@a-zA-Z0-9_\.\-/]+)['\"]?", re.IGNORECASE),
    re.compile(r"MODULE_NOT_FOUND", re.IGNORECASE),
]


def detect_missing_python_packages(error_text: str) -> List[str]:
    """Extract missing Python package names from an error trace."""
    found: List[str] = []
    seen: set = set()
    for pattern in PYTHON_IMPORT_ERRORS:
        for match in pattern.finditer(error_text):
            raw_mod = match.group(1).split(".")[0]
            pkg = PYTHON_PACKAGE_MAP.get(raw_mod, raw_mod)
            if pkg and pkg not in seen:
                seen.add(pkg)
                found.append(pkg)
    return found


def detect_missing_node_packages(error_text: str) -> List[str]:
    """Extract missing Node package names from an error trace."""
    found: List[str] = []
    seen: set = set()
    for pattern in NODE_MODULE_ERRORS:
        for match in pattern.finditer(error_text):
            if match.groups():
                pkg = match.group(1)
                # Ignore relative module paths like './foo' or '../bar'
                if pkg.startswith("."):
                    continue
                if pkg not in seen:
                    seen.add(pkg)
                    found.append(pkg)
    return found


def install_python_packages(packages: List[str], log_fn: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """Install Python packages via pip in the current runtime environment."""
    if not packages:
        return False, "no packages specified"
    msg = f"Auto-installing Python packages: {', '.join(packages)}"
    if log_fn:
        log_fn(msg)
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"] + packages
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        if res.returncode == 0:
            success_msg = f"Successfully installed Python packages: {', '.join(packages)}"
            if log_fn:
                log_fn(success_msg)
            return True, success_msg
        err = res.stderr.decode("utf-8", "replace").strip() or res.stdout.decode("utf-8", "replace").strip()
        fail_msg = f"Failed installing Python packages ({res.returncode}): {err[:200]}"
        if log_fn:
            log_fn(fail_msg)
        return False, fail_msg
    except Exception as exc:
        err_msg = f"Exception installing Python packages: {exc}"
        if log_fn:
            log_fn(err_msg)
        return False, err_msg


def install_node_packages(
    packages: List[str], cwd: Path, log_fn: Optional[Callable[[str], None]] = None
) -> Tuple[bool, str]:
    """Install Node packages via npm in the specified workspace directory."""
    if not packages:
        return False, "no node packages specified"
    msg = f"Auto-installing Node packages in {cwd}: {', '.join(packages)}"
    if log_fn:
        log_fn(msg)
    cmd = ["npm", "install", "--no-audit", "--no-fund"] + packages
    try:
        res = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        if res.returncode == 0:
            success_msg = f"Successfully installed Node packages: {', '.join(packages)}"
            if log_fn:
                log_fn(success_msg)
            return True, success_msg
        err = res.stderr.decode("utf-8", "replace").strip() or res.stdout.decode("utf-8", "replace").strip()
        fail_msg = f"Failed installing Node packages ({res.returncode}): {err[:200]}"
        if log_fn:
            log_fn(fail_msg)
        return False, fail_msg
    except Exception as exc:
        err_msg = f"Exception installing Node packages: {exc}"
        if log_fn:
            log_fn(err_msg)
        return False, err_msg


def auto_install_from_error(
    error_text: str,
    cwd: Optional[Path] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    """Inspect error output, detect missing Python or Node packages, and install them."""
    installed_any = False
    details: List[str] = []

    py_pkgs = detect_missing_python_packages(error_text)
    if py_pkgs:
        ok, msg = install_python_packages(py_pkgs, log_fn=log_fn)
        if ok:
            installed_any = True
            details.append(msg)

    node_pkgs = detect_missing_node_packages(error_text)
    if node_pkgs and cwd and Path(cwd).is_dir():
        ok, msg = install_node_packages(node_pkgs, Path(cwd), log_fn=log_fn)
        if ok:
            installed_any = True
            details.append(msg)

    return installed_any, "; ".join(details) or "no missing packages detected"
