// Phase 2A/2B/2C GPU dual-video compositor -- combined transition shader.
//
// One shader, selected transition behaviour via the `transitionType`
// uniform (0=Cross Dissolve, 1=Push, 2=Wipe, 3=Zoom, 4=RGB Glitch,
// 5=Pixel Dissolve, 6=Luma Dissolve, 7=Film Burn, 8=Zoom Blur,
// 9=Diagonal Wipe) rather than several separate shader files or runtime
// fragmentShader swapping -- keeps the QML/Python plumbing simple (a
// handful of plain-float/vec2 properties set once per commit) and avoids
// the RHI pipeline-recreation stall that switching `fragmentShader` at
// runtime would risk. Ten branches in one well-commented, self-contained-
// function-per-effect file was judged more maintainable than ten small
// files repeating the same uniform block/sampler boilerplate -- revisit
// this choice only if a future effect genuinely needs its own uniform
// shape this one can't express cleanly. See CODEX_HANDOFF.md's "Phase 2B"
// and "Phase 2C" sections for the full rationale and per-effect design
// notes.
//
// `progress` always means 0.0 = fully the outgoing (currently-primary)
// video, 1.0 = fully the incoming (secondary/promoted) video -- the QML
// NumberAnimation driving it always runs a fixed 0.0 -> 1.0 regardless of
// which physical deck (source1/source2) happens to be primary this time;
// `primaryIsFirst` (set once per commit, see video_subprocess.py's
// _commit_dual_transition) tells this shader which physical texture is
// currently the outgoing one, so directional effects have an unambiguous
// "A"/"B" regardless of deck-swap bookkeeping.
//
// Every effect function below is individually responsible for converging
// to *exactly* sampleOutgoing(uv) at progress=0.0 and *exactly*
// sampleIncoming(uv) at progress=1.0, with zero residual glitch/blur/
// brightness/mask contribution at either endpoint -- this is a hard
// requirement (Phase 2C spec), not just a visual nicety, and each
// function's comment explains why its own boundary math actually holds
// (most use an envelope term, e.g. sin(progress*PI) or a threshold swept
// well outside [0,1], that is provably exactly zero/one at the endpoints
// regardless of any other per-pixel randomness).
//
// `seed` is generated once per committed transition (see
// video_dual_transition.py's DualDeckController.generate_transition_seed,
// called alongside effect selection in try_commit()) and passed through
// unchanged for the whole transition -- deliberately not re-randomised
// per frame, so effects using it (RGB Glitch, Pixel Dissolve, Luma
// Dissolve, Film Burn) stay temporally stable rather than shimmering.
// `resolution` is a live QML binding (blend's own width/height, which
// track the embedded window's real size including resizes) rather than
// anything Python ever sets directly -- used only by Pixel Dissolve to
// keep block size visually consistent across different output sizes.
//
// This is the GLSL *source*. It is NOT loaded at runtime -- Qt Quick's
// RHI-based ShaderEffect requires a pre-compiled .qsb (Qt Shader Baker)
// file, produced once via:
//
//   qsb --qt6 -o video_dual_deck_blend.frag.qsb video_dual_deck_blend.frag
//
// Re-run this whenever the shader source changes; commit the resulting
// .qsb alongside this file.
#version 440

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float progress;        // 0.0 = fully outgoing, 1.0 = fully incoming
    float transitionType;  // 0..9, see file header
    float direction;       // +1.0 / -1.0 -- push, wipe & diagonal wipe only
    float primaryIsFirst;  // >0.5 if source1 is the outgoing video, else source2 is
    float seed;            // fixed for the whole transition; 0.0 if unused by the effect
    vec2 resolution;       // live output size in pixels; Pixel Dissolve only
    // Zoom Blur only: each source's actual on-screen video-content rect
    // (xy = min UV, zw = max UV), excluding VideoOutput's own letterbox/
    // pillarbox padding -- see video_dual_deck.qml's matching property
    // comment for how these are computed and why Zoom Blur specifically
    // needs them (its taps can be displaced into padding that every other
    // effect never touches, since they only ever sample a pixel's own
    // unmoved coordinate).
    vec4 content1Rect;
    vec4 content2Rect;
};

