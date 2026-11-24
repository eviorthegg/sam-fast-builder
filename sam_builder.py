#!/usr/bin/env python3
"""
SAM Fast Builder: incremental builds for AWS SAM projects.

`sam build` is slow because it starts a container and reinstalls every
dependency, even when you only changed one line of code. This tool keeps a
fingerprint of each function's files and does the least work that is still
correct:

- dependencies changed (requirements.txt / package.json)  -> `sam build` for that resource
- only source files changed                                -> copy the changed files into .aws-sam/build
- nothing changed                                          -> skip
- template.yaml changed                                    -> one full `sam build`

Source copying runs in parallel. `sam build` invocations are serialized,
because every run rewrites the shared .aws-sam/build/template.yaml.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML is required. Install with: pip install pyyaml")
    sys.exit(1)

__version__ = "2.1.0"


# ============================================================================
# CloudFormation YAML tags
# ============================================================================
# SAM templates use tags like !Ref, !GetAtt and !Sub. PyYAML's safe loader
# rejects unknown tags, so they are loaded as plain values; this tool never
# needs to evaluate them.

class _TemplateLoader(yaml.SafeLoader):
    """Safe loader that tolerates CloudFormation intrinsic-function tags."""


def _cf_constructor(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


# A dedicated loader class, so importing this module does not change the
# behaviour of yaml.safe_load for the rest of the program.
_TemplateLoader.add_multi_constructor("!", _cf_constructor)


# ============================================================================
# Constants
# ============================================================================
CACHE_FILE = ".sam-builder-cache.json"
LOG_FILE = "sam-builder.log"
SAM_BUILD_DIR = ".aws-sam/build"
MAX_WORKERS = 8

SOURCE_EXTENSIONS = {
    ".py",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml",
    ".html", ".htm", ".css",
    ".txt", ".sql", ".csv",
    ".xml", ".md", ".graphql", ".gql",
}

# A change to any of these needs a real `sam build` (dependency install).
DEPENDENCY_FILES = ["requirements.txt", "package.json", "package-lock.json"]

# Folders that are never part of a function's deployable source.
IGNORED_DIRS = {"__pycache__", "node_modules", ".git", ".aws-sam", ".pytest_cache", ".venv", "venv"}


# ============================================================================
# Logging (thread-safe)
# ============================================================================
class Logger:
    """Thread-safe logger that writes to the console and to a log file."""

    def __init__(self, log_path: Path, verbose: bool = False):
        self.log_path = log_path
        self.verbose = verbose
        self._lock = Lock()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write(f"=== SAM Builder Log - {datetime.now().isoformat()} ===\n\n")

    def _emit(self, level: str, msg: str, console: Optional[str]) -> None:
        line = f"[{datetime.now():%H:%M:%S}] [{level}] {msg}"
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if console is not None:
                print(console)

    def info(self, msg: str):
        self._emit("INFO", msg, f"  {msg}")

    def success(self, msg: str):
        self._emit("SUCCESS", msg, f"✓ {msg}")

    def warning(self, msg: str):
        self._emit("WARN", msg, f"⚠ {msg}")

    def error(self, msg: str):
        self._emit("ERROR", msg, f"✗ {msg}")

    def debug(self, msg: str):
        self._emit("DEBUG", msg, f"  [DEBUG] {msg}" if self.verbose else None)

    def header(self, msg: str):
        self._emit("HEADER", msg, f"\n{'=' * 60}\n{msg}\n{'=' * 60}")


# ============================================================================
# Hashing and cache
# ============================================================================

def get_file_hash(filepath: Path) -> str:
    """MD5 of a file's contents (a change detector, not a security measure)."""
    if not filepath.exists():
        return ""
    hasher = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_all_source_hashes(src_dir: Path) -> Dict[str, str]:
    """Hash every tracked source file under src_dir, keyed by POSIX relative path."""
    hashes: Dict[str, str] = {}
    if not src_dir.exists():
        return hashes
    for path in src_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        rel = path.relative_to(src_dir)
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        hashes[rel.as_posix()] = get_file_hash(path)
    return hashes


def empty_cache() -> Dict:
    return {"template": "", "requirements": {}, "source_files": {}}


def load_cache(project_dir: Path) -> Dict:
    cache_path = project_dir / CACHE_FILE
    cache = empty_cache()
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return cache


