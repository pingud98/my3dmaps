import numpy as np
import pytest

from my3dmaps.config import ProjectConfig
from my3dmaps.geo import make_grid
from my3dmaps.water import WaterResult


def pytest_configure(config):
    config.addinivalue_line("markers", "network: hits the real SRTM / Natural Earth mirrors")


def pytest_addoption(parser):
    parser.addoption("--network", action="store_true", default=False, help="run network tests")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--network"):
        return
    skip = pytest.mark.skip(reason="needs --network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def synth():
    """Synthetic 2x1 project: a lake on the west, a 3000 m peak on the east."""
    cfg = ProjectConfig(name="synth", scale=50000, tiles_x=2, tiles_y=1, pitch_mm=2.0, exaggeration=2.0)
    grid = make_grid(cfg)
    ny, nx = grid.shape
    X, Y = np.meshgrid(np.linspace(0, 1, nx), np.linspace(0, 1, ny))
    elev = 372 + 3000 * np.exp(-((X - 0.7) ** 2 + (Y - 0.5) ** 2) / 0.05) + 200 * np.sin(8 * X) * np.cos(6 * Y)
    lake = (X - 0.25) ** 2 + (Y - 0.5) ** 2 < 0.03
    elev = np.maximum(elev, 372.0)
    elev[lake] = 372.0
    water = WaterResult(lake, np.where(lake, 372.0, np.nan).astype(np.float32), [], 372.0)
    return cfg, grid, elev, water
