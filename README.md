# Bills Music Player

## Public source through Phase 9

This sanitised source snapshot includes the accepted Phase 9 changes:
playback and queue identity safeguards, pause and recovery authority,
shutdown lifetime handling, a GUI stall watchdog, and CPU dual-deck frame
ownership fixes with regression tests. CPU dual-deck remains an experimental
test path; the application does not select it for normal playback.

BASS and FFmpeg binaries are not distributed in this repository. Obtain them
separately under their publishers' licences; see THIRD_PARTY_DEPENDENCIES.md.
Build scripts may consume locally supplied dependencies but those files and
build outputs must remain untracked. Private development handoff documents,
diagnostics, machine configuration and generated build identity are not synced.
References to CODEX_HANDOFF.md below describe private historical development
notes, which are intentionally not included in this public repository.

Source verification:

```text
python -m compileall billsmusic tests
python -m pytest tests
```

The tests require pytest and the application's Python dependencies. Some
integration tests also require native media libraries or display/audio support;
their skips and failures must be reviewed for the environment used.

---

# Bills Music Player — package skeleton

`Main.py` (3031 lines) has been split into a `billsmusic/` package. Behavior is
unchanged; the code is just reorganized into modules.

## Layout

    Main.py                  # thin launcher — `python Main.py` still works
    billsmusic/
      __init__.py
      config.py              # APP_TITLE, all tuning constants, cache/config paths
      net.py                 # http_get
      platform_utils.py      # resource_path, configure_vlc_env (PyInstaller/VLC)
      metadata.py            # Track dataclass, read_track_meta, tag_or,
                             #   parse_track_number, read_cover_bytes
      audio.py               # AudioAnalyzer (numpy/soundfile/librosa, all guarded)
      widgets.py             # MarqueeLabel, AutoScrollText, BeatWidget, ShimmerFrame
      workers.py             # AnalyzerWorker, BioWorker, SearchWorker,
                             #   LibraryScanThread
      window.py              # PlayerWindow (unchanged 1600-line class)
      app.py                 # main() entry point + crash/exception logging

## Run

    python Main.py
    # or
    python -m billsmusic.app

## Changes beyond moving code (intentional, minimal)

- Removed the duplicate `SearchWorker.stop()` (the file defined it twice;
  the second shadowed the first).
- `LibraryScanThread`'s `PlayerWindow` type hint is now a string annotation
  with a `TYPE_CHECKING` import, to avoid a circular import between
  workers.py and window.py.

## Deliberately NOT changed (these are the deeper refactor)

- `PlayerWindow` is still one ~1600-line class. Splitting it into a
  PlaybackController / LibraryModel / view is a separate, behavior-affecting job.
- The duplicated tag-reading methods on `PlayerWindow`
  (`_read_album_title_track`, `_tag_or`, `_parse_track_number`) still exist
  alongside the module-level versions in metadata.py. Collapsing them changes
  call sites, so it belongs to the refactor, not the skeleton.
- The `except Exception` swallowing and the insecure-TLS fallback default are
  unchanged here — flagged earlier, but they alter behavior so they're out of
  scope for a pure reorganization.

Note: workers.py still imports nothing from window.py at runtime, but
LibraryScanThread reaches into PlayerWindow's private methods at call time
(passed in by the caller). That coupling is preserved as-is.

---

## v2 — Visualizer rebuild + jukebox glow-up

### The "doesn't feel right" fix (audio.py)
The old analyzer re-normalised every single frame against that frame's own
max, which auto-gained away all dynamics — quiet and loud passages looked
identical, and the 8 ms FFT window had no bass resolution. Rewritten to:

- **Precompute the whole-track spectrogram on load** (2048-pt STFT, 512 hop,
  32 mel bands). `get_levels(t)` is now an instant array lookup, perfectly
  aligned to the audio clock — no lag, no per-frame jitter.
- **Normalise once over the whole track** using per-band percentiles (20th→0,
  97th→1), so loud parts genuinely look loud. A mild gamma (1.35) makes motion
  punchier.
- Works with **soundfile**, falling back to **librosa** for formats libsndfile
  can't decode. librosa is used if present but never required.

