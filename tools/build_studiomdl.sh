#!/bin/sh
# Build Valve's HLSDK studiomdl natively on macOS (arm64/x86_64).
#
# The stock source is 32-bit Windows C; four small fixes make it run:
#   1. ALIGN macro truncates 64-bit pointers to int -> use uintptr_t
#      (without this it writes through a truncated pointer and segfaults).
#   2. windows.h shim: BMP structs (packed), BYTE/WORD/DWORD, MAKEWORD,
#      stricmp family mapped to strcasecmp.
#   3. lbmlib.h's WORD/LONG typedefs clash with the shim -> guard them.
#   4. Backslash include paths -> forward slashes.
#
# Usage: sh tools/build_studiomdl.sh [output-dir]   (default: tools/)
set -e
OUT="${1:-$(dirname "$0")}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone --depth 1 --filter=blob:none --sparse https://github.com/ValveSoftware/halflife "$WORK/hlsdk"
cd "$WORK/hlsdk"
git sparse-checkout set utils/studiomdl utils/common engine dlls common public

mkdir -p "$WORK/build/shim" "$WORK/build/src"
cat > "$WORK/build/shim/windows.h" << 'EOF'
#ifndef PORT_WINDOWS_H
#define PORT_WINDOWS_H
#define _WINDOWS_
#include <stdint.h>
#include <string.h>
#include <strings.h>
typedef uint8_t  BYTE;
typedef uint16_t WORD;
typedef uint32_t DWORD;
typedef uint32_t ULONG;
typedef int32_t  LONG;
typedef int      BOOL;
typedef void*    HANDLE;
#pragma pack(push, 1)
typedef struct { WORD bfType; DWORD bfSize; WORD bfReserved1; WORD bfReserved2; DWORD bfOffBits; } BITMAPFILEHEADER;
typedef struct { DWORD biSize; LONG biWidth; LONG biHeight; WORD biPlanes; WORD biBitCount; DWORD biCompression; DWORD biSizeImage; LONG biXPelsPerMeter; LONG biYPelsPerMeter; DWORD biClrUsed; DWORD biClrImportant; } BITMAPINFOHEADER;
typedef struct { BYTE rgbBlue; BYTE rgbGreen; BYTE rgbRed; BYTE rgbReserved; } RGBQUAD;
#pragma pack(pop)
#define BI_RGB 0
#define stricmp strcasecmp
#define strnicmp strncasecmp
#ifndef MAKEWORD
#define MAKEWORD(a, b) ((WORD)(((BYTE)(a)) | (((WORD)((BYTE)(b))) << 8)))
#endif
#endif
EOF
cp "$WORK/build/shim/windows.h" "$WORK/build/shim/WINDOWS.H"

H="$WORK/hlsdk"
for f in studiomdl.c write.c tristrip.c bmpread.c studiomdl.h; do
  sed -e 's#\.\.\\\.\.\\engine\\studio\.h#studio.h#' "$H/utils/studiomdl/$f" > "$WORK/build/src/$f"
done
sed -i '' 's|#define ALIGN( a ) a = (byte \*)((int)((byte \*)a + 3) \& ~ 3)|#include <stdint.h>\n#define ALIGN( a ) a = (byte *)((uintptr_t)((byte *)a + 3) \& ~(uintptr_t)3)|' "$WORK/build/src/write.c"
sed -i '' 's/cur = (int)pData;/cur = (int)(intptr_t)pData;/g' "$WORK/build/src/write.c"
sed 's/^typedef long\t\t\tLONG;/#ifndef _WINDOWS_\ntypedef long LONG;\n#endif/' "$H/utils/common/lbmlib.h" > "$WORK/build/src/lbmlib.h"
cp "$H/utils/common/lbmlib.c" "$WORK/build/src/"
cp "$H/engine/studio.h" "$WORK/build/src/"

clang -o "$OUT/studiomdl" -O1 -fcommon -w -std=gnu89 \
  -Dstricmp=strcasecmp -Dstrnicmp=strncasecmp -Dstrcmpi=strcasecmp \
  -I"$WORK/build/shim" -I"$WORK/build/src" \
  -I"$H/utils/common" -I"$H/public" -I"$H/common" -I"$H/dlls" -I"$H/utils/studiomdl" \
  "$WORK/build/src/studiomdl.c" "$WORK/build/src/write.c" "$WORK/build/src/tristrip.c" \
  "$WORK/build/src/bmpread.c" "$WORK/build/src/lbmlib.c" \
  "$H/utils/common/cmdlib.c" "$H/utils/common/mathlib.c" \
  "$H/utils/common/scriplib.c" "$H/utils/common/trilib.c" -lm

echo "built: $OUT/studiomdl"
echo 'note: QC paths must use forward slashes on macOS:  sed -i "" "s#\\\\#/#g" model.qc'
