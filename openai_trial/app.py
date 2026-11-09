"""Explicit entry point; importing this file never sends an API request."""
from clinical_app.pipeline import main

if __name__ == "__main__":
    raise SystemExit(main())
