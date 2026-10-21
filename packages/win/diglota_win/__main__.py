"""``python -m diglota_win`` from packages/win, and the entry PyInstaller freezes."""

from diglota_win.shell import run

if __name__ == "__main__":
    raise SystemExit(run())
