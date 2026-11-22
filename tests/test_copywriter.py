from streamclipper.copywriter import copy_for, keywords, make_hashtags, make_title, title_case


def _words(text: str) -> list:
    return [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.3} for i, w in enumerate(text.split())]


def test_title_case_and_length():
    assert title_case("HE DID NOT SEE THAT COMING") == "He Did Not See That Coming"
    assert title_case("WAIT FOR THE END") == "Wait for the End"
    assert title_case("I CAN'T STOP LAUGHING") == "I Can't Stop Laughing"
    long = make_title("THIS IS A VERY LONG HOOK THAT KEEPS GOING AND GOING AND GOING FOREVER AND EVER", [], set())
    assert len(long) <= 60 and not long.endswith(" ")


def test_keywords_skip_filler_and_profanity():
    kws = keywords(_words("bro the cameras broke bro the cameras are gone fuck chat junior junior junior"))
    assert kws == ["junior", "cameras", "broke"]


def test_titles_are_unique():
    taken = set()
    a = make_title("HE DID NOT SEE THAT COMING", ["cameras"], taken)
    taken.add(a.lower())
    b = make_title("HE DID NOT SEE THAT COMING", ["cameras"], taken)
    taken.add(b.lower())
    c = make_title("HE DID NOT SEE THAT COMING", [], taken, at=953.0)
    assert len({a, b, c}) == 3
    assert b == "He Did Not See That Coming: Cameras"
    assert c == "He Did Not See That Coming at 15:53"


def test_gta_tag_only_when_mentioned():
    assert "#GTA6" not in make_hashtags("funny", ["cameras"], "the cameras broke", "Black Myth Wukong part 1")
    assert "#GTA6" in make_hashtags("funny", [], "we are in gta now", "")
    assert "#GTA6" in make_hashtags("chaos", [], "", "Streamer plays GTA 6 for the first time")
    tags = make_hashtags("clutch", ["junior", "cameras"], "", "")
    assert tags[:2] == ["#clutch", "#gamingclips"] and "#junior" in tags and tags[-2:] == ["#shorts", "#gaming"]


def test_copy_for_fills_everything_in_plain_english():
    clip = {
        "hook_headline": "HIS CAMERAS BROKE, BRO NO",
        "category": "rage",
        "start": 1757.0,
        "words": _words("my cameras are broken bro the cameras broke no no no"),
    }
    got = copy_for(clip, set())
    assert got["title"] == "His Cameras Broke, Bro No"
    assert got["description"] == "A rage moment from the stream about cameras, broken and broke."
    assert got["hashtags"][0] == "#rage" and "#cameras" in got["hashtags"]
    assert all(t.startswith("#") for t in got["hashtags"]) and len(got["hashtags"]) <= 8