layout(binding = 1) uniform sampler2D source1;
layout(binding = 2) uniform sampler2D source2;

vec4 sampleOutgoing(vec2 uv) {
    return primaryIsFirst > 0.5 ? texture(source1, uv) : texture(source2, uv);
}

vec4 sampleIncoming(vec2 uv) {
    return primaryIsFirst > 0.5 ? texture(source2, uv) : texture(source1, uv);
}

// Zoom Blur only (see this file's uniform block comment on content1Rect/
// content2Rect for why): clamps a UV into whichever physical source's own
// real video-content rect is currently the outgoing/incoming one, so a
// displaced blur tap can never land in VideoOutput's letterbox/pillarbox
// padding -- that padding is not guaranteed opaque (VideoOutput leaves it
// untouched rather than painting a solid backdrop), so sampling it exposes
// whatever sits behind the compositor instead of real picture content.
vec2 clampToOutgoingContent(vec2 uv) {
    vec4 rect = primaryIsFirst > 0.5 ? content1Rect : content2Rect;
    return clamp(uv, rect.xy, rect.zw);
}

vec2 clampToIncomingContent(vec2 uv) {
    vec4 rect = primaryIsFirst > 0.5 ? content2Rect : content1Rect;
    return clamp(uv, rect.xy, rect.zw);
}

// -- shared procedural-randomness helpers (Phase 2C) -------------------
// Cheap, deterministic hash/noise -- no external texture, no per-frame
// Python involvement. Standard shader-toy-style hash; not cryptographic,
// just needs to look unstructured and be stable for a given input.
float hash11(float n) {
    return fract(sin(n) * 43758.5453123);
}

float hash21(vec2 p) {
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453123);
}

// Single-octave value noise (bilinear-interpolated hash grid) -- much
// cheaper than Perlin/simplex, organic/blobby rather than salt-and-pepper
// static, which is exactly what distinguishes Luma Dissolve from the
// deliberately blocky Pixel Dissolve.
float valueNoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    float a = hash21(i);
    float b = hash21(i + vec2(1.0, 0.0));
    float c = hash21(i + vec2(0.0, 1.0));
    float d = hash21(i + vec2(1.0, 1.0));
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

// Known-good reference implementation -- must keep behaving exactly as it
// always has: progress 0.0 = pure outgoing, 0.5 = a genuine blend of both,
// 1.0 = pure incoming.
vec4 crossDissolve(vec2 uv) {
    return mix(sampleOutgoing(uv), sampleIncoming(uv), progress);
}

// Both videos visible simultaneously, sliding along a conveyor belt: the
// outgoing video translates fully off-screen in `direction`, the incoming
// video slides in from the opposite edge. Never samples outside [0,1] on
// either texture (each branch's coordinate is proven in-range by
// construction), so this never wraps or smears an edge pixel.
vec4 push(vec2 uv) {
    float stripPos = uv.x + progress * direction;
    if (direction >= 0.0) {
        if (stripPos < 1.0) {
            return sampleOutgoing(vec2(stripPos, uv.y));
        }
        return sampleIncoming(vec2(stripPos - 1.0, uv.y));
    }
    if (stripPos >= 0.0) {
        return sampleOutgoing(vec2(stripPos, uv.y));
    }
    return sampleIncoming(vec2(stripPos + 1.0, uv.y));
}

// The outgoing video stays spatially stationary; a hard boundary sweeps
// across the screen revealing the incoming video underneath. Both videos
// are sampled at their own unmodified coordinates -- nothing is
// translated, unlike push.
vec4 wipe(vec2 uv) {
    bool revealed = direction >= 0.0 ? (uv.x < progress) : (uv.x > (1.0 - progress));
    return revealed ? sampleIncoming(uv) : sampleOutgoing(uv);
}

