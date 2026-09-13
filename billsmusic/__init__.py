"""Bills Music Player package."""

__version__ = "1.1.0"
# Distinguishes this exact build for the build_identity startup diagnostic
# (see window.py), independent of __version__ -- lets an interim/acceptance
# build that intentionally does not bump the public semantic version still
# be told apart in the field from every other build labelled 1.0.71.
# Normally equal to __version__; only ever set to something else for such a
# build, and reset to match __version__ again once that round closes.
BUILD_IDENTITY_TAG = __version__
