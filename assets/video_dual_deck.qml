import QtQuick
import QtQuick.Window
import QtMultimedia

// Phase 2A GPU dual-video compositor scene. Loaded by
// GpuDualDeckVideoSubprocessController (real playback) and by the
// standalone GPU capability probe (video_subprocess.py's
// _run_gpu_capability_probe) -- the probe loads this exact file too, so a
// successful probe genuinely proves this scene (including the ShaderEffect
// and its compiled shader) initialises, not just that some generic Qt Quick
// window can pick a GPU backend.
Window {
    id: window
    // Never a real visible top-level window on the user's desktop -- the
    // parent process embeds this by native winId() before anything is
    // shown at its real position, exactly like classic mode's QVideoWidget
    // "show before winId()" workaround in this same file's
    // VideoSubprocessController.
    x: -32000
    y: -32000
    visible: true
    width: 640
    height: 360
    // Opaque black, not Qt Quick's default white: VideoOutput's
    // PreserveAspectFit fillMode does not paint anything into its own
    // letterbox/pillarbox padding (aspect-mismatched video leaves that
    // padding fully transparent), and every GPU transition effect's
    // texture() calls faithfully reproduce that transparency for any
    // pixel that samples it -- confirmed directly by temporarily setting
    // this colour to an unmistakable diagnostic colour (lime) and seeing
    // it appear exactly in the padding bands via grabWindow(). Without an
    // opaque backdrop here, those transparent pixels let whatever sits
    // behind this embedded window's native surface show through --
    // Bills Music Player's own Now Playing background/visualiser -- which
    // is what surfaced as a "radial/star background" during Zoom Blur
    // specifically (its multi-tap blending is what makes an otherwise
    // thin, easy-to-miss padding band visually prominent). This is a
    // presentation-layer fix, not a per-sample one: it does not touch any
    // sampled video alpha (already confirmed to render solid black on
    // this Direct3D11 RHI backend -- see video_dual_deck_blend.frag's
    // zoomBlur() history), it simply guarantees nothing can ever be
    // exposed *behind* the compositor, for every effect, not only Zoom
    // Blur.
    color: "black"

    MediaPlayer {
        id: player0
        objectName: "player0"
        videoOutput: videoOutput0
    }
    MediaPlayer {
        id: player1
        objectName: "player1"
        videoOutput: videoOutput1
    }
    AudioOutput {
        id: audio0
        objectName: "audio0"
        muted: true
    }
    AudioOutput {
        id: audio1
        objectName: "audio1"
        muted: true
    }
    Component.onCompleted: {
        player0.audioOutput = audio0
        player1.audioOutput = audio1
        window.requestActivate()
    }

    VideoOutput {
        id: videoOutput0
        objectName: "videoOutput0"
        anchors.fill: parent
    }
    VideoOutput {
        id: videoOutput1
        objectName: "videoOutput1"
        anchors.fill: parent
    }

    ShaderEffect {
        id: blend
        objectName: "blend"
        anchors.fill: parent
        property variant source1: videoOutput0
        property variant source2: videoOutput1
        property real progress: 0.0
        // 0=Cross Dissolve, 1=Push, 2=Wipe, 3=Zoom, 4=RGB Glitch,
        // 5=Pixel Dissolve, 6=Luma Dissolve, 7=Film Burn, 8=Zoom Blur,
        // 9=Diagonal Wipe -- see video_dual_deck_blend.frag's module
        // comment for the full per-effect design. Set once per commit by
        // GpuDualDeckVideoSubprocessController._commit_dual_transition
        // (video_subprocess.py), before transitionAnim starts.
        property real transitionType: 0.0
        // +1.0 / -1.0 -- push, wipe & diagonal wipe only, ignored by the
        // other effects.
        property real direction: 1.0
        // >0.5 if source1 (deck 0) is the outgoing video this transition,
        // else source2 (deck 1) is. Needed because progress now always
        // animates a fixed 0->1 regardless of which physical deck is
        // primary (see transitionAnim below) -- unlike Phase 2A's
        // original cross-dissolve-only design, which instead flipped the
        // *animation's* from/to per commit. That trick is fine for a
        // symmetric blend but not expressible for a directional effect.
        property real primaryIsFirst: 1.0
        // Phase 2C: fixed for the whole transition, generated once per
        // commit (never re-randomised per frame -- see
        // DualDeckController.generate_transition_seed in
        // video_dual_transition.py). Used by RGB Glitch/Pixel Dissolve/
        // Luma Dissolve/Film Burn for deterministic pseudo-randomness;
        // ignored by the other effects.
        property real seed: 0.0
        // Phase 2C: live binding to this item's own size (which tracks
        // the embedded window's real size, including resizes, since
        // blend fills its parent) -- Pixel Dissolve only, to keep block
        // size visually consistent across different output resolutions.
        // Nothing on the Python side ever sets this directly.
        property vector2d resolution: Qt.vector2d(width, height)
        // Zoom Blur only: the actual on-screen video-content rect inside
        // each VideoOutput (excluding letterbox/pillarbox padding), in the
        // same 0..1 UV space as qt_TexCoord0 -- both VideoOutputs share
        // blend's own size via anchors.fill, so dividing by videoOutputN's
        // own width/height is equivalent to dividing by blend's. Falls
        // back to the full 0..1 rect (no restriction) if contentRect is
        // degenerate (e.g. briefly zero-sized before a source has ever
        // reported a frame), so this can never clamp every sample to a
        // single frozen corner pixel. xy = content-rect min, zw = max.
        property vector4d content1Rect: {
            var r = videoOutput0.contentRect;
            var w = Math.max(1, videoOutput0.width);
            var h = Math.max(1, videoOutput0.height);
            if (r.width <= 0 || r.height <= 0) {
                return Qt.vector4d(0.0, 0.0, 1.0, 1.0);
            }
            return Qt.vector4d(r.x / w, r.y / h, (r.x + r.width) / w, (r.y + r.height) / h);
        }
        property vector4d content2Rect: {
            var r = videoOutput1.contentRect;
            var w = Math.max(1, videoOutput1.width);
            var h = Math.max(1, videoOutput1.height);
            if (r.width <= 0 || r.height <= 0) {
                return Qt.vector4d(0.0, 0.0, 1.0, 1.0);
            }
            return Qt.vector4d(r.x / w, r.y / h, (r.x + r.width) / w, (r.y + r.height) / h);
        }
        fragmentShader: "video_dual_deck_blend.frag.qsb"
        // Effect-appropriate easing, chosen once per committed transition
        // (transitionType is set immediately before commit). Kept here in
        // QML/JS rather than passed from Python to avoid marshalling a
        // QEasingCurve across the same PyQt6 boundary that enum-typed
        // MediaPlayer signals already needed a bridge workaround for (see
        // the Connections blocks below). RGB Glitch and Film Burn use
        // Linear specifically: both shape their own internal timing
        // directly from the raw progress value (e.g. Film Burn's
        // brightness peak is tuned to land at progress=0.5) -- an extra
        // easing curve on top would shift where those internal landmarks
        // fall within the configured duration.
        onTransitionTypeChanged: {
            if (transitionType < 0.5) {
                transitionAnim.easing.type = Easing.InOutQuad   // Cross Dissolve
            } else if (transitionType < 1.5) {
                transitionAnim.easing.type = Easing.InOutCubic  // Push
            } else if (transitionType < 2.5) {
                transitionAnim.easing.type = Easing.InOutQuad   // Wipe
            } else if (transitionType < 3.5) {
                transitionAnim.easing.type = Easing.InOutCubic  // Zoom
            } else if (transitionType < 4.5) {
                transitionAnim.easing.type = Easing.Linear      // RGB Glitch
            } else if (transitionType < 5.5) {
                transitionAnim.easing.type = Easing.InOutQuad   // Pixel Dissolve
            } else if (transitionType < 6.5) {
                transitionAnim.easing.type = Easing.InOutQuad   // Luma Dissolve
            } else if (transitionType < 7.5) {
                transitionAnim.easing.type = Easing.Linear      // Film Burn
            } else if (transitionType < 8.5) {
                transitionAnim.easing.type = Easing.InOutCubic  // Zoom Blur
            } else {
                transitionAnim.easing.type = Easing.InOutQuad   // Diagonal Wipe
            }
        }
    }

    NumberAnimation {
        id: transitionAnim
        objectName: "transitionAnim"
        target: blend
        property: "progress"
        from: 0.0
        to: 1.0
        duration: 500
        easing.type: Easing.InOutQuad
    }

    // -- enum-safe IPC bridge --------------------------------------------
    // QQuickMediaPlayer's enum-typed signals/properties (mediaStatus,
    // playbackState, error) do not marshal to Python cleanly through
    // PyQt6's QML introspection (confirmed: reading them via
    // QObject.property() or connecting Python slots directly to these
    // signals raises "unable to convert a C++ ... instance to a Python
    // object"). Routing through QML JavaScript first works, because QML
    // treats C++ enum values as plain numbers -- `bridge` is a Python
    // QObject with plain-int/str-typed pyqtSignals, injected into the QML
    // context before this file is loaded (see
    // GpuDualDeckVideoSubprocessController.__init__).
    Connections {
        target: player0
        function onMediaStatusChanged(status) { bridge.mediaStatusChanged(0, status) }
        function onErrorOccurred(error, errorString) { bridge.errorOccurred(0, error, errorString) }
        function onPlaybackStateChanged(state) { bridge.playbackStateChanged(0, state) }
    }
    Connections {
        target: player1
        function onMediaStatusChanged(status) { bridge.mediaStatusChanged(1, status) }
        function onErrorOccurred(error, errorString) { bridge.errorOccurred(1, error, errorString) }
        function onPlaybackStateChanged(state) { bridge.playbackStateChanged(1, state) }
    }

    // -- input forwarding --------------------------------------------------
    // Mirrors classic mode's QVideoWidget mouse/key overrides in
    // VideoSubprocessController -- double-click/right-click/Escape land on
    // this embedded window's own native surface regardless of process
    // boundary, so they must be captured and forwarded here rather than
    // relying on the parent's widgets to ever see them.
    MouseArea {
        id: inputArea
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        onDoubleClicked: bridge.doubleClicked()
        onClicked: function(mouse) {
            if (mouse.button === Qt.RightButton) {
                bridge.contextMenuRequested()
            }
        }
    }
    Keys.onEscapePressed: bridge.escapePressed()
}