// Tasteful, not extreme: the outgoing video zooms in slightly while
// fading out (scale 1.0 -> 1.12, always sampled in-range, no bounds check
// needed), the incoming video grows from slightly-shrunk to normal size
// while fading in (scale 0.92 -> 1.0). Growing "into frame" from smaller
// than the viewport does sample outside [0,1] near the start of the
// transition -- explicitly filled with opaque black (the same background
// treatment as ordinary video letterboxing) rather than left to the
// sampler's clamp-to-edge behaviour, which would smear.
vec4 zoom(vec2 uv) {
    vec2 center = vec2(0.5, 0.5);
    float scaleOut = 1.0 + progress * 0.12;
    float scaleIn = 0.92 + progress * 0.08;
    vec2 uvOut = center + (uv - center) / scaleOut;
    vec2 uvIn = center + (uv - center) / scaleIn;
    vec4 colorOut = sampleOutgoing(uvOut);
    vec4 colorIn;
    if (uvIn.x >= 0.0 && uvIn.x <= 1.0 && uvIn.y >= 0.0 && uvIn.y <= 1.0) {
        colorIn = sampleIncoming(uvIn);
    } else {
        colorIn = vec4(0.0, 0.0, 0.0, 1.0);
    }
    return mix(colorOut, colorIn, progress);
}

// -- Phase 2C ------------------------------------------------------------

// Deliberate music-video digital-glitch look, not corrupted playback:
// the screen is divided into horizontal bands; each band independently
// "flips" from A to B once `progress` crosses that band's own random
// threshold (seeded once per transition), producing alternating A/B
// slices mid-transition while still resolving to all-A at progress=0 and
// all-B at progress=1 for every band (thresholds are always in [0,1), so
// progress=0 can never exceed one and progress=1 always exceeds every
// one). Horizontal displacement and RGB channel separation are both
// scaled by `intensity = sin(progress*pi)`, an envelope that is exactly
// zero at both progress=0 and progress=1 regardless of anything else in
// the expression -- this is what guarantees no residual offset/
// separation survives to the final frame.
vec4 rgbGlitch(vec2 uv) {
    float intensity = sin(clamp(progress, 0.0, 1.0) * 3.14159265);
    float numBands = 24.0;
    float bandIndex = floor(uv.y * numBands);
    float bandSeedBase = bandIndex + seed * 97.0;
    float bandDisplace = hash11(bandSeedBase) - 0.5;
    float bandThreshold = hash11(bandSeedBase + 53.7);
    bool showB = progress > bandThreshold;
    float xOffset = bandDisplace * 0.06 * intensity;
    vec2 displacedUV = vec2(uv.x + xOffset, uv.y);
    float chroma = 0.01 * intensity;
    vec2 uvR = clamp(vec2(displacedUV.x + chroma, displacedUV.y), vec2(0.0), vec2(1.0));
    vec2 uvG = clamp(displacedUV, vec2(0.0), vec2(1.0));
    vec2 uvB = clamp(vec2(displacedUV.x - chroma, displacedUV.y), vec2(0.0), vec2(1.0));
    vec4 colorR = showB ? sampleIncoming(uvR) : sampleOutgoing(uvR);
    vec4 colorG = showB ? sampleIncoming(uvG) : sampleOutgoing(uvG);
    vec4 colorB = showB ? sampleIncoming(uvB) : sampleOutgoing(uvB);
    return vec4(colorR.r, colorG.g, colorB.b, 1.0);
}

// GPU-computed blocks (no per-pixel QML/Python objects): each block gets
// one stable random value; a block shows B once `progress` exceeds that
// value. Block size is defined in pixels and divided by the live
// `resolution` uniform so the effect reads as the same block density
// regardless of actual output size. random() is always in [0,1), so
// progress=0 shows all-A and progress=1 shows all-B for every block.
vec4 pixelDissolve(vec2 uv) {
    float blockSizePixels = 40.0;
    vec2 safeResolution = max(resolution, vec2(1.0));
    vec2 blockCoord = floor(uv * safeResolution / blockSizePixels);
    float r = hash21(blockCoord + vec2(seed * 41.0, seed * 17.0));
    return r < progress ? sampleIncoming(uv) : sampleOutgoing(uv);
}

