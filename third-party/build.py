#!/usr/bin/env python3
"""Builds zlib, libexpat and exiv2 from the vendored sources in this directory.

Everything is installed into third-party/prefix, using @rpath-based install
names and relative rpaths so the whole tree is relocatable.

Then compiles pyexiv2's C++ binding against that prefix and pip-installs
pyexiv2 into the venv running this script.

Usage:
  build.py         configure, build and install everything into prefix,
                   then build and install pyexiv2 into the current venv
  build.py clean   remove the build directories, the prefix tree and the
                   staged pyexiv2 build artifacts

Environment overrides:
  CMAKE   path to the cmake executable (default: cmake from PATH,
          e.g. the one in your activated venv)
  JOBS    parallel build jobs (default: number of CPUs)

Note: pybind11 (headers only, needed to compile the pyexiv2 binding) is
installed into the running venv if it is not already present.
"""

import importlib.util
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
    print("error: cmake not found (activate your venv or set CMAKE=/path/to/cmake)", file=sys.stderr)
    sys.exit(1)


CMAKE: str
JOBS: int


PYEXIV2_SRC = TP / "pyexiv2"
PYEXIV2_LIB = PYEXIV2_SRC / "pyexiv2" / "lib"
# Our libexiv2's runtime dependencies, copied next to it so its @loader_path
# rpath resolves them.
PYEXIV2_RUNTIME_LIBS = ("libexiv2.dylib", "libexpat.1.dylib", "libz.1.dylib")
# Artifacts staged into the pyexiv2 source tree (it is a submodule).
PYEXIV2_ARTIFACTS = ("exiv2api.so",) + PYEXIV2_RUNTIME_LIBS


def clean() -> None:
    for d in (PREFIX, TP / "zlib" / "build", TP / "libexpat" / "build", TP / "exiv2" / "build"):
        if d.exists():
            shutil.rmtree(d)
            print(f"removed {d.relative_to(TP)}")
    for f in PYEXIV2_ARTIFACTS:
        p = PYEXIV2_LIB / f
        if p.exists():
            p.unlink()
            print(f"removed {p.relative_to(TP)}")
    print("done.")


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


def build_pyexiv2() -> None:
    # Installs pyexiv2 into the venv running this script, following pyexiv2's
    # own build procedure (see pyexiv2/lib/README.md and
    # .github/workflows/build.yml): compile the C++ binding next to a prebuilt
    # exiv2 runtime library, drop both into pyexiv2/lib/, and let setup.py
    # package that prebuilt artifact verbatim (it skips compilation when
    # exiv2api.so is already present). Unlike the upstream exiv2 release,
    # which links the system libexpat/libz, our libexiv2 references them via
    # @rpath + its @loader_path rpath, so they must sit next to it as well.
    # At import time pyexiv2/lib/__init__.py dlopens libexiv2.dylib from its
    # own directory before importing exiv2api, whose (rpath-less) libexiv2
    # dependency then resolves to that already-loaded image.
    if importlib.util.find_spec("pybind11") is None:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "pybind11"], check=True)
    pybind11_includes = subprocess.check_output(
        [sys.executable, "-m", "pybind11", "--includes"], text=True).split()

    print("=== building: pyexiv2 binding ===")
    subprocess.run(
        ["c++", "exiv2api.cpp", "-o", "exiv2api.so", "-O3", "-Wall", "-std=c++11",
         "-shared", "-fPIC"] + pybind11_includes +
        ["-I", str(PREFIX / "include"), "-L", str(PREFIX / "lib"),
         "-lexiv2", "-undefined", "dynamic_lookup"],
        cwd=PYEXIV2_LIB, check=True)

    print("=== staging: exiv2 runtime libraries ===")
    for name in PYEXIV2_RUNTIME_LIBS:
        shutil.copy2((PREFIX / "lib" / name).resolve(), PYEXIV2_LIB / name)

    print("=== installing: pyexiv2 ===")
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", str(PYEXIV2_SRC)], check=True)

    print("=== sanity check: pyexiv2 ===")
    out = subprocess.run(
        [sys.executable, "-c", "import pyexiv2; print(pyexiv2.__exiv2_version__)"],
        capture_output=True, text=True, check=True).stdout
    print(out.strip())


def main() -> None:
    global CMAKE, JOBS
    CMAKE = find_cmake()
    JOBS = int(os.environ.get("JOBS") or subprocess.check_output(
        ["sysctl", "-n", "hw.ncpu"]).strip().splitlines()[0])

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

    # --- pyexiv2 (into the venv running this script) ---
    build_pyexiv2()
    print("done.")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "clean":
        clean()
    elif len(sys.argv) == 1:
        main()
    else:
        print("usage: build.py [clean]", file=sys.stderr)
        sys.exit(2)
