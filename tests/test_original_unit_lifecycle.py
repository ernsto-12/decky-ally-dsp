"""The graceful helper precedes bounded cleanup of only the owned cgroup."""
import configparser

from allydsp import original_runtime


def test_original_unit_gracefully_stops_before_immediate_owned_cgroup_cleanup():
    # systemd accepts repeated Environment= assignments.
    unit = configparser.ConfigParser(interpolation=None, strict=False)
    unit.read_string(original_runtime.unit_text())
    service = unit["Service"]
    assert service["ExecStop"].endswith(" --action stop")
    assert service["ExecStop"].removesuffix(" --action stop") == service["ExecStopPost"]
    assert service["KillMode"] == "control-group"
    assert service["KillSignal"] == "SIGKILL"
    assert service["TimeoutStopSec"] == "12"
    # Restarting must pass through runtime preparation and route ownership.
    assert service["Restart"] == "no"
