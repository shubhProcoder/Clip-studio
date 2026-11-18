from streamclipper.draft.rootmeta import RENDER_SUFFIXES, unique_output_name


def test_free_name_is_kept(tmp_path):
    assert unique_output_name(tmp_path, "clip_01") == "clip_01"


def test_existing_mp4_or_plan_gives_suffix(tmp_path):
    (tmp_path / "clip_01.mp4").write_bytes(b"x")
    assert unique_output_name(tmp_path, "clip_01") == "clip_01_2"
    (tmp_path / "clip_01_2.clip.json").write_text("{}", encoding="utf-8")
    assert unique_output_name(tmp_path, "clip_01") == "clip_01_3"


def test_every_sidecar_counts(tmp_path):
    for k, suffix in enumerate(("_spec.json", ".timeline.json", "_timeline.json", "_thumb.jpg")):
        d = tmp_path / f"case{k}"
        d.mkdir()
        (d / f"c{suffix}").write_text("x", encoding="utf-8")
        assert unique_output_name(d, "c") == "c_2", suffix


def test_gate_sidecar_alone_takes_the_name(tmp_path):
    (tmp_path / "clip_01.gate.json").write_text("{}", encoding="utf-8")
    assert unique_output_name(tmp_path, "clip_01") == "clip_01_2"
    assert unique_output_name(tmp_path, "clip_01", suffixes=RENDER_SUFFIXES) == "clip_01_2"


def test_draft_folder_counts_only_with_draft_root(tmp_path):
    out, root = tmp_path / "out", tmp_path / "drafts"
    out.mkdir()
    (root / "GTA6_Viral_01").mkdir(parents=True)
    assert unique_output_name(out, "GTA6_Viral_01", root) == "GTA6_Viral_01_2"
    assert unique_output_name(out, "GTA6_Viral_01", None) == "GTA6_Viral_01"


def test_render_suffixes_ignore_the_plan_itself(tmp_path):
    (tmp_path / "clip_01.clip.json").write_text("{}", encoding="utf-8")
    assert unique_output_name(tmp_path, "clip_01", suffixes=RENDER_SUFFIXES) == "clip_01"
    (tmp_path / "clip_01.mp4").write_bytes(b"x")
    assert unique_output_name(tmp_path, "clip_01", suffixes=RENDER_SUFFIXES) == "clip_01_2"


def test_default_prefix_is_clip():
    from streamclipper.cli import build_parser

    p = build_parser()
    assert p.parse_args(["clip-stream", "--input", "x.mp4", "--out", "w"]).prefix == "clip"
    assert p.parse_args(["render", "--clips", "c.json", "--video", "x.mp4"]).prefix == "clip"
