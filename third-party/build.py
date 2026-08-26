#!/usr/bin/env python3
"""Builds zlib, libexpat and exiv2 from the vendored sources in this directory.

Everything is installed into third-party/prefix, using @rpath-based install
names and relative rpaths so the whole tree is relocatable.

Environment overrides:
  CMAKE   path to the cmake executable (default: cmake from PATH,
          falls back to ../.venv-3.13/bin/cmake)
  JOBS    parallel build jobs (default: number of CPUs)
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TP = Path(__file__).resolve().parent
PREFIX = TP / "prefix"


def find_cmake() -> str:
    c = os.environ.get("CMAKE")
    if c:
        return c
    if shutil.which("cmake"):
        return "cmake"
    venv_cmake = TP.parent / ".venv-3.13" / "bin" / "cmake"
    if venv_cmake.exists() and os.access(venv_cmake, os.X_OK):
        return str(venv_cmake)
    print("error: cmake not found (set CMAKE=/path/to/cmake)", file=sys.stderr)
    sys.exit(1)


CMAKE = find_cmake()

JOBS = int(os.environ.get("JOBS") or subprocess.check_output(
    ["sysctl", "-n", "hw.ncpu"]).strip().splitlines()[0])


def build(src: Path, builddir: Path, args: list[str]) -> None:
    print(f"=== configuring: {src.name} ===")
    subprocess.run([CMAKE, "-S", str(src), "-B", str(builddir)] + args, check=True)
    print(f"=== building: {builddir.name} ===")
    subprocess.run([CMAKE, "--build", str(builddir), f"-j{JOBS}"], check=True)


def strip_buggy_fixups(builddir: Path) -> None:
    # CMake 4.x emits a malformed install_name_tool -id/-change for @rpath
    # install names (e.g. -id ""@rpath"/libfoo.dylib"), which makes
    # install_name_tool abort. The build tree already carries the correct
    # @rpath install names, so drop these redundant blocks; keep the
    # -delete_rpath/-add_rpath blocks.
    for script in sorted(builddir.rglob("cmake_install.cmake")):
        lines = script.read_text().splitlines(keepends=True)
        out: list[str] = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            if "install_name_tool" in line and "execute_process" in line:
                block = [line]
                j = i + 1
                if not line.rstrip().endswith(")"):
                    while j < n and not lines[j].rstrip().endswith(")"):
                        block.append(lines[j]); j += 1
                    if j < n:
                        block.append(lines[j]); j += 1
                blob = "".join(block)
                if " -id " not in blob and " -change " not in blob:
                    out.extend(block)
                i = j
            else:
                out.append(line)
                i += 1
        script.write_text("".join(out))


def install(builddir: Path) -> None:
    print(f"=== installing: {builddir.name} ===")
    strip_buggy_fixups(builddir)
    subprocess.run([CMAKE, "--install", str(builddir)], check=True)


def rpaths_of(path: Path) -> list[str]:
    out = subprocess.run(
        ["otool", "-l", str(path)], capture_output=True, text=True, check=True).stdout
    rpaths = []
    for line in out.splitlines():
        m = re.match(r"\s+path (\S+)", line)
        if m:
            rpaths.append(m.group(1))
    return rpaths


def normalize_rpaths(file: Path, want: str) -> None:
    # Drop absolute rpaths and make sure the given relative rpath is present.
    rpaths = rpaths_of(file)
    for rpath in rpaths:
        if not rpath.startswith("@"):
            subprocess.run(
                ["install_name_tool", "-delete_rpath", rpath, str(file)], check=True)
    if want not in rpaths_of(file):
        subprocess.run(
            ["install_name_tool", "-add_rpath", want, str(file)], check=True)


# Fresh install tree on every run (build dirs are kept for incremental builds).
if PREFIX.exists():
    shutil.rmtree(PREFIX)
PREFIX.mkdir(parents=True)

# --- zlib (shared) ---
build(TP / "zlib", TP / "zlib" / "build", [
    "-DCMAKE_BUILD_TYPE=Release",
    f"-DCMAKE_INSTALL_PREFIX={PREFIX}",
    '-DCMAKE_INSTALL_NAME_DIR="@rpath"',
    "-DCMAKE_INSTALL_RPATH=",
    "-DZLIB_BUILD_SHARED=ON",
    "-DZLIB_BUILD_STATIC=OFF",
    "-DZLIB_BUILD_TESTING=OFF",
])
install(TP / "zlib" / "build")

# --- libexpat (shared) ---
build(TP / "libexpat" / "expat", TP / "libexpat" / "build", [
    "-DCMAKE_BUILD_TYPE=Release",
    f"-DCMAKE_INSTALL_PREFIX={PREFIX}",
    '-DCMAKE_INSTALL_NAME_DIR="@rpath"',
    "-DCMAKE_INSTALL_RPATH=",
    "-DBUILD_SHARED_LIBS=ON",
    "-DEXPAT_BUILD_TESTS=OFF",
    "-DEXPAT_BUILD_TOOLS=OFF",
    "-DEXPAT_BUILD_EXAMPLES=OFF",
])
install(TP / "libexpat" / "build")

# --- exiv2 (finds zlib/expat in prefix) ---
build(TP / "exiv2", TP / "exiv2" / "build", [
    "-DCMAKE_BUILD_TYPE=Release",
    f"-DCMAKE_PREFIX_PATH={PREFIX}",
    f"-DCMAKE_INSTALL_PREFIX={PREFIX}",
    '-DCMAKE_INSTALL_NAME_DIR="@rpath"',
    "-DCMAKE_INSTALL_RPATH=",
    "-DEXIV2_ENABLE_INIH=OFF",
    "-DEXIV2_ENABLE_BROTLI=OFF",
])
install(TP / "exiv2" / "build")

# CMake bakes an absolute rpath into the CLI (for the build-tree libexiv2);
# replace it with a relative one so the tree stays relocatable.
normalize_rpaths(PREFIX / "bin" / "exiv2", "@loader_path/../lib")
# libexiv2 finds libz/libexpat in its own directory.
normalize_rpaths(PREFIX / "lib" / "libexiv2.dylib", "@loader_path")

# --- sanity check ---
print("=== sanity check ===")
version = subprocess.run(
    [str(PREFIX / "bin" / "exiv2"), "--version"], capture_output=True, text=True, check=True)
print(version.stdout.splitlines()[0])
print("done.")