def save_cache(project_dir: Path, cache: Dict) -> None:
    with open(project_dir / CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


# ============================================================================
# Template
# ============================================================================

def parse_template(template_path: Path) -> Tuple[List[Dict], List[Dict]]:
    """Return the Lambda functions and layers defined in a SAM template."""
    with open(template_path, "r", encoding="utf-8") as f:
        template = yaml.load(f, Loader=_TemplateLoader) or {}

    functions: List[Dict] = []
    layers: List[Dict] = []

    for name, resource in (template.get("Resources") or {}).items():
        res_type = resource.get("Type", "")
        props = resource.get("Properties") or {}
        metadata = resource.get("Metadata") or {}

        if res_type == "AWS::Serverless::Function":
            code_uri = props.get("CodeUri")
            if isinstance(code_uri, str):
                functions.append({
                    "name": name,
                    "code_uri": code_uri,
                    # esbuild / makefile builds transform the source, so a
                    # raw file copy would be wrong for them.
                    "custom_build": bool(metadata.get("BuildMethod")),
                })
        elif res_type == "AWS::Serverless::LayerVersion":
            content_uri = props.get("ContentUri")
            if isinstance(content_uri, str):
                layers.append({"name": name, "content_uri": content_uri})

    return functions, layers


# ============================================================================
# Building
# ============================================================================

class Builder:
    """Holds the shared state for one run: cache, locks, options."""

    def __init__(self, project_dir: Path, cache: Dict, log: Logger, *,
                 force: bool = False, sync_cache: bool = False, use_container: bool = True):
        self.project_dir = project_dir
        self.cache = cache
        self.log = log
        self.force = force
        self.sync_cache = sync_cache
        self.use_container = use_container
        self.cache_lock = Lock()
        # Every `sam build` rewrites .aws-sam/build/template.yaml, so two at
        # once can corrupt the build. Copies run in parallel; builds do not.
        self.sam_lock = Lock()

    # ---- sam CLI --------------------------------------------------------

    def run_sam_build(self, resource_name: Optional[str] = None) -> bool:
        cmd = ["sam", "build"]
        if self.use_container:
            cmd.append("--use-container")
        if resource_name:
            cmd.append(resource_name)  # `sam build [RESOURCE_LOGICAL_ID]`

        with self.sam_lock:
            self.log.info(f"Running: {' '.join(cmd)}")
            try:
                result = subprocess.run(cmd, cwd=self.project_dir, capture_output=True, text=True)
            except FileNotFoundError:
                self.log.error("sam CLI not found. Install the AWS SAM CLI.")
                return False

        if result.returncode != 0:
            self.log.error(f"sam build failed for {resource_name or 'all'}: {result.stderr[-800:]}")
            return False
        self.log.success(f"sam build completed for {resource_name or 'all'}")
        return True

    # ---- cache helpers --------------------------------------------------

    def dependencies_changed(self, code_uri: str) -> bool:
        cached = self.cache["requirements"]
        for dep_file in DEPENDENCY_FILES:
            dep_path = self.project_dir / code_uri / dep_file
            key = f"{code_uri}/{dep_file}"
            current = get_file_hash(dep_path)
            # A dependency file that was added, edited or removed all count.
            if current != cached.get(key, ""):
                self.log.debug(f"{dep_file} changed in {code_uri}")
                return True
        return False

    def record_built(self, code_uri: str) -> None:
        """Remember the current state of a resource as built."""
        src_dir = self.project_dir / code_uri
        with self.cache_lock:
            for dep_file in DEPENDENCY_FILES:
                key = f"{code_uri}/{dep_file}"
                digest = get_file_hash(src_dir / dep_file)
                if digest:
                    self.cache["requirements"][key] = digest
                else:
                    self.cache["requirements"].pop(key, None)
            self.cache["source_files"][code_uri] = get_all_source_hashes(src_dir)

    def sources_changed(self, code_uri: str) -> bool:
        current = get_all_source_hashes(self.project_dir / code_uri)
        return current != self.cache["source_files"].get(code_uri, {})

    # ---- fast path ------------------------------------------------------

    def sync_source_files(self, code_uri: str, function_name: str) -> Tuple[int, int, int]:
        """
        Make the build folder match the source folder for tracked files.
        Returns (copied, deleted, unchanged).
        """
        src_dir = self.project_dir / code_uri
        dest_dir = self.project_dir / SAM_BUILD_DIR / function_name

        current = get_all_source_hashes(src_dir)
        with self.cache_lock:
            previous = dict(self.cache["source_files"].get(code_uri, {}))

        copied = deleted = unchanged = 0
        for rel_path, digest in current.items():
            if previous.get(rel_path) == digest and (dest_dir / rel_path).exists():
                unchanged += 1
                continue
            dest_file = dest_dir / rel_path
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_dir / rel_path, dest_file)
            copied += 1
            self.log.debug(f"{function_name}: copied {rel_path}")

        # Files deleted or renamed in the source must not linger in the build.
        for rel_path in previous.keys() - current.keys():
            stale = dest_dir / rel_path
            if stale.exists():
                stale.unlink()
                deleted += 1
                self.log.debug(f"{function_name}: removed {rel_path}")

        with self.cache_lock:
            self.cache["source_files"][code_uri] = current
        return copied, deleted, unchanged

    # ---- per resource ---------------------------------------------------

    def build_function(self, func: Dict) -> Tuple[str, str, bool, int]:
        """Returns (name, action, success, files_copied_or_deleted)."""
        name, code_uri = func["name"], func["code_uri"]
        build_dir = self.project_dir / SAM_BUILD_DIR / name

        if self.sync_cache:
            self.record_built(code_uri)
            self.log.info(f"{name}: cache synced")
            return name, "sync", True, 0

        needs_sam = (
            self.force
            or not build_dir.exists()
            or self.dependencies_changed(code_uri)
            or (func["custom_build"] and self.sources_changed(code_uri))
        )

        if needs_sam:
            self.log.info(f"{name}: running sam build")
            ok = self.run_sam_build(name)
            if ok:
                self.record_built(code_uri)
            return name, "sam_build", ok, 0

        copied, deleted, unchanged = self.sync_source_files(code_uri, name)
        if copied or deleted:
            self.log.success(f"{name}: copied {copied}, removed {deleted}, unchanged {unchanged}")
            return name, "copy", True, copied + deleted
        self.log.success(f"{name}: no changes")
        return name, "skip", True, 0

    def build_layer(self, layer: Dict) -> Tuple[str, str, bool]:
        name, content_uri = layer["name"], layer["content_uri"]

        if self.sync_cache:
            self.record_built(content_uri)
            self.log.info(f"{name} (layer): cache synced")
            return name, "sync", True

        build_dir = self.project_dir / SAM_BUILD_DIR / name
        # Layer builds use a different folder layout (e.g. python/), so a
        # source change is rebuilt rather than copied.
        needs_sam = (
            self.force
            or not build_dir.exists()
            or self.dependencies_changed(content_uri)
            or self.sources_changed(content_uri)
        )
        if not needs_sam:
            self.log.success(f"{name} (layer): no changes")
            return name, "skip", True

        self.log.info(f"{name} (layer): running sam build")
        ok = self.run_sam_build(name)
        if ok:
            self.record_built(content_uri)
        return name, "sam_build", ok