### Threading (workers.py)
`AnalyzerWorker` now decodes the track **off-lock** so the heavy work never
freezes the GUI thread, and emits a new `analysis_busy` signal. Time updates
just do cheap lookups. Poll timer dropped from 10 ms to 16 ms (~60 fps).

### Visualizer + GUI (widgets.py, window.py)
- New **neon synthwave** `BeatWidget`: glowing bars with bloom, white peak
  caps, mirror reflections, an animated horizon glow that pulses with loudness,
  and a magenta→violet→cyan frequency sweep. Five modes: neon, bars, waveform,
  dots, mirror (right-click to switch, same as before).
- Full **jukebox QSS theme**: deep-violet base, magenta/cyan gradient accents
  on buttons, sliders, progress bar, tree/queue selection, scrollbars.
- See `preview_neon.png` for the look.

All public APIs were preserved, so the rest of the app needed no further
changes. Tested end-to-end under PyQt6: every visual mode renders, the
analyzer tracks frequency correctly (80 Hz→band 0, 1 kHz→band 7, 8 kHz→band 22),
and the worker pipeline decodes-then-streams levels without blocking.

### Note on requirements
Playback through VLC needs **python-vlc** plus 64-bit VLC installed (pip install python-vlc).
The visualizer needs **numpy** and **soundfile** (`pip install numpy soundfile`).
librosa is optional but improves format support (`pip install librosa`).

---

## v3 — Visualiser diagnostic logging

Right-click any track in the library → **"Log visualiser data"**. The track
plays and every analysis frame is written to a CSV in:

    %LOCALAPPDATA%\Bills Music Player\vizlogs\<track>_<backend>_<timestamp>.csv

Logging stops automatically when the track changes or the app closes. The
filename records the backend (`builtin` or `vlc`) so you can run the same track
on each and compare.

### What the CSV contains
- **Header comments** with the track path, backend, latency offset, duration,
  and a **self-check**: the file is independently re-analysed and the expected
  peak band / level / RMS at 7 points across the track are recorded. This lets
  the live frames be validated against what the analysis *should* produce.
- **Per-frame rows**: `wall_s, player_clock_s, analysis_t_s, rms_db, peak_band`,
  then `band00..band31` (the 0–1 levels sent to the visualiser).

Comparing `player_clock_s` vs `analysis_t_s` reveals timing offsets; comparing
the live band columns vs the self-check reveals analysis errors. Running once
on each backend separates a sync problem from an analysis problem.

### Bug fixed alongside this
`_analyzer_tick` previously only read the **VLC** clock (`active_player.get_time()`),
so on the **built-in player** the visualiser received no time updates from the
proper path. It's now backend-aware via a `_player_clock_s()` helper — which on
its own should improve how the visualiser tracks when using the built-in player.

---

## v4 — Root cause of the "doesn't feel right" found & fixed

Your two diagnostic logs revealed it conclusively. The analysis numbers were
fine; the problem was the **clock**:

- The visualiser redraws at ~60 fps, but VLC's `get_time()` only reports a new
  position about **3.6 times per second**, jumping ~0.27 s each time.
- Result: ~**16 consecutive frames showed identical data**, then snapped
  forward — a stair-stepping stutter the smoothing only partly hid. Comparing
  the live band columns against the self-check showed the peak band drifting
  further from expected as the song went on, the signature of clock lag.

### Fix: clock interpolation
`_analyzer_tick` now anchors to each real clock update and **interpolates
using elapsed wall-time between updates**, producing a smoothly advancing
analysis time on every frame (verified: 120 distinct times across 120 frames
vs ~7 before). When the raw clock jumps it re-anchors; it won't run more than
one update-interval ahead, so seeks snap cleanly.

This should make the bars track the music continuously instead of freezing and
jumping. Re-run a logged track if you want to confirm — `player_clock_s` will
now advance every row.

### Note on the two logs
Both logs reported `backend,vlc` despite the internal/external filenames, so
both were VLC runs. That's fine — the fix targets the VLC clock, which is what
you're using. The built-in player path also benefits from the same
interpolation.

---

## v5 — Jukebox intro animation integrated