// Smoother, organic alternative to Pixel Dissolve using single-octave
// value noise instead of hard blocks, with a soft threshold band so the
// boundary isn't a harsh edge. The reveal threshold is swept from below
// 0.0 to above 1.0 as progress goes 0->1 (not just 0->1 itself) --
// otherwise the soft band's own width would let a few low/high-noise
// pixels show a partial blend exactly at progress=0 or 1.0, which would
// violate the "must be exactly A/B at the boundaries" requirement. With
// the extended sweep, at progress=0 every possible noise value is above
// the band's upper edge (pure A everywhere) and at progress=1 every
// value is below the band's lower edge (pure B everywhere).
vec4 lumaDissolve(vec2 uv) {
    float n = valueNoise(uv * 6.0 + vec2(seed * 19.0, seed * 7.0));
    float softness = 0.15;
    float threshold = mix(-softness, 1.0 + softness, progress);
    float mixAmount = 1.0 - smoothstep(threshold - softness, threshold + softness, n);
    return mix(sampleOutgoing(uv), sampleIncoming(uv), mixAmount);
}

// A warm light-leak enters from a corner chosen once per transition (via
// seed), brightens, obscures A, and near its peak the underlying image
// itself swaps toward B; brightness then recedes to reveal a clean B.
// `envelope = sin(progress*pi)` is exactly zero at both progress=0 and
// progress=1 (a single smooth peak around progress=0.5 in between, never
// repeating -- satisfies "brief peak, no rapid flashing"), and the leak
// mask is *multiplied* by envelope (not just shaped by it through a
// division), so it is forced to exactly zero at both endpoints regardless
// of the distance-based falloff term. The underlying image swap
// (baseMix) is a smoothstep purely of progress with no randomness, so it
// too is exactly A at progress=0 and exactly B at progress=1.
vec4 filmBurn(vec2 uv) {
    vec2 leakOrigin = vec2(
        seed > 0.5 ? 1.0 : 0.0,
        fract(seed * 3.7) > 0.5 ? 1.0 : 0.0
    );
    float dist = distance(uv, leakOrigin);
    float edgeNoise = valueNoise(uv * 5.0 + seed * 23.0) * 0.25;
    float envelope = sin(clamp(progress, 0.0, 1.0) * 3.14159265);
    float leakRadius = max(envelope * 1.3, 0.001);
    float leakFalloff = clamp(1.0 - (dist - edgeNoise) / leakRadius, 0.0, 1.0);
    float leakMask = leakFalloff * envelope;
    float baseMix = smoothstep(0.35, 0.65, progress);
    vec4 base = mix(sampleOutgoing(uv), sampleIncoming(uv), baseMix);
    vec3 warmColor = vec3(1.0, 0.72, 0.35);
    return vec4(base.rgb + warmColor * leakMask * 0.9, 1.0);
}

