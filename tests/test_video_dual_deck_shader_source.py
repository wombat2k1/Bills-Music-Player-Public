"""Static regression coverage for a real visual bug found by manual testing
in the packaged build: Zoom Blur's outgoing/incoming videos collapsed to a
dark image (and, in earlier diagnoses, to solid black, then to an exposed
application background) around the transition midpoint instead of a smooth
motion smear.

Three earlier fix attempts (visible in git history / prior session notes)
addressed real but secondary or mistaken causes -- excessive blur distance,
a mistaken belief that sampled taps needed their alpha forced to 1.0, which
turned out to itself be the cause of a full-frame-black bug on the real
Direct3D11 RHI backend (any post-hoc write to a texture-sampled value's
alpha component reliably drove the whole fragment output to solid black) --
before the actual remaining cause was found: VideoOutput's default
PreserveAspectFit rendering leaves letterbox/pillarbox padding around
aspect-mismatched video unpainted (not guaranteed opaque), and Zoom Blur's
multi-tap averaging is the one effect in this file that blends real content
together with nearby padding, smearing an otherwise easy-to-miss padding
band into a prominent, app-background-exposing artefact. The fix clamps
every tap into each source's actual on-screen video-content rect
(`content1Rect`/`content2Rect`, computed from VideoOutput's own
`contentRect` in video_dual_deck.qml) instead of a generic [0,1] clamp.

Automated tests cannot execute GLSL or read back rendered pixels here
(this repo has previously and deliberately ruled out
QQuickWindow.grabWindow() for normal use -- see CODEX_HANDOFF.md; it was
used only as a temporary, removed-after-use diagnostic harness for this
investigation, and was itself found mid-investigation to not reliably
reflect live shader state across repeated captures within one process, a
limitation documented in CODEX_HANDOFF.md). These tests instead pin the
properties that were actually wrong, directly in the shader *source* text,
the same way test_video_preferences.py already statically inspects Python
source elsewhere in this project.

Final visual acceptance still requires a human watching real playback --
see this session's CODEX_HANDOFF.md entry for the manual verification
record."""
import os
import re

_FRAG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "assets", "video_dual_deck_blend.frag",
)
_QML_PATH = os.path.join(
    os.path.dirname(__file__), "..", "assets", "video_dual_deck.qml",
)


