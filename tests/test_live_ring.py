from streamclipper.live.ring import Ring


def _write(ring_dir, names_durs, csv_name="live.csv"):
    lines, t = [], 0.0
    for name, dur in names_durs:
        (ring_dir / name).write_bytes(b"x")
        lines.append(f"{name},{t:.6f},{t + dur:.6f}\n")
        t += dur
    (ring_dir / csv_name).write_text("".join(lines), encoding="utf-8")


def test_poll_assigns_contiguous_stream_time(tmp_path):
    ring = Ring(tmp_path / "buffer", keep_s=60)
    _write(ring.dir, [(f"seg_{i:06d}.ts", 10.0) for i in range(3)])
    ring.watch(ring.dir / "live.csv")
    new = ring.poll(now=100.0)
    assert [s.number for s in new] == [0, 1, 2]
    assert [s.start for s in new] == [0.0, 10.0, 20.0]
    assert ring.end == 30.0 and ring.next_number() == 3
    assert ring.poll() == []  # nothing new


def test_partial_line_waits_and_second_list_continues_time(tmp_path):
    ring = Ring(tmp_path / "buffer")
    (ring.dir / "seg_000000.ts").write_bytes(b"x")
    (ring.dir / "seg_000001.ts").write_bytes(b"x")
    (ring.dir / "live.csv").write_text("seg_000000.ts,0.0,10.0\nseg_000001.ts,10.0,1", encoding="utf-8")
    ring.watch(ring.dir / "live.csv")
    assert len(ring.poll()) == 1  # second line not finished yet
    (ring.dir / "live.csv").write_text("seg_000000.ts,0.0,10.0\nseg_000001.ts,10.0,18.0\n", encoding="utf-8")
    assert [s.dur for s in ring.poll()] == [8.0]
    # a reconnect: a new list whose times restart at 0; stream time continues
    (ring.dir / "seg_000002.ts").write_bytes(b"x")
    (ring.dir / "live_01.csv").write_text("seg_000002.ts,0.0,10.0\n", encoding="utf-8")
    ring.watch(ring.dir / "live_01.csv")
    (seg,) = ring.poll()
    assert seg.start == 18.0 and seg.end == 28.0


def test_prune_deletes_only_own_segments(tmp_path):
    ring = Ring(tmp_path / "buffer", keep_s=20)
    _write(ring.dir, [(f"seg_{i:06d}.ts", 10.0) for i in range(5)])
    stranger = ring.dir / "seg_000099.ts"  # same pattern, but no list of this run names it
    stranger.write_bytes(b"user")
    other = ring.dir / "notes.txt"
    other.write_text("keep", encoding="utf-8")
    outside = tmp_path / "seg_000000.ts"  # same name, other folder
    outside.write_bytes(b"user")
    ring.watch(ring.dir / "live.csv")
    ring.poll()
    assert ring.prune() == 3  # end 50 s, keep 20 s -> segments ending <= 30 s go
    assert not (ring.dir / "seg_000000.ts").exists()
    assert (ring.dir / "seg_000003.ts").exists() and (ring.dir / "seg_000004.ts").exists()
    assert stranger.exists() and other.exists() and outside.exists()
    assert not ring.owns(stranger) and not ring.owns(outside)
    assert ring.prune() == 0


def test_window_selection_across_segment_edges(tmp_path):
    ring = Ring(tmp_path / "buffer", keep_s=600)
    _write(ring.dir, [(f"seg_{i:06d}.ts", d) for i, d in enumerate([10.0, 9.5, 10.5, 10.0])])
    ring.watch(ring.dir / "live.csv")
    ring.poll()
    assert [s.number for s in ring.select(9.0, 21.0)] == [0, 1, 2]
    assert [s.number for s in ring.select(10.0, 19.5)] == [1]  # touching edges are not overlaps
    assert [s.number for s in ring.select(19.6, 40.0)] == [2, 3]
    assert ring.segment_at(19.4).number == 1


def test_select_refuses_deleted_segments(tmp_path):
    import pytest

    from streamclipper.util import ToolError

    ring = Ring(tmp_path / "buffer", keep_s=10)
    _write(ring.dir, [(f"seg_{i:06d}.ts", 10.0) for i in range(3)])
    ring.watch(ring.dir / "live.csv")
    ring.poll()
    ring.prune()
    with pytest.raises(ToolError):
        ring.select(5.0, 15.0)


def test_wall_to_stream(tmp_path):
    ring = Ring(tmp_path / "buffer")
    assert ring.wall_to_stream(5.0) == 0.0
    _write(ring.dir, [("seg_000000.ts", 10.0)])
    ring.watch(ring.dir / "live.csv")
    ring.poll(now=1000.0)
    assert ring.wall_to_stream(1004.0) == 14.0
