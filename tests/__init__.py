import os

# `import app` (and `import app.appletv` etc.) starts the pad thread and its
# HomePod/Apple TV warm-up scans; under test that background thread raced the
# tests that patch asyncio.sleep (life-ops #123). Honoured by app/spotify.py.
os.environ.setdefault("MUSICFIG_NO_AUTOSTART", "1")
