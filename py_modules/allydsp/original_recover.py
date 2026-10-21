"""systemd ExecStopPost entry point, including after a worker crash."""
import os
import sys
import argparse
import fcntl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from allydsp import original_runtime as runtime

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("restore", "prepare", "route"), default="restore")
    args = parser.parse_args()
    os.makedirs(runtime.ORIGINAL_DIR, exist_ok=True)
    with open(os.path.join(runtime.ORIGINAL_DIR, "routing.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        {"restore": runtime._restore_routing_locked,
         "prepare": runtime._prepare_route_locked,
         "route": runtime._route_after_ready_locked}[args.action]()
