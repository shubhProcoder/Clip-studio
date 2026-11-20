"""Audit of built drafts and rendered MP4s."""

import json
import os
from pathlib import Path

from streamclipper.env import detect_env
from streamclipper.gate import gate_mp4, print_gate_table
from streamclipper.util import DRAFT_EXTRA_BYTES, ToolError, fwd, utf16_len

AUDIT_CHECKS = (
    ("companion_files", "Missing companion file"),
    ("draft_extra", "draft.extra"),
    ("media_files", "Missing media"),
    ("tracks", ("Track ", "Video seg", "Audio seg")),
    ("text_styles", "Text seg"),
    ("registered", ("root_meta_info", "Not registered")),
)


def cmd_audit(args):
    if args.mp4:
        mp4 = Path(args.mp4).resolve()
        ok = print_gate_table(f"EXPORT GATE: {fwd(mp4)}", gate_mp4(mp4, args.timeline or None))
        if not ok:
            raise SystemExit(1)
        if not (args.draft or args.project):
            return
    name = args.draft or args.project
    if not name:
        raise ToolError("Nothing to audit.", "pass --draft <draft folder name> or --mp4 <video.mp4>")
    env = detect_env(save=False)
    draft_root = Path(args.draft_root or env["draft_root"])
    pdir = draft_root / name
    if not (pdir / "draft_content.json").is_file():
        raise ToolError(
            f"Draft not found: {fwd(pdir)}", "check the draft name (folder under the draft root) or pass --draft-root"
        )
    dc = json.loads((pdir / "draft_content.json").read_text(encoding="utf-8"))
    errors = []
    for key in ("draft_meta_info.json", "draft.extra", "draft_settings", "timeline_layout.json"):
        if not (pdir / key).is_file():
            errors.append(f"Missing companion file: {key}")
    if (pdir / "draft.extra").is_file() and (pdir / "draft.extra").read_bytes() != DRAFT_EXTRA_BYTES:
        errors.append("draft.extra is not the 26-byte value CapCut writes")
    vid_ids = {m["id"]: m for m in dc["materials"].get("videos", [])}
    aud_ids = {m["id"]: m for m in dc["materials"].get("audios", [])}
    txt_ids = {m["id"]: m for m in dc["materials"].get("texts", [])}
    eff_ids = {m["id"]: m for m in dc["materials"].get("effects", [])}
    all_ids = {
        m.get("id") for lst in dc["materials"].values() if isinstance(lst, list) for m in lst if isinstance(m, dict)
    }
    for m_type in ("videos", "audios"):
        for m in dc["materials"].get(m_type, []):
            if not os.path.isfile(m["path"]):
                errors.append(f"Missing media: {m['path']}")
    for t in dc.get("tracks", []):
        ttype = t.get("type")
        prev_e = 0
        for i, seg in enumerate(t["segments"]):
            st = seg["target_timerange"]["start"]
            du = seg["target_timerange"]["duration"]
            if st < prev_e:
                errors.append(f"Track {ttype} seg {i} overlaps ({st} < {prev_e})")
            prev_e = st + du
            for ref in seg.get("extra_material_refs", []):
                if ref not in all_ids:
                    errors.append(f"Track {ttype} seg {i} references missing material {ref}")
            mid = seg.get("material_id")
            if ttype == "video" and mid not in vid_ids:
                errors.append(f"Video seg {i} missing material {mid}")
            elif ttype == "audio" and mid not in aud_ids:
                errors.append(f"Audio seg {i} missing material {mid}")
            elif ttype == "text":
                if mid not in txt_ids:
                    errors.append(f"Text seg {i} missing material {mid}")
                else:
                    cobj = json.loads(txt_ids[mid]["content"])
                    tlen = utf16_len(cobj["text"])
                    styles = cobj.get("styles", [])
                    if not styles:
                        errors.append(f"Text seg {i} has empty styles")
                    elif styles[0]["range"][0] != 0 or styles[-1]["range"][1] != tlen:
                        errors.append(
                            f"Text seg {i} UTF-16 style ranges {[s['range'] for s in styles]} do not span [0, {tlen}]"
                        )
                    for st_item in styles:
                        for sh in st_item.get("shadows", []):
                            if float(sh.get("diffuse", 0.0)) > 0.15:
                                errors.append(
                                    f"Text seg {i} shadow diffuse {sh.get('diffuse')} > 0.15 "
                                    "(causes yellow blob/glyph overflow!)"
                                )
    rm_path = draft_root / "root_meta_info.json"
    if not rm_path.is_file():
        errors.append("root_meta_info.json missing in the draft root (CapCut will not list the draft)")
    else:
        rm = json.loads(rm_path.read_text(encoding="utf-8"))
        if name not in {s.get("draft_name") for s in rm.get("all_draft_store", [])}:
            errors.append("Not registered in root_meta_info.json")
    rows = []
    for check, prefix in AUDIT_CHECKS:
        hits = [e for e in errors if e.startswith(prefix)]
        rows.append(
            (
                check,
                "FAIL" if hits else "PASS",
                (hits[0] + (f" (+{len(hits) - 1} more)" if len(hits) > 1 else "")) if hits else "ok",
            )
        )
    print_gate_table(f"DRAFT AUDIT: {name}", rows)
    if errors:
        print(f"AUDIT FAILED: {name} ({len(errors)} errors)")
        for e in errors:
            print(f"  - {e}")
        raise SystemExit(1)
    print(
        f"AUDIT PASSED: {name} (dur={dc['duration'] / 1e6:.2f}s, fps={dc.get('fps')}, "
        f"tracks={[t['type'] for t in dc['tracks']]}, text_glows={len(eff_ids)}, 0 errors)"
    )
