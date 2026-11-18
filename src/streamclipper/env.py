"""CapCut detection, bundled assets (fonts, effects, SFX), fonts and doctor checks."""

import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

from streamclipper.captions.styles import FONT_FILES
from streamclipper.paths import ENV_FILE, TEMPLATE_DIR
from streamclipper.util import TOOL_HINTS, ToolError, fwd, ytdlp_cmd

_ASSET_NOTICE = []  # one 'effects skipped' notice per run


GLOW_PRESETS = {
    "neon": {
        "effect_id": "7194814295517958657",
        "name": "glowing neon",
        "subdir": "b1985a36a9c9ecd0395004fb85ab9097",
        "strength": 0.36,
        "range": 0.32,
    },
    "deep": {
        "effect_id": "7367637998579487248",
        "name": "DeepGlow",
        "subdir": "9c7e9af83b4e4effc689afcd35febcd5",
        "strength": 0.38,
        "range": 0.34,
    },
    "rays": {
        "effect_id": "7194814374001775105",
        "name": "glowing rays",
        "subdir": "13fd5bb6c22236bb299c266428395180",
        "strength": 0.32,
        "range": 0.30,
    },
}


TRANSITION_PRESETS = {
    "zoom-switch": {
        "effect_id": "7677936466021895431",
        "name": "Zoom Switch",
        "subdir": "c54aedc4567b4324c702eee82cd6f7e5",
        "duration": 333333,
        "is_overlap": True,
    },
    "zoom-rotate": {
        "effect_id": "7687146916689513748",
        "name": "Zoom Rotate",
        "subdir": "f1b480690d541beff35c478a3fccc2b1",
        "duration": 400000,
        "is_overlap": True,
    },
    "heat-flicks": {
        "effect_id": "7670091305904557319",
        "name": "Heat Flicks",
        "subdir": "e289cabc5d1b33297a875da4d63b7ed8",
        "duration": 266666,
        "is_overlap": True,
    },
}


def find_capcut_local() -> Path:
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CapCut"
    elif sys.platform == "darwin":
        # CapCut for Mac keeps its User Data under ~/Movies/CapCut; older builds used Application Support
        local = Path.home() / "Movies/CapCut"
        if not local.is_dir():
            local = Path.home() / "Library/Application Support/CapCut"
    else:
        local = Path.home() / ".local/share/CapCut"
    return local


def find_draft_root() -> Path:
    override = os.environ.get("CAPCUT_DRAFT_ROOT")
    if override and Path(override).is_dir():
        return Path(override)
    return default_draft_root()