The animation previewer is now wired into the app (`billsmusic/overlay.py`).

### What happens
- **On every track change**: a full-screen intro plays — the artist sweeps in
  big with a clean neon glow and holds at centre, the title detonates in (one
  of four random styles each play: flyin / zoombounce / glitch / neonsweep),
  then artist + title shrink together into a glowing now-playing badge that
  parks below the header (top-right).
- **Across the track**: "DID YOU KNOW" cards drift in bottom-right, one at a
  time, paced over the song's duration. They're fed from the real artist bio
  (split into chunks); if no bio is found, they fall back to song facts built
  from the tags (album, genre, bitrate).

### How it's wired
- `JukeboxOverlay` is a click-through widget covering the central area, raised
  above everything, kept sized via `resizeEvent`. Because it's click-through,
  the controls and library underneath stay fully usable.
- `play_index` calls `_start_jukebox_intro(info)`; `_on_bio_ready` feeds bio
  chunks via `overlay.set_cards(...)`; `_song_facts(info)` is the no-bio
  fallback.
- Badge and cards are inset to clear the header and control rows.

### Tunables (in overlay.py, if you want to tweak the feel)
- Intro durations per style: the dict in `start_track`.
- `SHRINK_START` (0.80) — when the shrink-to-corner begins within the intro.
- Card dwell time: the `6.0` in `_tick` (seconds on screen).
- Badge size/position: `_badge_rect`; card size/position: `_paint_card`.

Backup of the pre-animation version was taken before this step.

---

## v6 — GUI cleanup: visualiser as hero, less clutter

Reworked the layout now that the jukebox cards carry the artist/track info:

- **Removed from view**: the always-on bio box and the always-on file-info
  (tag) box. The bio now flows entirely to the "DID YOU KNOW" cards; file info
  is shown on demand.
- **Visualiser is the hero**: it now fills the main right area. The library
  tree takes the left. A slim "UP NEXT" queue strip sits under the visualiser.
- **Header trimmed** to just the now-playing marquee + search.
- **Admin actions moved to right-click**: Add Folder, Rescan, and the built-in
  player toggle now live in the library context menu (also on empty-space
  right-click). "Show track info" is there too, and pops the file details as a
  centred jukebox-style card that auto-dismisses.

### Safety note on the rebuild
Rather than delete the bio/tag/button widgets (which ~40 code paths reference),
they're kept as hidden, still-functional objects and simply removed from the
visible layout. This avoided breaking the bio animation subsystem, the
scan enable/disable logic, etc. Verified: window builds, two-track cycling,
bio→cards, on-demand info panel, and resize all work.

### Tunables
- Queue strip height: `setMaximumHeight(140)` in `_build_ui`.
- Info panel size/dwell: `_paint_info_panel` (360×220) and `show_info_panel`
  `life=8.0`.
- Card clearance from bottom: `bottom_inset` in `_paint_card`.

---

## v7 — Badge title fit + longer card dwell

- **Badge title now fits**: long titles shrink (13→9pt) to fit the badge width,
  and if still too long they elide with "…" before the equaliser glyph, instead
  of spilling past the edge. Short titles still show full-size. The shrink-morph
  also lands on the fitted size so there's no overflow during the animation.
  (Tunable: the size range and reserved width in `_fit_text` / `_paint_badge`.)
- **"DID YOU KNOW" cards stay twice as long**: dwell time 6s → 12s
  (the value in `_tick`).

---

## v1.0.40 — Cast progress and equaliser clock

Google Cast playback now uses PyChromecast's live
`MediaStatus.adjusted_current_time` while the receiver is playing. The older
code used `current_time`, which is only the position from the receiver's most
recent status message. That raw value can remain unchanged for a long time,
so the app's progress bar and the local equaliser both appeared frozen even
while playback continued normally on the Cast device.

Paused and stopped Cast sessions still use the raw reported position, so their
clock cannot drift. The adjusted value is bounded to the receiver's reported
duration. A rate-limited `cast/clock_snapshot` diagnostics event records the
state, raw position, adjusted position, position source, and duration every ten
seconds during Cast playback; it contains no media path or track metadata.

