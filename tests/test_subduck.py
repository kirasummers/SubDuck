from pathlib import Path

import subduck.profiles as profiles_mod
from subduck.matcher import (
    build_word_regex,
    calculate_merged_mute_intervals,
    censor_text_line,
    censor_word_token,
    scan_subtitles_for_triggers,
)
from subduck.models import (
    CensorConfig,
    SubtitleEntry,
    VideoFileInfo,
    sec_to_srt_time,
    srt_time_to_sec,
)
from subduck.processor import (
    build_output_paths,
    get_container_subtitle_codec,
    get_default_season_mirror_dir,
    safe_copy_file,
)
from subduck.sub_parser import (
    extract_subtitles_from_video,
    parse_srt_content,
    serialize_srt,
)


def test_timestamps():
    assert sec_to_srt_time(3723.456) == "01:02:03,456"
    assert abs(srt_time_to_sec("01:02:03,456") - 3723.456) < 1e-3
    assert sec_to_srt_time(-1.0) == "00:00:00,000"
    assert sec_to_srt_time(1.9999) == "00:00:02,000"
    assert abs(srt_time_to_sec("04:15.250") - 255.25) < 1e-3


def test_file_info_props():
    v = VideoFileInfo(path=Path("/tmp/ep01.mkv"), filename="ep01.mkv", file_size=1536 * 1024, duration_sec=3665.0)
    assert v.duration_str == "01:01:05"
    assert v.size_str == "1.5 MB"


def test_regex_boundaries():
    rx = build_word_regex("cat", strict=True)
    assert rx.search("black cat outside")
    assert rx.search("cat-like reflexes")
    assert not rx.search("scatter across")
    assert not rx.search("bobcat tracks")

    loose = build_word_regex("cat", strict=False)
    assert loose.search("scatter across")

    # Compound flexibility (spaces and hyphens interchangeable)
    compound_space = build_word_regex("panic attack", strict=True)
    assert compound_space.search("having a panic attack")
    assert compound_space.search("having a panic-attack")
    assert compound_space.search("having a panic_attack")

    compound_hyphen = build_word_regex("anti-inflammatory", strict=True)
    assert compound_hyphen.search("took an anti-inflammatory pill")
    assert compound_hyphen.search("took an anti inflammatory pill")


def test_censor_tokens():
    assert censor_word_token(" needle"[1:], "asterisks") == "******"
    assert censor_word_token("needle", "inner_asterisks") == "n****e"
    assert censor_word_token("no", "inner_asterisks") == "**"
    assert censor_word_token("needle", "tag") == "[censored]"
    assert censor_word_token("needle", "block") == "██████"
    assert censor_word_token("needle", "remove") == ""

    out, hits = censor_text_line("found a needle on the floor", ["needle"], style="remove", strict=True)
    assert out == "found a on the floor"
    assert hits == ["needle"]


def test_mute_windows():
    cues = [
        SubtitleEntry(1, 10.0, 12.0, "nothing here"),
        SubtitleEntry(2, 20.0, 21.5, "loud crash downstairs"),
        SubtitleEntry(3, 21.7, 23.0, "another crash outside"),
    ]
    cfg = CensorConfig(trigger_words=["crash"], safety_before_ms=200, safety_after_ms=200)
    matches = scan_subtitles_for_triggers(cues, cfg)
    assert len(matches) == 2

    wins = calculate_merged_mute_intervals(matches, 200, 200)
    assert len(wins) == 1
    assert abs(wins[0][0] - 19.8) < 1e-3
    assert abs(wins[0][1] - 23.2) < 1e-3

    # word-level timing narrows window inside a longer cue
    w_cue = SubtitleEntry(
        index=4,
        start_sec=30.0,
        end_sec=36.0,
        text="walked past the wasps near the shed",
        word_timings=[
            ("walked", 30.0, 30.5),
            ("past", 30.5, 30.9),
            ("the", 30.9, 31.1),
            ("wasps", 32.8, 33.5),
            ("near", 33.5, 33.8),
            ("the", 33.8, 34.0),
            ("shed", 34.0, 34.5),
        ],
    )
    w_cfg = CensorConfig(trigger_words=["wasps"], safety_before_ms=150, safety_after_ms=150)
    w_wins = calculate_merged_mute_intervals(scan_subtitles_for_triggers([w_cue], w_cfg), 150, 150)
    assert len(w_wins) == 1
    assert abs(w_wins[0][0] - 32.65) < 1e-3
    assert abs(w_wins[0][1] - 33.65) < 1e-3


