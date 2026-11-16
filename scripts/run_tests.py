"""
run_tests.py — Run every offline test suite (each uses its own temporary database).

    python scripts/run_tests.py

The live Telegram check (tests/live_check.py) is separate because it needs a real bot token.
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = ["test_safety.py", "test_intel.py", "test_portal.py", "test_costs.py",
          "test_trips.py", "simulate_live_trip.py", "test_deploy.py", "test_ai_guide.py"]


def main() -> int:
    failed = []
    for name in SUITES:
        start = time.time()
        proc = subprocess.run([sys.executable, str(ROOT / "tests" / name)], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        ok = proc.returncode == 0
        print(f"{'PASS' if ok else 'FAIL'}  {name:24} {time.time() - start:5.1f}s")
        if not ok:
            failed.append(name)
            print((proc.stdout + proc.stderr)[-3000:])
    print(f"\n{len(SUITES) - len(failed)}/{len(SUITES)} suites passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