Automated coverage protects the Cast adapter, the equaliser clock path, the
progress update path, paused behavior, and diagnostics rate limiting. A real
Chromecast listening test is still required to confirm receiver-specific
behavior and perceptual equaliser synchronisation.

The version-1.0.39 unexpected-exit report was also investigated. Its
`crash.log` contains only the normal crash-capture startup headers, with no
fatal traceback; there is no `error.log` and Windows recorded no application
error in the relevant period. The player log shows overlapping 1.0.39
processes and one process ending without normal shutdown markers, so the exit
was abrupt but its cause is not established by the available evidence.

The verified Windows package is
`installer\BillsMusicPlayerSetup-1.0.40-Nuitka-CastFix.exe`. The packaged app
reached its internal ready stage in a smoke launch, and the crash log gained
only the normal capture-start header with no fatal traceback. The user then
confirmed on a real Cast receiver that both the progress bar and equaliser now
work during playback.

## Now Playing detail stale-result protection

Every authoritative track activation now receives a monotonically increasing
Now Playing generation, including repeat plays of the same file from distinct
queue entries. Local lyrics, biography results, and Mini Player artwork work
capture that generation and only update widgets when it still identifies the
current track. Older biography requests are cooperatively superseded; a
network request already in progress is allowed to finish, but its result is
not emitted to the GUI when a newer track has taken over. Existing artwork
caches and Party Mode's per-request artwork guard remain in use, so cache hits
are not delayed and no playback or crossfade behaviour changes.

## v1.0.42 — smooth Now Playing startup

Starting a library track now schedules missing bitrate, key and BPM work in
the background. Cached values remain immediate; for audio tracks without them,
the heavier BPM/key work waits until the title animation has settled, then only
runs if that track is still current. This keeps the initial visual transition
smooth without delaying playback.

## Authoritative media capability registry

`billsmusic/media_capabilities.py` is the single source of truth for the
application's existing audio, video and karaoke extensions. It records library
scan and playlist eligibility, permitted playback backends, BPM/key and
ReplayGain eligibility, and generates the audio/video file-dialog filters.
Registry lookup is extension-only, case-insensitive and performs no file I/O;
the selected backend still decides whether a particular file can actually be
decoded. Add future formats to the registry and its parity test rather than
creating another extension list.

## v1.0.44 — serialised native audio analysis

Two captured Windows access violations showed different native audio jobs
overlapping after tracks were double-clicked. The first involved
Librosa/SciPy key estimation and the visualiser's NumPy FFT; after those were
guarded, the next involved simultaneous SoundFile/libsndfile reads by the
visualiser and waveform generator. Visualiser preparation, waveform
generation, BPM/key work and ReplayGain analysis now share one cancellable
background-analysis gate. Playback and the GUI do not use this gate.

Development verification now defaults to running from source with Python,
the test suite, and `python -m compileall`. Nuitka packaging is only run when
explicitly requested because its long builds and reused-cache failures are not
useful for routine development checks.

## Music-video visual transitions (Phase 1) and the GPU compositor (Phase 2A/2B/2C)

Video-to-video track changes can now play a short painted transition (Fade
Black, Flash, Push Left/Right, Zoom Blur, RGB Glitch, Film Burn, Pixel
Dissolve, plus Random Smooth/Energetic/All) instead of cutting instantly.
The transition is a screen-covering overlay, not a second video decoder — it
covers the switch rather than genuinely blending two videos together, and it
never interferes with normal music playback, karaoke, or the existing
crossfade engine. Configurable under Preferences → Video Transitions,
enabled by default.

A follow-up investigation ("Phase 2A") built a genuine dual-video
cross-dissolve, where two videos are actually decoded and blended on screen
at once, instead of hidden behind the overlay. A first attempt compositing
on the CPU (`QVideoFrame.paint()`/`QPainter`) reproducibly caused a native
crash and is permanently disabled — its code stays in the tree as the
record of that investigation, but nothing enables it. A second attempt
composites entirely on the GPU (Qt Quick's RHI scene graph + a compiled
shader) and passed extensive standalone testing (short runs, a 900-second/
240-transition soak test with real music-video content) with zero crashes;
this is implemented in source mode and packaged into the Nuitka standalone
build. It stays unavailable to users until a runtime GPU capability check
succeeds each session, and even then defaults to unchecked in Preferences.
A genuine native crash was found and fixed during packaging — not in the
compositor itself, but in the process-replacement logic used when
switching into GPU mode from an already-running session; see
`CODEX_HANDOFF.md`'s "Nuitka packaging phase for the GPU compositor"
section for the full root-cause investigation and fix.