def _shader_source() -> str:
    with open(_FRAG_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _qml_source() -> str:
    with open(_QML_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _zoom_blur_function_body(source: str) -> str:
    start = source.index("vec4 zoomBlur(vec2 uv) {")
    # The next top-level "vec4 <name>(" after zoomBlur's own signature
    # marks the start of the following function.
    next_function = re.search(r"\nvec4 \w+\(vec2 uv\) \{", source[start + 1:])
    assert next_function is not None, "could not find the function after zoomBlur"
    return source[start:start + 1 + next_function.start()]


def _strip_line_comments(text: str) -> str:
    # Strips this file's plain "// ..." line comments so string checks
    # below only see actual GLSL code, not prose that happens to quote
    # the buggy pattern being guarded against.
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def test_zoom_blur_never_writes_a_sampled_taps_alpha_component():
    # The confirmed root cause of the full-frame-black bug: modifying the
    # alpha channel of a texture-sampled value in this function renders
    # solid black on the real GPU/driver, regardless of how the
    # modification is spelled. Guard against every spelling reappearing.
    body = _strip_line_comments(_zoom_blur_function_body(_shader_source()))
    assert ".rgb, 1.0)" not in body, (
        "reconstructing a sampled tap via vec4(x.rgb, 1.0) renders solid "
        "black on the real Direct3D11 RHI backend -- do not reintroduce it"
    )
    assert re.search(r"\.a\s*=", body) is None, (
        "writing to a sampled tap's .a component renders solid black on "
        "the real Direct3D11 RHI backend -- do not reintroduce it"
    )


def test_zoom_blur_accumulates_full_vec4_samples_untouched():
    # sampleOutgoing()/sampleIncoming() must be accumulated as complete,
    # unmodified vec4 values (RGB and alpha together) -- this is what
    # dodges the alpha-write bug above while still combining correctly.
    body = _zoom_blur_function_body(_shader_source())
    assert body.count("sampleOutgoing(outUV0)") == 1
    assert body.count("sampleOutgoing(outUV4)") == 1
    assert body.count("sampleIncoming(inUV0)") == 1
    assert body.count("sampleIncoming(inUV4)") == 1
    assert "colorOut /= 3.5" in body
    assert "colorIn /= 3.5" in body
    assert "return mix(colorOut, colorIn, progress);" in body


def test_zoom_blur_radial_displacement_is_bounded():
    # Regression ceiling for the original reported bug: the very first
    # values (progress*0.12 and (1-progress)^2*0.18) let the outermost of
    # only 5 taps land up to ~350px apart on a 1080p output -- far enough
    # that a cheap 5-tap "blur" reads as individually-visible discrete
    # copies (radial "spokes") rather than smooth smear. The fixed values
    # are 0.03 and 0.045; this ceiling is set well below the old,
    # confirmed-too-large values but with headroom above the current ones,
    # so a future tweak that overshoots back toward the original problem
    # gets caught without being so tight it breaks on a reasonable retune.
    source = _shader_source()
    out_match = re.search(
        r"outBlurAmount = progress \* ([0-9.]+);", source,
    )
    in_match = re.search(
        r"inBlurAmount = \(1\.0 - progress\) \* \(1\.0 - progress\) \* ([0-9.]+);",
        source,
    )
    assert out_match is not None, "could not find outBlurAmount's magnitude constant"
    assert in_match is not None, "could not find inBlurAmount's magnitude constant"
    out_amount = float(out_match.group(1))
    in_amount = float(in_match.group(1))
    assert 0.0 < out_amount <= 0.08, out_amount
    assert 0.0 < in_amount <= 0.08, in_amount


def test_zoom_blur_samples_are_weighted_toward_the_least_displaced_tap():
    # A uniform average lets a heavily-displaced outer tap (the one most
    # likely to have drifted into padding, and the biggest contributor to
    # visible ghosting) count exactly as much as the centre tap. Weighting
    # towards offset 0 keeps the result closer to genuine, undisplaced
    # picture content. Weights (1.0, 0.85, 0.7, 0.55, 0.4) sum to exactly
    # 3.5, matching the divisor asserted above.
    body = _zoom_blur_function_body(_shader_source())
    for weight in ("1.0", "0.85", "0.7", "0.55", "0.4"):
        assert body.count(f"* {weight}") >= 2, (
            f"expected weight {weight} to appear in both the outgoing and "
            "incoming accumulations"
        )


def test_zoom_blur_boundaries_collapse_to_exact_endpoints():
    # At progress=0, outUV0..outUV4 all collapse to
    # clampToOutgoingContent(uv) (outBlurAmount is exactly zero), so
    # colorOut is a weighted average of five identical samples -- exactly
    # sampleOutgoing(clampToOutgoingContent(uv)) since the weights sum to
    # 3.5. That equals sampleOutgoing(uv) for every on-screen video pixel
    # (the clamp is a no-op there); at progress=1, colorOut's weight in the
    # final mix() is zero, and the symmetric argument holds for colorIn.
    # This test pins the source-level structure that makes that proof hold
    # (rather than re-deriving the proof at runtime, which would need a
    # real GPU).
    body = _zoom_blur_function_body(_shader_source())
    assert "vec2 outUV0 = clampToOutgoingContent(uv);" in body
    assert "vec2 inUV0 = clampToIncomingContent(zoomedUV);" in body
    assert "float outBlurAmount = progress * " in body
    assert "float inBlurAmount = (1.0 - progress) * (1.0 - progress) * " in body
    assert "return mix(colorOut, colorIn, progress);" in body


def test_zoom_blur_taps_are_clamped_to_actual_video_content_rect():
    # The actual remaining-cause fix: every one of the 10 taps (5 outgoing,
    # 5 incoming) must be clamped into the real on-screen video-content
    # rect, not the generic full-item [0,1] range, so a displaced tap can
    # never land in VideoOutput's unpainted letterbox/pillarbox padding.
    body = _strip_line_comments(_zoom_blur_function_body(_shader_source()))
    assert body.count("clampToOutgoingContent(") == 5, (
        "expected all 5 outgoing taps to be clamped to the real "
        "video-content rect"
    )
    assert body.count("clampToIncomingContent(") == 5, (
        "expected all 5 incoming taps to be clamped to the real "
        "video-content rect"
    )
    # No remaining generic [0,1] item-space clamp() calls inside the
    # function -- every tap must go through the content-rect-aware helpers
    # above instead.
    assert "clamp(uv" not in body
    assert "vec2(0.0), vec2(1.0)" not in body


def test_content_rect_helpers_clamp_into_the_correct_physical_source():
    # clampToOutgoingContent()/clampToIncomingContent() must pick whichever
    # physical source (content1Rect vs content2Rect) is currently the
    # outgoing/incoming one, mirroring sampleOutgoing()/sampleIncoming()'s
    # own primaryIsFirst-based selection -- a hardcoded pick would clamp
    # against the wrong source's content rect whenever decks are swapped.
    source = _strip_line_comments(_shader_source())
    assert (
        "vec2 clampToOutgoingContent(vec2 uv) {\n"
        "    vec4 rect = primaryIsFirst > 0.5 ? content1Rect : content2Rect;\n"
        "    return clamp(uv, rect.xy, rect.zw);\n"
        "}"
    ) in source
    assert (
        "vec2 clampToIncomingContent(vec2 uv) {\n"
        "    vec4 rect = primaryIsFirst > 0.5 ? content2Rect : content1Rect;\n"
        "    return clamp(uv, rect.xy, rect.zw);\n"
        "}"
    ) in source


def test_content_rect_uniforms_are_declared_once():
    source = _shader_source()
    assert source.count("vec4 content1Rect;") == 1
    assert source.count("vec4 content2Rect;") == 1


def test_qml_computes_content_rects_from_video_output_content_rect_with_fallback():
    # Each contentRect must come from the real VideoOutput.contentRect
    # (Qt's own PreserveAspectFit-aware property), normalised into the
    # same 0..1 UV space qt_TexCoord0 uses, with a full-rect (no
    # restriction) fallback for the degenerate case where contentRect is
    # momentarily zero-sized (e.g. before a source has ever reported a
    # frame) -- without the fallback, every clamp() would collapse to a
    # single frozen corner pixel instead of leaving the tap unrestricted.
    qml = _qml_source()
    assert "videoOutput0.contentRect" in qml
    assert "videoOutput1.contentRect" in qml
    assert qml.count("Qt.vector4d(0.0, 0.0, 1.0, 1.0)") == 2, (
        "expected both content1Rect and content2Rect to fall back to the "
        "full 0..1 rect when contentRect is degenerate"
    )


def test_compositor_window_has_opaque_black_background():
    # Even with content-rect clamping, VideoOutput's letterbox/pillarbox
    # padding (and, it turns out, other transparent regions) is genuinely
    # unpainted at the RHI level -- confirmed directly by temporarily
    # setting this same window's background to an unmistakable diagnostic
    # colour (lime) and watching it appear exactly where the exposed
    # application background had been reported, via a single-shot
    # grabWindow() capture immune to the repeated-capture staleness issue
    # documented above. Every transparent ShaderEffect pixel, for every
    # effect (not just Zoom Blur), composites over this window's own
    # background -- Qt Quick's default is opaque white, not transparent,
    # so without an explicit override here the embedded native window
    # would let Bills Music Player's own Now Playing background/visualiser
    # show through instead. This is a presentation-layer fix: it changes
    # what sits *behind* the compositor, not any sampled video alpha.
    qml = _qml_source()
    assert 'color: "black"' in qml


def test_zoom_blur_dispatch_and_diagonal_wipe_are_undisturbed():
    # The investigation added and later removed a temporary
    # transitionType=10 diagnostic branch (and a matching QML easing
    # entry) -- confirm no trace of it remains and the dispatch chain ends
    # cleanly at Diagonal Wipe (transitionType 9, the last real effect).
    source = _shader_source()
    assert "Zoom Blur Diagnostic" not in source
    main_start = source.index("void main() {")
    main_body = source[main_start:]
    # 9 branches for the first 9 effects (Cross Dissolve..Zoom Blur), plus
    # a final unconditional else for Diagonal Wipe (transitionType 9) --
    # the last real effect, with no dangling transitionType=10 case.
    assert main_body.count("else if (transitionType <") == 8
    assert main_body.strip().endswith(
        "} else {\n        result = diagonalWipe(qt_TexCoord0);\n    }\n"
        "    fragColor = result * qt_Opacity;\n}"
    )
