#!/bin/sh
# Build Valve's HLSDK studiomdl natively on macOS (arm64/x86_64).
#
# The stock source is 32-bit Windows C; four small fixes make it run:
#   1. ALIGN macro truncates 64-bit pointers to int -> use uintptr_t
#      (without this it writes through a truncated pointer and segfaults).
#   2. windows.h shim: BMP structs (packed), BYTE/WORD/DWORD, MAKEWORD,
#      stricmp family mapped to strcasecmp.
#   3. lbmlib.h's WORD/LONG typedefs clash with the shim -> guard them.
#   4. Backslash include paths -> forward slashes. NOTE: studiomdl.c uses
#      FORWARD slashes for studio.h while write.c/tristrip.c use
#      backslashes; both must resolve to the SAME studio.h or the two
#      translation units disagree on struct layouts and the binary
#      corrupts memory at write time.
#
# One feature is added on top: `$texrendermode "tex.bmp" additive|masked|
# fullbright|flatshade|chrome` (community compiler extension; sets the
# texture's STUDIO_NF_* flags). Stock HLSDK errors on the command.
#
# Usage: sh tools/build_studiomdl.sh [output-dir]   (default: tools/)
set -e
OUT="${1:-$(dirname "$0")}"
mkdir -p "$OUT"
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
# lbmlib.c includes <WINDOWS.H>; on case-sensitive filesystems it needs its
# own copy (on macOS's default case-insensitive FS the cp is a no-op).
cp "$WORK/build/shim/windows.h" "$WORK/build/shim/WINDOWS.H" 2>/dev/null || true

H="$WORK/hlsdk"
for f in studiomdl.c write.c tristrip.c bmpread.c studiomdl.h; do
  sed -e 's#\.\.\\\.\.\\engine\\studio\.h#studio.h#' \
      -e 's#\.\./\.\./engine/studio\.h#studio.h#' \
      "$H/utils/studiomdl/$f" > "$WORK/build/src/$f"
done

# $texrendermode support (additive/masked/... -> texture flags).
python3 - "$WORK/build/src/studiomdl.c" << 'PYEOF'
import sys
p = sys.argv[1]
s = open(p).read()
cmd = '''
int numtexrendermodes;
char texrendermode_name[128][64];
int texrendermode_flags[128];

void Cmd_TexRenderMode (void)
{
	char trm_texname[64];
	GetToken (false);
	strcpyn (trm_texname, token);
	GetToken (false);
	if (numtexrendermodes < 128)
	{
		strcpyn (texrendermode_name[numtexrendermodes], trm_texname);
		if (!stricmp (token, "additive"))
			texrendermode_flags[numtexrendermodes] = STUDIO_NF_ADDITIVE;
		else if (!stricmp (token, "masked"))
			texrendermode_flags[numtexrendermodes] = STUDIO_NF_MASKED;
		else if (!stricmp (token, "fullbright"))
			texrendermode_flags[numtexrendermodes] = STUDIO_NF_FULLBRIGHT;
		else if (!stricmp (token, "flatshade"))
			texrendermode_flags[numtexrendermodes] = STUDIO_NF_FLATSHADE;
		else if (!stricmp (token, "chrome"))
			texrendermode_flags[numtexrendermodes] = STUDIO_NF_FLATSHADE | STUDIO_NF_CHROME;
		else
			texrendermode_flags[numtexrendermodes] = 0;
		numtexrendermodes++;
	}
}

int lookup_texture( char *texturename )'''
assert s.count("int lookup_texture( char *texturename )") == 1
s = s.replace("int lookup_texture( char *texturename )", cmd, 1)
old = "\telse {\n\t\ttexture[i].flags = 0;\n\t}\n\tnumtextures++;"
new = ("\telse {\n\t\ttexture[i].flags = 0;\n\t}\n\t{\n\t\tint trm;\n"
       "\t\tfor (trm = 0; trm < numtexrendermodes; trm++)\n"
       "\t\t\tif (stricmp (texrendermode_name[trm], texturename) == 0)\n"
       "\t\t\t\ttexture[i].flags |= texrendermode_flags[trm];\n\t}\n"
       "\tnumtextures++;")
assert s.count(old) == 1
s = s.replace(old, new, 1)
anchor = 'else if (!strcmp (token, "$bodygroup"))'
hook = ('else if (!strcmp (token, "$texrendermode"))\n\t\t{\n'
        '\t\t\tCmd_TexRenderMode ();\n\t\t}\n\n\t\t' + anchor)
assert s.count(anchor) == 1
s = s.replace(anchor, hook, 1)
open(p, "w").write(s)
PYEOF
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