A follow-up pass ("Phase 2B") extended the GPU compositor with three more
genuine dual-video effects alongside Cross Dissolve — Push Left/Right,
Wipe Left/Right, and Zoom — all real GPU shader transitions between two
simultaneously-decoded videos, not painted overlays. One shader file
branches on the chosen effect; effect choice never changes decoder
architecture, queue semantics, or audio handover. Selectable per-session
via a new "GPU transition effect" combo box in Preferences (including a
"Random GPU" option, never immediately repeating), shown only once GPU
mode is actually available. See `CODEX_HANDOFF.md`'s "Phase 2B" section
for the shader design and full verification record.

A second pass ("Phase 2C") added six more genuine GPU effects on the same
single shader: RGB Glitch, Pixel Dissolve, Luma Dissolve, Film Burn,
Zoom Blur, and Diagonal Wipe — 12 real GPU dual-video effects in total.
Four of the new names (RGB Glitch, Pixel Dissolve, Film Burn, Zoom Blur)
intentionally match existing Phase 1 painted-overlay effect names; the two
stay fully separate (different Preferences combo boxes, different config
keys, different code paths), so this is a deliberate naming parallel, not
a collision. Random GPU gained two curated pools — "Random GPU Smooth"
and "Random GPU Energetic" — alongside the original "Random GPU" (which
now draws from all 12). A short (4-minute) mixed-effect soak randomly
exercising all 12 effects via preload/commit/cancel/pause/seek completed
870 cycles with zero errors; see `CODEX_HANDOFF.md`'s "Phase 2C" section
for the full shader design, the boundary-correctness reasoning behind
each effect, and the soak/packaging verification record.

Manual testing of the installed build then found Zoom Blur genuinely
broken visually (dark collapse exposing the app's own background around
the transition midpoint, not a smooth smear), across two further fix
passes: forcing every sample's alpha to fully opaque turned out to be the
actual cause of a full-frame-black bug (on this GPU/driver, writing to a
texture-sampled value's alpha component, however it's spelled, renders
solid black — fixed by accumulating samples as complete, untouched
`vec4`s instead), and after that, video with a mismatched aspect ratio
still exposed the app's background through `VideoOutput`'s unpainted
letterbox/pillarbox padding — fixed by clamping every blur tap into each
source's real on-screen video-content rect (`VideoOutput.contentRect`)
instead of the generic full-item range — but that still left the actual
transparent pixel itself exposing whatever sat behind the compositor's
`ShaderEffect`. The real fix: `color: "black"` on the GPU compositor
scene's root `Window`, confirmed by temporarily setting it to an
unmistakable diagnostic colour and watching that colour appear exactly
where the exposed background had been — a presentation-layer fix that
never touches sampled video alpha (proven broken for that purpose on this
GPU/driver) and fixes every effect's unclamped padding, not just Zoom
Blur's. See `CODEX_HANDOFF.md`'s "Zoom Blur: actual root cause", "Zoom
Blur: letterbox/pillarbox padding fix", and "Zoom Blur: opaque compositor
background fix" subsections for the full investigation, including a
methodological finding partway through: the `grabWindow()`-based
screenshot harness used throughout was found not to reliably reflect live
rendering state across repeated captures within one running transition
(worked around in the final fix by using a fresh subprocess per capture).

