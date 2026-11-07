"""Run even when the native package is absent from this interpreter's import path."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "opencoding" / "preflight.py"), run_name="__main__")