// Distinct from the plain Zoom effect above: outgoing A mildly scales up
// while radially smearing outward (motion-blur approximation via a
// modest 5-tap average along the line toward centre, not a real
// multi-pass Gaussian -- "attractive blur" over "technically perfect"
// per the brief), incoming B starts blurred+slightly zoomed and rapidly
// sharpens to normal. Both blur amounts are only ever *read* through
// mix(colorOut, colorIn, progress) -- at progress=0 that mix always
// returns colorOut exactly (colorIn's value is irrelevant, its weight is
// zero), and at progress=0 colorOut's own blur amount is also exactly
// zero (all 5 taps collapse to the same clamped point), so the result is
// provably exactly sampleOutgoing(clampToOutgoingContent(uv)) -- which
// equals sampleOutgoing(uv) for every on-screen video-content pixel (the
// clamp is a no-op there) and is the nearest real-content edge pixel,
// never raw letterbox/pillarbox padding, for the rest. The symmetric
// argument holds at progress=1 for colorIn.
vec4 zoomBlur(vec2 uv) {
    vec2 center = vec2(0.5, 0.5);
    vec2 toCenter = center - uv;

    // Fixed three times after real-world visual testing:
    //
    // 1. (First pass) Visible discrete radial "spoke" ghosting collapsing
    //    toward black around the midpoint, instead of a smooth motion
    //    smear -- caused by the blur *distance* being too large for only
    //    5 taps. Reduced the blur distance ~4x.
    // 2. (Second pass, mistaken) Believed the residual black was caused by
    //    non-opaque alpha in sampled taps (e.g. VideoOutput letterbox
    //    padding) and by looping constructs interacting badly with dynamic
    //    texture sampling, and "fixed" both by unrolling the loop and
    //    force-writing every tap's alpha to 1.0 via `vec4(tap.rgb, 1.0)`.
    //    Manual visual testing showed this did NOT fix the real symptom.
    // 3. (Third pass, actual root cause) A focused, non-paused bisection
    //    harness (grabWindow() screenshots captured while the transition
    //    played normally, never pausing the MediaPlayers -- pausing was
    //    tried first and turned out to be its own confound, since a
    //    paused MediaPlayer's VideoOutput can stop supplying a valid
    //    texture) isolated the true cause with certainty: on this
    //    Direct3D11 RHI backend, taking a value that came from a
    //    sampleOutgoing()/sampleIncoming() texture() call and then
    //    *modifying its alpha component* -- whether via
    //    `vec4(tap.rgb, 1.0)` reconstruction, a `tap.a = 1.0` field write,
    //    or even `tap += vec4(0,0,0,1.0-tap.a)` -- reliably drove the
    //    ENTIRE fragment output to solid opaque black, uniformly across
    //    the whole frame, not just at letterbox edges. Returning the exact
    //    same sampled vec4 completely unmodified (alpha included) rendered
    //    correctly every time, and a hardcoded, non-textured
    //    vec4(1.0, 0.0, 0.0, 1.0) also rendered correctly (confirming the
    //    defect is specific to post-hoc alpha edits on a texture-sampled
    //    value in this branch, not a general rendering/dispatch/deck
    //    problem). The fix is simply to never touch the alpha channel of
    //    a sampled tap at all: weighted-average the taps as full vec4s
    //    (RGB *and* A together) and let the un-tampered alpha pass
    //    straight through, exactly like every other effect in this file
    //    already does via sampleOutgoing()/sampleIncoming() directly.
    // 4. (Fourth pass, actual remaining cause) Pass 3 fixed a genuine
    //    catastrophic full-frame black bug, but real-world manual testing
    //    on aspect-mismatched video (VideoOutput's default PreserveAspectFit
    //    letterboxes/pillarboxes when a video's aspect ratio doesn't match
    //    the output) still showed the app's own background exposed around
    //    the transition midpoint -- because VideoOutput does not paint an
    //    opaque backdrop behind its letterbox/pillarbox padding, so that
    //    padding is not real picture content and is not guaranteed opaque.
    //    A temporary diagnostic (mix(A,B,progress) reached via this exact
    //    same deck/timing/content-rect path) exposed the *identical*
    //    padding under a deliberately squished window aspect ratio,
    //    proving this was never specific to zoomBlur()'s own coordinate
    //    offsets -- every effect samples raw, unclamped UVs, so any of them
    //    would show it for a pixel that already sits in padding. What makes
    //    Zoom Blur different is that it is the one effect whose multi-tap
    //    averaging blends *real content together with* nearby padding,
    //    smearing a thin, otherwise easy-to-miss padding band into a much
    //    more visually prominent artefact. Fixed by clamping every tap
    //    (including the zero-offset one) into the actual on-screen
    //    video-content rect -- content1Rect/content2Rect, computed from
    //    each VideoOutput's own `contentRect` in video_dual_deck.qml --
    //    instead of the generic [0,1] item-space clamp, via
    //    clampToOutgoingContent()/clampToIncomingContent() below. Scoped to
    //    this effect only, per instruction; the same padding-transparency
    //    is technically reachable by other effects too, just far less
    //    visible without multi-tap blending.
    //
    // Samples are weighted toward the centre (least-displaced, offset 0)
    // tap rather than averaged uniformly -- a heavily-displaced outer tap
    // that does still happen to land on padding contributes less to the
    // final blend. Weights (1.0, 0.85, 0.7, 0.55, 0.4) and their sum
    // (3.5) are the fixed values that formula produces for 5 evenly
    // spaced taps -- computed once here rather than accumulated at
    // runtime, since there is no loop left to accumulate them in.
    float outBlurAmount = progress * 0.03;
    vec2 outUV0 = clampToOutgoingContent(uv);
    vec2 outUV1 = clampToOutgoingContent(uv + toCenter * outBlurAmount * 0.25);
    vec2 outUV2 = clampToOutgoingContent(uv + toCenter * outBlurAmount * 0.5);
    vec2 outUV3 = clampToOutgoingContent(uv + toCenter * outBlurAmount * 0.75);
    vec2 outUV4 = clampToOutgoingContent(uv + toCenter * outBlurAmount);
    vec4 colorOut =
        sampleOutgoing(outUV0) * 1.0 +
        sampleOutgoing(outUV1) * 0.85 +
        sampleOutgoing(outUV2) * 0.7 +
        sampleOutgoing(outUV3) * 0.55 +
        sampleOutgoing(outUV4) * 0.4;
    colorOut /= 3.5;

    float inBlurAmount = (1.0 - progress) * (1.0 - progress) * 0.045;
    float inScale = 1.0 + (1.0 - progress) * 0.10;
    vec2 zoomedUV = center + (uv - center) / inScale;
    vec2 inUV0 = clampToIncomingContent(zoomedUV);
    vec2 inUV1 = clampToIncomingContent(zoomedUV + toCenter * inBlurAmount * 0.25);
    vec2 inUV2 = clampToIncomingContent(zoomedUV + toCenter * inBlurAmount * 0.5);
    vec2 inUV3 = clampToIncomingContent(zoomedUV + toCenter * inBlurAmount * 0.75);
    vec2 inUV4 = clampToIncomingContent(zoomedUV + toCenter * inBlurAmount);
    vec4 colorIn =
        sampleIncoming(inUV0) * 1.0 +
        sampleIncoming(inUV1) * 0.85 +
        sampleIncoming(inUV2) * 0.7 +
        sampleIncoming(inUV3) * 0.55 +
        sampleIncoming(inUV4) * 0.4;
    colorIn /= 3.5;

    return mix(colorOut, colorIn, progress);
}