Manual testing with real near-end natural playback then found a distinct,
deeper bug, unrelated to rendering: the outgoing deck's own video reaching
its natural end while a GPU cross-dissolve was already committed and still
playing would start a *second*, independent Phase 1 overlay transition on
top of it — because `VideoTransitionManager.handle_natural_end()`/
`request_manual_next()` only checked the Phase 1 controller's own state
(which never leaves `IDLE` for a GPU-handled transition), not whether the
separate GPU dual-deck engine had already committed one. That second
transition advanced the queue again and ran classic video teardown
(`load()`/`stop()`) on top of the still-running GPU transition — exactly
what surfaced as the app's own background flashing through between
videos, plus a black gap from the second overlay's own paint. Fixed by
checking the dual engine's own committed-state before either path is
allowed to start a competing transition. See `CODEX_HANDOFF.md`'s
"Video->Video queue-advance double-transition bug" subsection for the full
root-cause trace and the permanent lifecycle diagnostics added alongside
it.

The user's own review of those new diagnostics (once the diagnostics
level was raised to "Detailed" and `video_dual_transitions_enabled` was
actually turned on — neither had been true during earlier testing, so
none of the GPU/shader fixes above had ever actually been exercised)
pinpointed a further, distinct gap with exact log evidence: two of four
real Video->Video transitions still fell to the Phase 1 overlay *despite*
the GPU secondary deck having been ready for several seconds beforehand.
`handle_natural_end()` only ever checked whether a dual transition was
*already* committed — it never tried committing the already-ready one
itself, unlike `request_manual_next()`, which already did. Fixed by giving
`handle_natural_end()` the same try-the-dual-engine-first step. See
`CODEX_HANDOFF.md`'s "Video->Video natural-end missing commit attempt"
subsection for the full reconstructed timeline.

Real near-end testing of that fix then surfaced a further, distinct
timing bug: diagnostics showed every commit in the session firing at the
*exact same millisecond* as the outgoing track's own end-of-media, in
every transition — meaning `try_commit()` was only ever actually reached
through `handle_natural_end()`'s end-of-media fallback, never earlier.
By the time end-of-media fires, the outgoing deck has no live frame left
to blend, so what should be a genuine A+B cross-dissolve showed as a
roughly one-second solid black gap instead. Fixed by giving the GPU
engine its own bounded, single-shot deadline timer — armed the moment
the secondary deck becomes ready (and rescheduled on every position tick
after that) to fire at `duration - configured_lead_time` regardless of
whether any further position update ever arrives — so a transition now
normally commits well before end-of-media, with the existing
`handle_natural_end()` fallback kept only as a last-resort safety net.
See `CODEX_HANDOFF.md`'s "Bounded deadline timer: committing before EOF,
not at it" subsection for the full diagnostics evidence and design.

Real testing of that deadline timer directly measured it committing 990ms
before the outgoing deck's own real end-of-media -- proving the scheduling
itself correct -- yet the black gaps persisted, pointing downstream.
Reconstructing the diagnostics found the actual cause immediately: Phase
1's own legacy overlay engine was firing a *second*, competing transition
on the same millisecond as the GPU commit, because
`VideoTransitionManager.observe_position()` had its own automatic-commit
branch that never got the "a dual transition already committed" guard the
other two call sites received in v1.0.51 -- its only existing defence was
a flag set solely by that same method's own successful commit, and the
deadline timer commits through an independent timer callback that never
sets it. In one reconstructed case this cascaded further: Phase 1's
controller landed in a state that made the natural-end handler perform a
second, independent queue advance, silently skipping the next track
entirely. Fixed by adding the identical guard to this third call site. See
`CODEX_HANDOFF.md`'s "The v1.0.53 deadline timer's own regression"
subsection for the full reconstructed evidence.

The Inno Setup installer builds successfully for all three phases but has
not been installed and run as an installed app — left for manual
acceptance testing by design (Phase 2C's own instructions were explicit
about not auto-installing over a working copy during development). The
karaoke-in-packaged-build check also remains outstanding. See
`CODEX_HANDOFF.md`'s "Phase 2A"/"Phase 2B"/"Phase 2C" sections for the
full investigation, design, and implementation notes. The Phase 1 overlay
transition above is unaffected either way and remains the default,
always-available transition system.

