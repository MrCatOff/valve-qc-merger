#!/bin/sh
# Build a GoldSrc model decompiler natively on macOS (arm64/x86_64).
#
# This is the counterpart to build_studiomdl.sh: studiomdl compiles a QC + SMDs
# into a .mdl; this decompiles a .mdl back into a QC, reference SMDs, animation
# SMDs and BMP textures. The source is Toodles2You/halflife-tools ("DecompMDL"),
# a clean C rewrite that already targets Linux, so it needs almost no porting:
#
#   1. The .c files rely on a precompiled header (src/pch.h) for vec3_t and the
#      libc includes; CMake feeds it as a PCH. A direct clang build must force it
#      in with -include src/pch.h (otherwise: "unknown type name 'vec3_t'").
#   2. makepath() creates each path component left to right, but on an ABSOLUTE
#      path the leading "/" makes it call mkdir("") -> ENOENT -> "Failed to make
#      directory". Guard the empty root component (c != path) so absolute output
#      paths work. (Relative paths were unaffected, which is why upstream/Linux
#      never hit it.)
#
# macOS defines __GNUC__ under clang, so the POSIX mkdir()/strcasecmp()/strdup()
# branches are taken as-is; only -lm is needed to link math.c.
#
# It also handles .spr / .wad / .bsp; for our use the interesting input is .mdl.
#
# Usage: sh tools/build_mdldec.sh [output-dir]   (default: tools/)
#   then: tools/decompmdl <model.mdl> <output-dir>
set -e
OUT="${1:-$(dirname "$0")}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"  # absolute: we cd into the build dir before linking
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone --depth 1 https://github.com/Toodles2You/halflife-tools "$WORK/src"
cd "$WORK/src"

# Fix makepath() for absolute output paths: skip the empty root before a leading
# slash. Matches regardless of the exact indentation upstream uses.
sed -i.bak 's/if (!makedir (path))/if (c != path \&\& !makedir (path))/' src/common.c
grep -q 'c != path && !makedir (path)' src/common.c \
  || { echo "patch failed: makepath guard not applied" >&2; exit 1; }

clang -o "$OUT/decompmdl" -O1 -w -include src/pch.h \
  src/common.c src/math.c src/decompile.c src/studio.c src/model.c \
  src/texture.c src/animation.c src/sprite.c src/wad.c src/info.c -lm

echo "built: $OUT/decompmdl"
echo 'decompile:  '"$OUT"'/decompmdl model.mdl out_dir/   (writes out_dir/<model>/{model.qc, *.smd, anims/, maps_8bit/})'
