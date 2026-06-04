"""Hardware-free tests for multi-frame rendering and the animation driver.

The renderer half builds real (tiny) animated GIFs on disk and checks they expand
into the expected frame sequence. The driver half exercises ``Animator._step``
directly with an injected clock, so frame advancement, looping, the page-change
race guard, and disconnect handling are all tested without real time or threads.
"""

import pytest

from PIL import Image as PILImage
from StreamDeck.Transport.Transport import TransportError

from streamdeck_runner.animation import Animator, Clip
from streamdeck_runner.config import Animation, Button, Defaults
from streamdeck_runner.renderer import Frame, KeyRenderer


# --- fakes ----------------------------------------------------------------

class FakeClock:
    """A monotonic clock whose value the test advances by hand."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeDeck:
    """Just enough deck for the renderer and the animator."""

    def __init__(self) -> None:
        self.images: dict[int, bytes] = {}

    def key_image_format(self):
        return {"size": (72, 72), "format": "JPEG", "flip": (True, True), "rotation": 0}

    def set_key_image(self, key, image) -> None:
        self.images[key] = image

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def write_gif(path, count: int = 3, durations=(100, 200, 300)) -> str:
    colors = [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)]
    frames = [PILImage.new("RGBA", (32, 32), colors[i % len(colors)]) for i in range(count)]
    frames[0].save(
        path, save_all=True, append_images=frames[1:], duration=list(durations), loop=0
    )
    return str(path)


# --- renderer: render_frames ---------------------------------------------

def test_render_frames_static_image_is_single_frame(tmp_path):
    png = tmp_path / "static.png"
    PILImage.new("RGBA", (32, 32), (10, 20, 30, 255)).save(png)
    frames = KeyRenderer(Defaults()).render_frames(FakeDeck(), Button(key=0, image=str(png)))
    assert len(frames) == 1
    assert isinstance(frames[0].image, (bytes, bytearray))


def test_render_frames_missing_image_degrades_to_one_frame():
    frames = KeyRenderer(Defaults()).render_frames(
        FakeDeck(), Button(key=0, image="/no/such/file.gif", label="Hi")
    )
    assert len(frames) == 1  # blank/label-only fallback, no crash


def test_render_frames_animated_gif_uses_embedded_durations(tmp_path):
    gif = write_gif(tmp_path / "spin.gif", count=3, durations=(100, 200, 300))
    frames = KeyRenderer(Defaults()).render_frames(FakeDeck(), Button(key=0, image=gif))
    assert len(frames) == 3
    assert [f.duration for f in frames] == pytest.approx([0.1, 0.2, 0.3])


def test_render_frames_fps_overrides_embedded_durations(tmp_path):
    gif = write_gif(tmp_path / "spin.gif", count=3)
    button = Button(key=0, image=gif, animation=Animation(fps=20))
    frames = KeyRenderer(Defaults()).render_frames(FakeDeck(), button)
    assert [f.duration for f in frames] == pytest.approx([0.05, 0.05, 0.05])


def test_render_frames_animate_false_freezes_to_one_frame(tmp_path):
    gif = write_gif(tmp_path / "spin.gif", count=3)
    frames = KeyRenderer(Defaults()).render_frames(
        FakeDeck(), Button(key=0, image=gif, animate=False)
    )
    assert len(frames) == 1


# --- animator: _step ------------------------------------------------------

def make_animator(deck, clips, page="main", on_disconnect=lambda: None, clock=None):
    clock = clock or FakeClock()
    return Animator(deck, clips, lambda: page, on_disconnect, clock=clock), clock


def test_animator_advances_and_loops():
    # Whole-second frame durations stay binary-exact so the injected clock can hit
    # each deadline precisely without floating-point drift.
    clip = Clip([Frame(b"f0", 1.0), Frame(b"f1", 1.0), Frame(b"f2", 1.0)], loop=True)
    deck = FakeDeck()
    anim, clock = make_animator(deck, {("main", 2): clip})
    states = anim._build_states("main")

    anim._step(states, "main")           # frame 0 not yet due (due = 1.0)
    assert deck.images == {}

    clock.t = 1.0
    anim._step(states, "main")
    assert deck.images[2] == b"f1"

    clock.t = 2.0
    anim._step(states, "main")
    assert deck.images[2] == b"f2"

    clock.t = 3.0
    anim._step(states, "main")
    assert deck.images[2] == b"f0"       # wrapped back to the start


def test_animator_parks_on_last_frame_when_not_looping():
    clip = Clip([Frame(b"f0", 1.0), Frame(b"f1", 1.0)], loop=False)
    deck = FakeDeck()
    anim, clock = make_animator(deck, {("main", 0): clip})
    states = anim._build_states("main")

    clock.t = 1.0
    anim._step(states, "main")
    assert deck.images[0] == b"f1"

    clock.t = 10.0
    timeout = anim._step(states, "main")
    assert deck.images[0] == b"f1"       # stayed on the final frame
    assert timeout == anim._max_idle     # nothing more is ever due


def test_animator_skips_write_when_page_changed_mid_step():
    clip = Clip([Frame(b"f0", 1.0), Frame(b"f1", 1.0)], loop=True)
    deck = FakeDeck()
    current = {"page": "main"}
    anim = Animator(deck, {("main", 0): clip}, lambda: current["page"], lambda: None,
                    clock=(clock := FakeClock()))
    states = anim._build_states("main")

    clock.t = 1.0
    current["page"] = "apps"             # page switched while the frame was due
    anim._step(states, "main")
    assert deck.images == {}             # stale frame is not painted over the new page


def test_animator_reports_disconnect():
    class BoomDeck(FakeDeck):
        def set_key_image(self, key, image):
            raise TransportError("gone")

    seen = []
    clip = Clip([Frame(b"f0", 1.0), Frame(b"f1", 1.0)], loop=True)
    anim, clock = make_animator(BoomDeck(), {("main", 0): clip}, on_disconnect=lambda: seen.append(1))
    states = anim._build_states("main")

    clock.t = 1.0
    anim._step(states, "main")
    assert seen == [1]
    assert anim._stop.is_set()


def test_build_states_only_includes_current_page():
    clips = {
        ("main", 0): Clip([Frame(b"a", 0.1), Frame(b"b", 0.1)], loop=True),
        ("apps", 1): Clip([Frame(b"c", 0.1), Frame(b"d", 0.1)], loop=True),
    }
    anim, _ = make_animator(FakeDeck(), clips)
    assert set(anim._build_states("main")) == {0}
    assert set(anim._build_states("apps")) == {1}
