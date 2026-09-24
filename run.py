"""Run the diglot reader.

    python run.py            # http://127.0.0.1:8787
    python run.py --port 9000 --reload
    pythonw run.py --log data/app.log     # no console; the log file takes its place

``--log`` exists for the double-click launcher, which starts the app with
``pythonw.exe``. That interpreter has no console at all, so ``sys.stdout`` is
``None`` and the first ``print`` would raise instead of being ignored -- and
uvicorn's own log would go nowhere. Pointing both at a file is what makes a
windowless start diagnosable when something goes wrong.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from app.config import Settings
from app.server import set_shutdown_hook


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the diglot reader.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--reload", action="store_true", help="restart on file changes")
    parser.add_argument("--log", default="", metavar="FILE",
                        help="append the banner and the server log to FILE "
                             "(needed when there is no console to print to)")
    args = parser.parse_args()

    if args.log:
        # Line buffered, and appended: a restart should not erase why the last one
        # failed. Set before uvicorn is configured so its StreamHandler -- which
        # resolves sys.stderr when it is built -- picks this up too.
        stream = open(args.log, "a", encoding="utf-8", buffering=1)
        sys.stdout = stream
        sys.stderr = stream

    settings = Settings.load()
    print(f"env file : {settings.env_path or '(none found)'}")
    print(f"corpus   : {settings.corpus_dir}  ({'found' if settings.corpus_dir.is_dir() else 'MISSING'})")
    print(f"library  : {settings.library_dir}")
    print(f"database : {settings.db_path}")
    print(f"tutor    : {'ready — ' + settings.llm_model if settings.llm_ready else 'not configured (reading and review still work)'}")
    if settings.judge_ready:
        print(f"judge    : ready — {settings.typesafe_model}")
    elif settings.llm_ready:
        print("judge    : not configured (the tutor grades instead, less precisely)")
    else:
        print("judge    : not configured, and neither is the tutor — exercises cannot be "
              "generated or graded, so reading, quotes and review are what there is")
    print(f"\n  →  http://{args.host}:{args.port}\n")

    # Built by hand rather than with ``uvicorn.run`` so the server object exists to
    # be stopped: the reader can quit from the app, which matters because the
    # launcher starts it with no console and no window to close.
    config = uvicorn.Config("app.server:app", host=args.host, port=args.port,
                            reload=args.reload, log_level="info")
    server = uvicorn.Server(config)
    set_shutdown_hook(lambda: setattr(server, "should_exit", True))
    server.run()


if __name__ == "__main__":
    main()
