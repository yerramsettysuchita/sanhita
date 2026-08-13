"""The certification workbench — a local, offline, build-step-free web app."""

from sanhita.web.app import create_app
from sanhita.web.highlight import Segment, segment_text

__all__ = ["Segment", "create_app", "segment_text"]
