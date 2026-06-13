"""Tests for package structure and metadata."""


def test_package_importable():
    """Test that crucible package can be imported."""
    import crucible

    assert crucible is not None


def test_version_exists():
    """Test that version is accessible."""
    import crucible

    assert hasattr(crucible, "__version__")
    assert crucible.__version__ == "0.1.0"


def test_deepeval_telemetry_disabled():
    """Test that DeepEval telemetry is disabled on import."""
    import os

    # The package import sets this environment variable
    assert os.environ.get("DEEPEVAL_TELEMETRY_OPT_OUT") == "YES"