def default_draft_root() -> Path:
    """CapCut's own draft folder, ignoring the CAPCUT_DRAFT_ROOT override."""
    local = find_capcut_local()
    candidates = [
        local / "User Data/Projects/com.lveditor.draft",
        local / "User Data/Projects/draft",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def ensure_bundled_assets(capcut_local: Path, install: bool = True) -> dict:
    """
    Locate CapCut glow shaders and transitions, fonts and SFX.
    Shaders/transitions are used from the user's own CapCut cache when present. Only when
    CapCut is installed and an id is missing (and install=True) is the bundled copy installed,
    with a notice. Nothing is created when CapCut is not installed; nothing is ever deleted.
    """
    eff_cache = capcut_local / "User Data/Cache/effect"
    capcut_installed = capcut_local.is_dir()
    sources = {}

    def _locate(kind: str, presets: dict, tpl_sub: str) -> dict:
        found = {}
        for key, info in presets.items():
            eid = info["effect_id"]
            dst_dir = eff_cache / eid
            sub_path = dst_dir / info["subdir"]
            label = f"{kind} {info['name']}"
            if sub_path.is_dir():
                found[key] = fwd(sub_path)
                sources[label] = "found in your CapCut cache"
                continue
            src_dir = TEMPLATE_DIR / tpl_sub / eid
            if not capcut_installed:
                sources[label] = "missing (CapCut Desktop not installed)"
                continue
            if not (src_dir / info["subdir"]).is_dir():
                sources[label] = "missing (not in your CapCut cache or data/templates; skipped)"
                continue
            if not install:
                sources[label] = "missing"
                continue
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
            print(
                f"Installed CapCut effect {info['name']} from bundled templates; "
                f"these are CapCut assets, see README Licensing"
            )
            found[key] = fwd(sub_path)
            sources[label] = "copied from bundled templates"
        return found

    installed_glows = _locate("glow", GLOW_PRESETS, "effects")
    installed_trans = _locate("transition", TRANSITION_PRESETS, "transitions")
    skipped = [k for k, v in sources.items() if v.startswith("missing (not in")]
    if install and skipped and not _ASSET_NOTICE:
        _ASSET_NOTICE.append(True)
        print(
            f"Notice: {len(skipped)} CapCut glow/transition effects are not available here; "
            f"drafts are built without them (open CapCut and use these effects once to cache them)."
        )

    fonts_dir = TEMPLATE_DIR / "fonts"
    installed_fonts = {}
    for fkey, fname in FONT_FILES.items():
        fpath = fonts_dir / fname
        if fpath.is_file():
            installed_fonts[fkey] = str(fpath.resolve()).replace("\\", "/")

    # Local templates/sfx (not in the public repo) first, then the project's own generated SFX.
    sfx_dir = TEMPLATE_DIR / "sfx"
    installed_sfx = {}
    for sfx_name in ("whoosh_swipe.mp3", "pop_click.mp3", "sub_drop_boom.wav"):
        sp = sfx_dir / sfx_name
        if not sp.is_file():
            sp = TEMPLATE_DIR / "sfx_generated" / (Path(sfx_name).stem + ".wav")
        if sp.is_file():
            installed_sfx[sfx_name.split("_")[0]] = str(sp.resolve()).replace("\\", "/")

    return {
        "glows": installed_glows,
        "transitions": installed_trans,
        "fonts": installed_fonts,
        "sfx": installed_sfx,
        "sources": sources,
    }


def find_font_fallback(capcut_local: Path) -> str:
    bundled = TEMPLATE_DIR / "fonts" / "BarlowCondensed-Black.ttf"
    if bundled.is_file():
        return str(bundled.resolve()).replace("\\", "/")
    user_font = capcut_local / "User Data/Resources/Font/SystemFont/CapCutSansText-Medium.otf"
    if user_font.is_file():
        return str(user_font).replace("\\", "/")
    win_arial = Path("C:/Windows/Fonts/arialbd.ttf")
    if win_arial.is_file():
        return str(win_arial).replace("\\", "/")
    return ""


def resolve_font(font_choice: str, text_sample: str, assets: dict, capcut_local: Path) -> tuple[str, str]:
    """
    Pick the optimal font path & ID:
      - If text contains Devanagari characters, automatically use `Baloo2-ExtraBold.ttf`!
      - Otherwise use the requested preset (`barlow`, `anton`, `baloo`, `montserrat`).
    """
    fonts = assets.get("fonts", {})
    has_devanagari = any(0x0900 <= ord(ch) <= 0x097F for ch in (text_sample or ""))
    if has_devanagari and "baloo" in fonts:
        return fonts["baloo"], ""
    fp = Path(str(font_choice or ""))
    if fp.suffix.lower() in (".ttf", ".otf") and fp.is_file():  # a brand kit font file
        return str(fp.resolve()).replace("\\", "/"), ""
    choice = (font_choice or "barlow").lower()
    if choice in fonts:
        return fonts[choice], ""
    if "barlow" in fonts:
        return fonts["barlow"], ""
    if "montserrat" in fonts:
        return fonts["montserrat"], "7265696998664948225"
    return find_font_fallback(capcut_local), ""


def detect_env(save: bool = True, install_assets: bool = False) -> dict:
    cc_local = find_capcut_local()
    draft_root = find_draft_root()
    assets = ensure_bundled_assets(cc_local, install=install_assets)
    sys_font_path = find_font_fallback(cc_local)
    existing_drafts = []
    if draft_root.is_dir():
        existing_drafts = sorted(
            [d.name for d in draft_root.iterdir() if d.is_dir() and (d / "draft_content.json").is_file()]
        )
    env = {
        "version": "3.0.0",
        "capcut_local": str(cc_local).replace("\\", "/"),
        "draft_root": str(draft_root).replace("\\", "/"),
        "draft_root_exists": draft_root.is_dir(),
        "bundled_fonts": assets["fonts"],
        "bundled_glow_shaders": assets["glows"],
        "bundled_transitions": assets["transitions"],
        "bundled_sfx": assets["sfx"],
        "system_font_path": sys_font_path,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "existing_draft_count": len(existing_drafts),
    }
    if save:
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENV_FILE.write_text(json.dumps(env, indent=2), encoding="utf-8")
    return env


def kill_capcut():
    """Force-close CapCut. Only used when the user passes --close-capcut."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", "CapCut.exe"], capture_output=True)
        subprocess.run(["taskkill", "/F", "/IM", "VEDetector.exe"], capture_output=True)
        time.sleep(0.35)


def _win_process_names() -> set | None:
    """Lower-case exe names of running processes via a Toolhelp snapshot (fast; tasklist can take 20 s)."""
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return None
        names = set()
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                names.add(entry.szExeFile.lower())
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
        return names
    except Exception:
        return None


def capcut_running() -> bool:
    if sys.platform != "win32":
        return False
    names = _win_process_names()
    if names is not None:
        return "capcut.exe" in names
    try:
        res = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq CapCut.exe", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return "capcut.exe" in (res.stdout or "").lower()


def ttf_name(path: Path, name_id: int) -> str:
    """Read one entry of a TrueType/OpenType 'name' table (prefers Windows Unicode English)."""
    try:
        data = Path(path).read_bytes()
        num_tables = struct.unpack(">H", data[4:6])[0]
        off = None
        for i in range(num_tables):
            rec = data[12 + 16 * i : 28 + 16 * i]
            if rec[:4] == b"name":
                off = struct.unpack(">I", rec[8:12])[0]
                break
        if off is None:
            return ""
        _, count, str_off = struct.unpack(">HHH", data[off : off + 6])
        best = ""
        for i in range(count):
            pid, eid, lid, nid, length, noff = struct.unpack(">HHHHHH", data[off + 6 + 12 * i : off + 18 + 12 * i])
            if nid != name_id:
                continue
            raw = data[off + str_off + noff : off + str_off + noff + length]
            if pid == 3:
                txt = raw.decode("utf-16-be", errors="replace")
                if lid == 0x409:
                    return txt.strip()
                best = best or txt
            elif pid == 1 and not best:
                best = raw.decode("latin-1", errors="replace")
        return best.strip()
    except (OSError, struct.error):
        return ""


def ttf_full_name(path: Path) -> str:
    return ttf_name(path, 4)


def _first_line(cmd: list) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return ((res.stdout or res.stderr or "").strip().splitlines() or [""])[0]
    except (OSError, subprocess.TimeoutExpired):
        return ""


_CUDA_DLL_DIRS = []  # folders already added (prepare_cuda runs once)
_CUDA_LINUX_DONE = []  # Linux: the preload ran (it may have found nothing to load)


def prepare_cuda() -> list:
    """
    Windows: make the pip-installed CUDA DLLs (nvidia-cublas-cu12, nvidia-cudnn-cu12, torch/lib) findable
    for ctranslate2. Linux: preload the pip-installed CUDA 12 libraries (the gpu extra) when the system has
    none, e.g. a Colab image with another CUDA version ("libcublas.so.12 is not found"). Returns the folders used.
    """
    if _CUDA_DLL_DIRS:
        return list(_CUDA_DLL_DIRS)
    import site

    roots = list(site.getsitepackages())
    try:
        roots.append(site.getusersitepackages())
    except AttributeError:
        pass
    if sys.platform.startswith("linux"):
        if not _CUDA_LINUX_DONE:
            _CUDA_LINUX_DONE.append(True)
            _CUDA_DLL_DIRS.extend(preload_linux_cuda(roots))
        return list(_CUDA_DLL_DIRS)
    if sys.platform != "win32":
        return []
    for root in roots:
        for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "torch/lib"):
            d = Path(root) / sub
            if d.is_dir() and str(d) not in _CUDA_DLL_DIRS:
                try:
                    os.add_dll_directory(str(d))
                except OSError:
                    continue
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                _CUDA_DLL_DIRS.append(str(d))
    return list(_CUDA_DLL_DIRS)


# loaded in this order: cuBLAS before cuDNN (cuDNN 9 links its sub-libraries itself)
_LINUX_CUDA_LIBS = (
    ("nvidia/cublas/lib", ("libcublasLt.so.12", "libcublas.so.12")),
    ("nvidia/cudnn/lib", ("libcudnn.so.9",)),
)


def preload_linux_cuda(roots: list, cdll=None) -> list:
    """
    dlopen the CUDA 12 libraries of the nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels with RTLD_GLOBAL, so
    ctranslate2's later dlopen("libcublas.so.12") finds them. Skipped for a library the system already has
    (ldconfig). LD_LIBRARY_PATH cannot be used: it is read once when the process starts.
    """
    import ctypes

    cdll = cdll or (lambda path: ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL))
    used = []
    for sub, names in _LINUX_CUDA_LIBS:
        for root in roots:
            d = Path(root) / sub
            if not d.is_dir():
                continue
            for name in names:
                if _system_has_lib(name) or not (d / name).is_file():
                    continue
                try:
                    cdll(str(d / name))
                except OSError:
                    continue
                if str(d) not in used:
                    used.append(str(d))
            break
    return used


def _system_has_lib(name: str) -> bool:
    import ctypes.util

    return ctypes.util.find_library(name.split(".so")[0][3:]) == name


def gpu_info() -> tuple:
    """(name, vram_mb) of the first NVIDIA GPU from nvidia-smi, or ('', 0)."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return "", 0
    line = _first_line([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        return "", 0
    try:
        return parts[0], int(float(parts[1]))
    except ValueError:
        return parts[0], 0


def cuda_status() -> tuple:
    """(ok, detail): can faster-whisper (ctranslate2) use a CUDA GPU here?"""
    prepare_cuda()
    try:
        import ctranslate2
    except Exception:
        return False, "ctranslate2 not installed (comes with faster-whisper)"
    try:
        n = ctranslate2.get_cuda_device_count()
    except Exception as e:
        return False, f"CUDA check failed: {e}"
    if n < 1:
        return False, f"no CUDA device (ctranslate2 {ctranslate2.__version__}); CPU is used"
    name, vram = gpu_info()
    gpu = f"{name}, {vram} MB" if name else "GPU"
    return True, f"{gpu}; ctranslate2 {ctranslate2.__version__} sees {n} CUDA device(s)"


def doctor_checks(env: dict, assets: dict) -> list:
    """Rows of (CHECK, STATUS, DETAIL, FIX)."""
    rows = []
    ok, miss, warn = "OK", "MISSING", "WARN"
    pv = sys.version_info
    rows.append(
        (
            "Python >= 3.10",
            ok if pv >= (3, 10) else miss,
            f"{pv.major}.{pv.minor}.{pv.micro}",
            "" if pv >= (3, 10) else "install Python 3.10+ from https://python.org",
        )
    )

    ffm = shutil.which("ffmpeg")
    rows.append(
        (
            "ffmpeg",
            ok if ffm else miss,
            _first_line([ffm, "-hide_banner", "-version"])[:60] if ffm else "not on PATH",
            "" if ffm else TOOL_HINTS["ffmpeg"],
        )
    )
    ffp = shutil.which("ffprobe")
    rows.append(
        ("ffprobe", ok if ffp else miss, "found" if ffp else "not on PATH", "" if ffp else TOOL_HINTS["ffprobe"])
    )
    if ffm:
        bc = (
            subprocess.run(
                [ffm, "-hide_banner", "-buildconf"], capture_output=True, text=True, encoding="utf-8", errors="replace"
            ).stdout
            or ""
        )
        flt = (
            subprocess.run(
                [ffm, "-hide_banner", "-filters"], capture_output=True, text=True, encoding="utf-8", errors="replace"
            ).stdout
            or ""
        )
        flt_names = {ln.split()[1] for ln in flt.splitlines() if len(ln.split()) > 2}
        for feat in ("libass", "libharfbuzz"):
            have = f"--enable-{feat}" in bc
            rows.append(
                (
                    f"ffmpeg {feat}",
                    ok if have else warn,
                    "enabled" if have else "not in build",
                    "" if have else "install a full ffmpeg build (winget install Gyan.FFmpeg)",
                )
            )
        for f in ("loudnorm", "ebur128", "blackdetect", "ass"):
            have = f in flt_names
            rows.append(
                (
                    f"ffmpeg filter {f}",
                    ok if have else warn,
                    "available" if have else "missing",
                    "" if have else "install a full ffmpeg build (winget install Gyan.FFmpeg)",
                )
            )

    try:
        yc = ytdlp_cmd()
        ver = _first_line(yc + ["--version"])
        rows.append(("yt-dlp", ok, f"{ver} ({'python module' if len(yc) > 1 else 'exe'})", ""))
    except ToolError:
        rows.append(("yt-dlp", miss, "not installed", TOOL_HINTS["yt-dlp"]))

    if importlib.util.find_spec("streamlink") is not None:
        try:
            from importlib.metadata import version as _ver

            sv = _ver("streamlink")
        except Exception:
            sv = "installed"
        rows.append(("streamlink (live mode)", ok, sv, ""))
    else:
        rows.append(("streamlink (live mode)", warn, "not installed (no 'live' command)", TOOL_HINTS["streamlink"]))
    if importlib.util.find_spec("cv2") is not None:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _ver

        found = []
        for dist in (
            "opencv-python-headless",
            "opencv-python",
            "opencv-contrib-python",
            "opencv-contrib-python-headless",
        ):
            try:
                found.append((dist, _ver(dist)))
            except PackageNotFoundError:
                continue
        cv = ", ".join(f"{d} {v}" for d, v in found) or "installed"
        if len(found) > 1:  # each wheel ships its own cv2 folder: the last one installed overwrites the others
            keep = "opencv-contrib-python" if any(d == "opencv-contrib-python" for d, _ in found) else found[0][0]
            others = " ".join(d for d, _ in found if d != keep)
            rows.append(
                (
                    "OpenCV (face detection)",
                    warn,
                    f"{len(found)} OpenCV packages installed ({cv}); they overwrite each other's cv2",
                    f"python -m pip uninstall -y {others} && python -m pip install --force-reinstall {keep}",
                )
            )
        else:
            rows.append(("OpenCV (face detection)", ok, cv, ""))
    else:
        rows.append(
            (
                "OpenCV (face detection)",
                warn,
                "not installed (facecam found by skin colour, less reliable)",
                'python -m pip install -e ".[vision]"',
            )
        )
    from streamclipper.faces import detector_status

    fd_name, fd_detail = detector_status()
    if fd_name == "MediaPipe":
        rows.append(("face detector", ok, f"{fd_name}: {fd_detail}", ""))
    else:
        fix = 'python -m pip install -e ".[vision]"'
        if "could not start" in fd_detail:
            fix = "apt-get install -y libegl1 libgles2"
        rows.append(("face detector", warn, f"{fd_name}: {fd_detail}", fix))

    if importlib.util.find_spec("numpy") is not None:
        try:
            from importlib.metadata import version as _ver

            nv = _ver("numpy")
        except Exception:
            nv = "installed"
        rows.append(("numpy", ok, nv, ""))
    else:
        rows.append(("numpy", miss, "not installed", TOOL_HINTS["numpy"]))

    if importlib.util.find_spec("faster_whisper") is not None:
        try:
            from importlib.metadata import version as _ver

            fv = _ver("faster-whisper")
        except Exception:
            fv = "installed"
        hub = Path.home() / ".cache" / "huggingface" / "hub"
        models = (
            sorted(
                p.name.replace("models--Systran--faster-whisper-", "")
                for p in hub.glob("models--Systran--faster-whisper-*")
            )
            if hub.is_dir()
            else []
        )
        rows.append(("faster-whisper (optional)", ok, f"{fv}; cached models: {', '.join(models) or 'none'}", ""))
        cuda_ok, cuda_detail = cuda_status()
        rows.append(
            (
                "CUDA for Whisper (optional)",
                ok if cuda_ok else warn,
                cuda_detail,
                "" if cuda_ok else "NVIDIA GPU + driver, then: pip install -e .[gpu] (CPU works, slower)",
            )
        )
    else:
        rows.append(
            ("faster-whisper (optional)", warn, "not installed (no word captions)", TOOL_HINTS["faster-whisper"])
        )

    cc_local = Path(env["capcut_local"])
    apps = cc_local / "Apps"
    versions = (
        sorted(
            [p.name for p in apps.iterdir() if p.is_dir() and re.match(r"^\d+\.\d+", p.name)],
            key=lambda v: [int(x) for x in re.findall(r"\d+", v)],
        )
        if apps.is_dir()
        else []
    )
    mac_app = Path("/Applications/CapCut.app/Contents/Info.plist")
    if not versions and sys.platform == "darwin" and mac_app.is_file():
        import plistlib

        try:
            with mac_app.open("rb") as f:
                versions = [str(plistlib.load(f).get("CFBundleShortVersionString") or "installed")]
        except Exception:
            versions = ["installed"]
    rows.append(
        (
            "CapCut Desktop",
            ok if versions else miss,
            ", ".join(versions) or "not found",
            "" if versions else "install CapCut Desktop from https://www.capcut.com and open it once",
        )
    )
    dr = Path(env["draft_root"])
    if dr.is_dir():
        writable = os.access(dr, os.W_OK)
        rows.append(
            (
                "Draft root",
                ok if writable else miss,
                f"{fwd(dr)} ({env['existing_draft_count']} drafts)",
                "" if writable else "check folder permissions",
            )
        )
    else:
        rows.append(
            ("Draft root", miss, fwd(dr), "open CapCut Desktop once, or pass --draft-root / set CAPCUT_DRAFT_ROOT")
        )
    running = capcut_running()
    rows.append(
        (
            "CapCut running",
            warn if running else ok,
            "running" if running else "not running",
            "close CapCut before building drafts (or pass --close-capcut)" if running else "",
        )
    )

    for fkey, fname in FONT_FILES.items():
        have = fkey in assets["fonts"]
        rows.append(
            (
                f"font {fname}",
                ok if have else miss,
                "bundled" if have else "missing",
                "" if have else "restore templates/fonts from the release zip",
            )
        )
    for label, src in assets.get("sources", {}).items():
        good = not src.startswith("missing")
        rows.append(
            (
                label,
                ok if good else warn,
                src,
                "" if good else "open CapCut once and use this effect, or keep templates/ intact",
            )
        )
    for sname in ("pop", "whoosh", "sub"):
        have = sname in assets["sfx"]
        rows.append(
            (
                f"sfx {sname}",
                ok if have else warn,
                "bundled" if have else "missing",
                "" if have else "restore templates/sfx from the release zip",
            )
        )
    return rows
