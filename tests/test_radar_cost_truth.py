from hassan_cloud.radar.candidates import SEED_CANDIDATES, USAGE_COST_UNVERIFIED as SEED_COST
from hassan_cloud.radar.github_source import USAGE_COST_UNVERIFIED as DISCOVERY_COST


def test_open_source_radar_does_not_imply_free_runtime_usage():
    assert SEED_COST == "UNVERIFIED"
    assert DISCOVERY_COST == "UNVERIFIED"
    assert SEED_CANDIDATES
    assert all(candidate["cost_type"] == "UNVERIFIED" for candidate in SEED_CANDIDATES)
    assert all(candidate["status"] == "NEW" for candidate in SEED_CANDIDATES)
