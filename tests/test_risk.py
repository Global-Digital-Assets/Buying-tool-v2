import pytest
import asyncio
from bot.risk import _select_tier

@pytest.mark.asyncio
@pytest.mark.parametrize("conf,tp_pct",[
    (0.97, 0.030),
    (0.92, 0.025),
    (0.88, 0.021),
    (0.82, 0.018),
    (0.75, 0.016),
    (0.65, 0.013),
])
async def test_tp_percentages(conf, tp_pct):
    tier = await _select_tier(conf)
    assert tier is not None
    assert abs(tier["tp_pct"] - tp_pct) < 1e-6