With the GPU transition itself proven correctly scheduled, real frame
inspection found a genuinely different problem: some music video files
contain actual black or near-black frames at their own start/end (ABBA -
Super Trouper has a ~1s black tail and a ~0.75s black-and-silent opening;
ABBA - Knowing Me, Knowing You opens near-black for ~0.3-0.4s), so a
technically perfect cross-dissolve between "A already faded to black" and
"B still on black" still looks like a pause. "Smart Video Transition
Points" (new, off by default) detects these in small bounded windows via
a wholly separate, disposable child process — never the main app's
threads, matching this codebase's existing Qt Multimedia isolation policy
— and adjusts the *existing, unmodified* transition engine's timing
(commit slightly before a detected black tail) and secondary-deck start
position (skip a detected black-and-silent intro, only when audio is
proven silent there too, never on visual blackness alone) rather than
introducing any new transition mechanism. Real profiling found seeking to
each analysis sample individually cost ~1.3s/seek against 4K content
(decoding forward from the nearest keyframe each time); continuous
playback through the same bounded window instead brought that down to
real time. Verified against three purpose-built synthetic fixtures
(generated with PyQt6's own `QMediaCaptureSession`/`QVideoFrameInput` —
no ffmpeg dependency needed) and against the real ABBA files that exposed
the original issue — four of five real-file checks matched expectations
exactly, including both true negatives; the fifth (Knowing Me, Knowing
You's very short ~350ms intro) is a documented, conservative false
negative, not a wrong skip. See `CODEX_HANDOFF.md`'s "Smart Video
Transition Points" section for the full architecture, calibration data,
and known limitations.

Real-device diagnostics on that Smart Intro Seek feature then found the
incoming secondary deck kept silently playing (muted) for however long it
took the transition deadline to fire, so by commit time it had already
advanced several seconds past the position the seek had just placed it
at. The secondary is now paused the instant it's confirmed ready (before
that readiness is even announced) and resumed — never re-seeked — exactly
at commit, verified with a real, non-mocked test that the secondary's
position genuinely does not advance across a real wait. A related
real-device finding, a slow secondary preload on a network-mapped drive
hitting the previous flat 4000ms timeout and falling back to the older,
visually inferior transition style, is now handled by an adaptive
timeout: genuine loading progress earns bounded extra time (up to a hard
cap that can never be exceeded), while a stalled or failed load still
fails promptly. A bounded, cancellable background read-ahead for the
upcoming video was also added to help network-mapped drives specifically.
Once those were proven stable, a synchronized equal-power audio crossfade
was added for genuine GPU Video→Video transitions — new, off by default
under Preferences → Video Transitions ("Crossfade video audio" +
curve choice) — fading the outgoing and incoming videos' audio together
with the picture instead of switching instantly, while master volume and
mute both remain fully live and authoritative throughout. See
`CODEX_HANDOFF.md`'s "Video preload/buffering + held secondary deck +
synchronized audio crossfade" section for the full architecture,
real-device measurements, and the two verification-environment
limitations documented there (pixel-level frame verification and one
real-device audio/timing measurement both require a machine with actual
GPU/display access to fully confirm, beyond what this project's
automated test environment can exercise).

Later real-device reports surfaced two further classes of bug in the dual-
video transition/preload machinery, both since fixed: an already-promoted
video could be redundantly reactivated in a way that briefly showed a
black frame, and — separately — nothing in the IPC layer distinguished an
event genuinely belonging to a superseded transition or preload attempt
from one belonging to the current one, so a stale message could in
principle reach state that had already moved on. Every dual-video-related
event and command now carries an explicit `transition_id`/`preload_id`
that is validated before it can touch any state, both the commit-ack and
completion watchdogs are bound to the specific timer and id they were
armed for, and a genuine video-subprocess failure now reports which
transition it actually affected instead of blindly aborting whatever
happened to be committed. This is control/lifecycle hardening, not a
claim to have identified the original native crash trigger reported
during investigation — that remains open. The hardened system has since
been exercised extensively on real hardware, including a genuine
unscripted real-user session, with the stale-event rejection visibly
catching and discarding exactly the expected stale report on every
transition and zero misattributed state, false-positive watchdogs, or
double queue advances observed. See `CODEX_HANDOFF.md`'s "v1.0.64" entries
for the full design, the specific defects found, and the real-device
acceptance results.