// A polished top-left -> bottom-right diagonal wipe (direction >= 0.0;
// the sign is already exposed via the existing `direction` uniform, so a
// reverse bottom-right -> top-left direction comes essentially free, but
// only one direction is currently wired up to a Preferences choice -- see
// video_subprocess.py). `diag` is 0.0 at the top-left corner and 1.0 at
// bottom-right; like Luma Dissolve, the reveal edge is swept from below
// 0.0 to above 1.0 (not just 0->1) so the feathered boundary can never
// leave a partial reveal visible exactly at either endpoint. A thin
// highlight glow hugs the moving boundary; it is itself scaled by the
// same sin(progress*pi) envelope used elsewhere, so it cannot linger as a
// visible artefact after the wipe completes.
vec4 diagonalWipe(vec2 uv) {
    float diag = direction >= 0.0
        ? (uv.x + (1.0 - uv.y)) * 0.5
        : 1.0 - (uv.x + (1.0 - uv.y)) * 0.5;
    float feather = 0.04;
    float edge = mix(-feather, 1.0 + feather, progress);
    float revealed = 1.0 - smoothstep(edge - feather, edge + feather, diag);
    vec4 result = mix(sampleOutgoing(uv), sampleIncoming(uv), revealed);
    float glowDist = abs(diag - progress);
    float glow = (1.0 - smoothstep(0.0, 0.02, glowDist))
        * sin(clamp(progress, 0.0, 1.0) * 3.14159265) * 0.35;
    return result + vec4(glow, glow, glow, 0.0);
}

void main() {
    vec4 result;
    if (transitionType < 0.5) {
        result = crossDissolve(qt_TexCoord0);
    } else if (transitionType < 1.5) {
        result = push(qt_TexCoord0);
    } else if (transitionType < 2.5) {
        result = wipe(qt_TexCoord0);
    } else if (transitionType < 3.5) {
        result = zoom(qt_TexCoord0);
    } else if (transitionType < 4.5) {
        result = rgbGlitch(qt_TexCoord0);
    } else if (transitionType < 5.5) {
        result = pixelDissolve(qt_TexCoord0);
    } else if (transitionType < 6.5) {
        result = lumaDissolve(qt_TexCoord0);
    } else if (transitionType < 7.5) {
        result = filmBurn(qt_TexCoord0);
    } else if (transitionType < 8.5) {
        result = zoomBlur(qt_TexCoord0);
    } else {
        result = diagonalWipe(qt_TexCoord0);
    }
    fragColor = result * qt_Opacity;
}