def test_srt_and_vtt_sidecar(tmp_path):
    raw_vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.500\n<b>first</b> line\n\n2\n00:00:04.000 --> 00:00:06.000\nsecond line\n"
    parsed = parse_srt_content(raw_vtt)
    assert len(parsed) == 2
    assert parsed[0].text == "first line"

    vid = tmp_path / "clip.mkv"
    vid.write_bytes(b"\x00" * 32)
    (tmp_path / "clip.en.srt").write_text(serialize_srt(parsed), encoding="utf-8")

    entries, src, is_bmp, codec = extract_subtitles_from_video(vid)
    assert len(entries) == 2
    assert src == "External (clip.en.srt)"
    assert not is_bmp
    assert codec == ""


def test_profiles_io(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles_mod, "PROFILES_FILE", tmp_path / "profiles.json")
    assert "Medical & Illness Triggers" in profiles_mod.load_all_profiles()

    cfg = CensorConfig(trigger_words=["wasp", "hornet"], output_suffix=" filtered")
    assert profiles_mod.save_user_profile("Insects", cfg)
    assert profiles_mod.load_all_profiles()["Insects"]["trigger_words"] == ["wasp", "hornet"]

    assert profiles_mod.delete_user_profile("Insects")
    assert "Insects" not in profiles_mod.load_all_profiles()


def test_paths_and_copy(tmp_path):
    s_dir = tmp_path / "TV" / "Drama" / "Season 1"
    s_dir.mkdir(parents=True)
    ep = s_dir / "S01E01.mkv"
    ep.write_bytes(b"data" * 128)

    mirror = get_default_season_mirror_dir(s_dir)
    assert mirror == tmp_path / "TV" / "Drama [Trigger-Clean]" / "Season 1"

    cfg = CensorConfig(output_mode="season_mirror")
    v_out, s_out = build_output_paths(ep, cfg)
    assert v_out == mirror / "S01E01.mkv"
    assert s_out == mirror / "S01E01.srt"

    cfg.output_mode = "inplace_replace"
    assert build_output_paths(ep, cfg)[0].name == ".tmp_S01E01.mkv"

    cfg.output_mode = "dual_audio"
    cfg.output_suffix = ""
    assert build_output_paths(ep, cfg)[0].name == "S01E01 dual.mkv"

    cfg.output_mode = "separate_suffix"
    cfg.output_suffix = " trigger"
    assert build_output_paths(ep, cfg)[0].name == "S01E01 trigger.mkv"

    assert get_container_subtitle_codec(".mkv") == "srt"
    assert get_container_subtitle_codec(".mp4") == "mov_text"
    assert get_container_subtitle_codec(".avi") is None

    dst = tmp_path / "out.mkv"
    assert safe_copy_file(ep, dst)
    assert dst.read_bytes() == ep.read_bytes()

    aborted = tmp_path / "aborted.mkv"
    assert not safe_copy_file(ep, aborted, cancel_check=lambda: True, chunk_size=16)
    assert not aborted.exists()


def test_sleep_inhibitor():
    from subduck.power_manager import SleepInhibitor, _active_inhibitors, _kill_stale_subduck_inhibitors

    inhibitor = SleepInhibitor()
    assert not inhibitor.is_active

    inhibitor.acquire("Test reason")
    assert inhibitor.is_active
    assert inhibitor in _active_inhibitors

    inhibitor.release()
    assert not inhibitor.is_active
    assert inhibitor not in _active_inhibitors

    with SleepInhibitor() as inh:
        assert inh.is_active
    assert not inh.is_active

    _kill_stale_subduck_inhibitors()

