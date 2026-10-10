"""Build Valve's HLSDK studiomdl with raised limits, for macOS, Linux and
Windows (cross-compiled with mingw-w64).

    python tools/build_studiomdl.py [--out DIR] [--target native|windows]
                                    [--cc COMPILER] [--max-models N]

Patches on top of the HLSDK source (utils/studiomdl):

- 64-bit fixes: the ALIGN macro and pointer casts truncated pointers to int;
- ``$texrendermode "tex.bmp" additive|masked|fullbright|flatshade|chrome``
  (community extension; sets the texture's STUDIO_NF_* flags);
- MAXSTUDIOMODELS 32 -> N (default 1024): submodels per model, blanks
  included. Stock studiomdl keeps ONE global array of 32 for the whole model
  and writes past it silently (meshes detach from their bones in game); the
  engine and ReHLDS read the count from the file and have no such cap. A model
  over the new limit now stops with an error instead;
- a model over 16 MB (the .mdl, its T.mdl or a sequence group) stops with an
  error: the output buffer overflowed silently before, and bigger files risk
  the client (pack textures into atlases instead). The buffer itself is 64 MB
  so nothing is overwritten before the check;
- ``studiomdl`` without arguments prints ``valve-qc-merger studiomdl:
  MAXSTUDIOMODELS N`` before the usage, so the tools know the limit.

macOS/Linux: a small windows.h shim. Windows: mingw's own headers.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HLSDK = "https://github.com/ValveSoftware/halflife"
SHIM = r"""#ifndef PORT_WINDOWS_H
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
"""

TEXRENDERMODE = '''
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


def _replace(text: str, old: str, new: str, path: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"{path}: patch anchor not found exactly once: {old[:60]!r}")
    return text.replace(old, new, 1)


def patch_sources(hlsdk: Path, src: Path, max_models: int) -> None:
    """Copy the studiomdl sources into ``src`` with every patch applied."""
    src.mkdir(parents=True, exist_ok=True)
    for name in ("studiomdl.c", "write.c", "tristrip.c", "bmpread.c", "studiomdl.h"):
        text = (hlsdk / "utils" / "studiomdl" / name).read_text(encoding="latin-1")
        # one studio.h for every translation unit (forward and back slashes)
        text = text.replace("..\\..\\engine\\studio.h", "studio.h")
        text = text.replace("../../engine/studio.h", "studio.h")
        if name == "studiomdl.c":
            text = _replace(text, "int lookup_texture( char *texturename )", TEXRENDERMODE,
                            name)
            text = _replace(
                text, "\telse {\n\t\ttexture[i].flags = 0;\n\t}\n\tnumtextures++;",
                "\telse {\n\t\ttexture[i].flags = 0;\n\t}\n\t{\n\t\tint trm;\n"
                "\t\tfor (trm = 0; trm < numtexrendermodes; trm++)\n"
                "\t\t\tif (stricmp (texrendermode_name[trm], texturename) == 0)\n"
                "\t\t\t\ttexture[i].flags |= texrendermode_flags[trm];\n\t}\n"
                "\tnumtextures++;", name)
            anchor = 'else if (!strcmp (token, "$bodygroup"))'
            text = _replace(text, anchor, 'else if (!strcmp (token, "$texrendermode"))\n'
                            "\t\t{\n\t\t\tCmd_TexRenderMode ();\n\t\t}\n\n\t\t" + anchor,
                            name)
            guard = ("\tif (nummodels >= MAXSTUDIOMODELS || bodypart[numbodyparts].nummodels"
                     " >= MAXSTUDIOMODELS)\n\t\tError( \"too many submodels: this studiomdl "
                     "keeps %d (MAXSTUDIOMODELS)\\n\", MAXSTUDIOMODELS );\n")
            text = _replace(text, "\tif (!GetToken (false)) return;\n\n"
                            "\tmodel[nummodels] = kalloc( 1, sizeof( s_model_t ) );",
                            "\tif (!GetToken (false)) return;\n\n" + guard
                            + "\tmodel[nummodels] = kalloc( 1, sizeof( s_model_t ) );", name)
            text = _replace(text, "int Option_Blank( )\n{\n"
                            "\tmodel[nummodels] = kalloc( 1, sizeof( s_model_t ) );",
                            "int Option_Blank( )\n{\n" + guard
                            + "\tmodel[nummodels] = kalloc( 1, sizeof( s_model_t ) );", name)
            text = _replace(text, "\tif (argc == 1)\n\t\tError (\"usage:",
                            "\tprintf (\"valve-qc-merger studiomdl: MAXSTUDIOMODELS %d\\n\", "
                            "MAXSTUDIOMODELS);\n\tif (argc == 1)\n\t\tError (\"usage:", name)
        if name == "write.c":
            text = _replace(text, "#define ALIGN( a ) a = (byte *)((int)((byte *)a + 3) & ~ 3)",
                            "#include <stdint.h>\n#define ALIGN( a ) a = (byte *)((uintptr_t)"
                            "((byte *)a + 3) & ~(uintptr_t)3)", name)
            text = text.replace("cur = (int)pData;", "cur = (int)(intptr_t)pData;")
            text = _replace(text, "#define FILEBUFFER ( 16 * 1024 * 1024)",
                            "#define FILEBUFFER ( 64 * 1024 * 1024)\n"
                            "#define MAXSTUDIOFILE ( 16 * 1024 * 1024)", name)
            check = ("\tif ({n} > MAXSTUDIOFILE)\n\t\tError( \"%s would be %d bytes: over "
                     "16 MB, too big for the client (pack the textures into atlases)\\n\", "
                     "{what}, {n} );\n")
            for length in ("pseqhdr->length", "phdr->length"):
                text = text.replace(f"\tSafeWrite( modelouthandle, pStart, {length} );",
                                    check.format(n=length, what='"the output"')
                                    + f"\tSafeWrite( modelouthandle, pStart, {length} );")
        (src / name).write_text(_portable(text), encoding="latin-1")
    for name in ("cmdlib.c", "cmdlib.h", "mathlib.c", "mathlib.h", "scriplib.c",
                 "scriplib.h", "trilib.c", "trilib.h"):
        path = hlsdk / "utils" / "common" / name
        if path.is_file():
            (src / name).write_text(_portable(path.read_text(encoding="latin-1")),
                                    encoding="latin-1")
    studio = (hlsdk / "engine" / "studio.h").read_text(encoding="latin-1")
    studio = _replace(studio, "#define MAXSTUDIOMODELS\t\t32",
                      f"#define MAXSTUDIOMODELS\t\t{max_models}", "studio.h")
    (src / "studio.h").write_text(studio, encoding="latin-1")
    lbm = (hlsdk / "utils" / "common" / "lbmlib.h").read_text(encoding="latin-1")
    lbm = lbm.replace("typedef long\t\t\tLONG;", "#ifndef _WINDOWS_\ntypedef long LONG;\n#endif")
    (src / "lbmlib.h").write_text(_portable(lbm), encoding="latin-1")
    (src / "lbmlib.c").write_text(
        _portable((hlsdk / "utils" / "common" / "lbmlib.c").read_text(encoding="latin-1")),
        encoding="latin-1")


def _portable(text: str) -> str:
    """Includes in lower case (``<STDIO.H>`` is not found on a case-sensitive
    disk) and HLSDK names that clash with libc/mingw renamed: ``gamma``
    (math.h), ``filelength`` (mingw io.h)."""
    import re
    text = re.sub(r'(#include\s*[<"])([^>"]+)([>"])',
                  lambda m: m.group(1) + m.group(2).lower() + m.group(3), text)
    text = re.sub(r"\bfilelength\b", "q_filelength", text)
    return re.sub(r"\bgamma\b", "studiomdl_gamma", text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path(__file__).parent)
    parser.add_argument("--target", choices=("native", "windows"), default="native")
    parser.add_argument("--cc", help="C compiler (default: cc, or x86_64-w64-mingw32-gcc)")
    parser.add_argument("--max-models", type=int, default=1024)
    parser.add_argument("--hlsdk", type=Path, help="an HLSDK checkout (default: clone it)")
    args = parser.parse_args()
    cc = args.cc or ("x86_64-w64-mingw32-gcc" if args.target == "windows" else
                     os.environ.get("CC", "cc"))
    work = Path(tempfile.mkdtemp())
    try:
        hlsdk = args.hlsdk
        if hlsdk is None:
            hlsdk = work / "hlsdk"
            subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none",
                            "--sparse", HLSDK, str(hlsdk)], check=True)
            subprocess.run(["git", "-C", str(hlsdk), "sparse-checkout", "set",
                            "utils/studiomdl", "utils/common", "engine", "dlls", "common",
                            "public"], check=True)
        src = work / "src"
        patch_sources(hlsdk, src, args.max_models)
        common = hlsdk / "utils" / "common"
        flags = ["-O1", "-fcommon", "-w", "-std=gnu89"]
        includes = []
        if args.target == "native":
            shim = work / "shim"
            shim.mkdir()
            (shim / "windows.h").write_text(SHIM)
            shutil.copy(shim / "windows.h", shim / "WINDOWS.H") if not (
                shim / "WINDOWS.H").exists() else None
            flags += ["-Dstricmp=strcasecmp", "-Dstrnicmp=strncasecmp",
                      "-Dstrcmpi=strcasecmp"]
            includes.append(f"-I{shim}")
        else:
            flags = ["-O1", "-fcommon", "-w", "-std=gnu11", "-static"]
        includes += [f"-I{src}", f"-I{common}", f"-I{hlsdk / 'public'}",
                     f"-I{hlsdk / 'common'}", f"-I{hlsdk / 'dlls'}",
                     f"-I{hlsdk / 'utils' / 'studiomdl'}"]
        args.out.mkdir(parents=True, exist_ok=True)
        binary = args.out / ("studiomdl.exe" if args.target == "windows" else "studiomdl")
        sources = [src / n for n in ("studiomdl.c", "write.c", "tristrip.c", "bmpread.c",
                                     "lbmlib.c")]
        sources += [src / n for n in ("cmdlib.c", "mathlib.c", "scriplib.c", "trilib.c")]
        subprocess.run([cc, "-o", str(binary), *flags, *includes, *map(str, sources), "-lm"],
                       check=True)
        print(f"built: {binary} (MAXSTUDIOMODELS {args.max_models})")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
