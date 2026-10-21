"""Run the web app. The entry point itself lives in ``packages/web/run.py``.

Kept at the root so the command everyone already knows -- ``python run.py`` -- and
``Open Diglot.vbs`` keep working after the move into a monorepo. Two lines, no
logic: the arguments, the banner and the server are the package's.
"""

from __future__ import annotations

from packages.web.run import main

if __name__ == "__main__":
    main()