# ============================================================================
# Entry point
# ============================================================================

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Incremental AWS SAM builds: copy changed code, rebuild only when dependencies change.")
    parser.add_argument("-f", "--force", action="store_true", help="run sam build for every resource")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose debug output")
    parser.add_argument("--template", default="template.yaml", help="SAM template (default: template.yaml)")
    parser.add_argument("--clear-cache", action="store_true", help="forget all hashes and rebuild")
    parser.add_argument("-j", "--jobs", type=int, default=MAX_WORKERS,
                        help=f"parallel workers for copying (default: {MAX_WORKERS})")
    parser.add_argument("--sync-cache", action="store_true",
                        help="record the current state as built without building (for an already-built project)")
    parser.add_argument("--no-container", action="store_true",
                        help="run sam build without --use-container")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    project_dir = Path.cwd()
    template_path = project_dir / args.template

    log = Logger(project_dir / LOG_FILE, verbose=args.verbose)
    log.header(f"SAM Fast Builder {__version__}")
    log.info(f"Project: {project_dir}")

    if not template_path.exists():
        log.error(f"Template not found: {template_path}")
        return 1

    cache = empty_cache() if args.clear_cache else load_cache(project_dir)
    if args.clear_cache:
        log.warning("Cache cleared: everything will be rebuilt")

    try:
        functions, layers = parse_template(template_path)
    except Exception as exc:  # noqa: BLE001 - report any parse failure cleanly
        log.error(f"Failed to parse template: {exc}")
        return 1
    log.info(f"Found {len(functions)} functions, {len(layers)} layers")

    builder = Builder(project_dir, cache, log, force=args.force,
                      sync_cache=args.sync_cache, use_container=not args.no_container)
    start = time.time()
    stats: Dict[str, int] = {"sam_build": 0, "copy": 0, "skip": 0, "sync": 0, "failed": 0, "files": 0}

    template_hash = get_file_hash(template_path)
    template_changed = template_hash != cache.get("template")

    if template_changed and not args.sync_cache:
        # Handlers, environment, new or removed resources: none of that lives in
        # the copied source, so the whole build is regenerated once.
        log.header("template.yaml changed: full sam build")
        if not builder.run_sam_build():
            stats["failed"] += 1
        else:
            for item in functions:
                builder.record_built(item["code_uri"])
            for item in layers:
                builder.record_built(item["content_uri"])
            stats["sam_build"] += 1
            cache["template"] = template_hash
    else:
        if args.sync_cache:
            cache["template"] = template_hash

        if layers:
            log.header("Layers")
            for layer in layers:
                _, action, ok = builder.build_layer(layer)
                stats[action if ok else "failed"] += 1

        log.header(f"Functions ({args.jobs} workers)")
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = [pool.submit(builder.build_function, func) for func in functions]
            for future in as_completed(futures):
                _, action, ok, files = future.result()
                stats[action if ok else "failed"] += 1
                stats["files"] += files

    save_cache(project_dir, cache)

    log.header("Summary")
    log.info(f"Time:          {time.time() - start:.2f}s")
    log.info(f"sam build:     {stats['sam_build']}")
    log.info(f"Fast copy:     {stats['copy']} ({stats['files']} files)")
    log.info(f"No changes:    {stats['skip']}")
    if args.sync_cache:
        log.info(f"Synced:        {stats['sync']}")
    if stats["failed"]:
        log.error(f"Failed:        {stats['failed']}")
        return 1
    log.success("Build is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
