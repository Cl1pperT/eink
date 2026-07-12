from __future__ import annotations

import sys


def main() -> int:
    try:
        import tkinter as tk
    except ImportError:
        print("Tkinter is unavailable. On macOS, install Python 3.11+ from python.org or Homebrew with Tk support.", file=sys.stderr)
        return 2
    try:
        from .app import SimulatorApp
        root = tk.Tk()
    except Exception as exc:
        print(f"Unable to start Tkinter: {exc}\nUse a macOS Python distribution that includes Tk support.", file=sys.stderr)
        return 2
    SimulatorApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
