"""Display controls: show one image, or start and stop the TV's own slideshow."""
from __future__ import annotations

import pytest
from samsungtvws.exceptions import ResponseError


@pytest.fixture
def tv(mock_tv, monkeypatch):
    """A TV that remembers its slideshow setting."""
    import server

    monkeypatch.setattr(server, "TV_RETRY_DELAY", 0)
    state = {"value": "off", "type": ""}

    def _set(duration=0, type=True, category_id=None):
        state.update(value=str(duration) if duration else "off",
                     type=("shuffleslideshow" if type else "slideshow") if duration else "")

    mock_tv.get_slideshow_status.side_effect = lambda: dict(state)
    mock_tv.set_slideshow_status.side_effect = _set
    mock_tv.state = state
    return mock_tv


async def test_status_when_off(client, tv):
    data = (await client.get("/api/slideshow")).json()

    assert data["running"] is False and data["interval"] is None
    assert data["intervals"] == [3, 15, 60, 720, 1440]


async def test_start_uses_all_my_photos(client, tv):
    resp = await client.post("/api/slideshow", json={"duration": 60, "shuffle": False})

    assert resp.json() == {"running": True, "interval": 60, "shuffle": False,
                           "intervals": [3, 15, 60, 720, 1440]}
    tv.set_slideshow_status.assert_called_once_with(duration=60, type=False, category_id="MY-C0002")


async def test_stop(client, tv):
    tv.state.update(value="15", type="shuffleslideshow")

    resp = await client.post("/api/slideshow", json={"duration": 0})

    assert resp.json()["running"] is False


@pytest.mark.parametrize("duration", [7, -3, "15", None])
async def test_unsupported_interval_is_rejected(client, tv, duration):
    resp = await client.post("/api/slideshow", json={"duration": duration})

    assert resp.status_code == 400
    tv.set_slideshow_status.assert_not_called()


async def test_reports_when_the_tv_ignores_the_change(client, tv):
    tv.set_slideshow_status.side_effect = None

    assert (await client.post("/api/slideshow", json={"duration": 15})).status_code == 502


async def test_showing_an_image_stops_the_slideshow_first(client, tv):
    tv.state.update(value="15", type="shuffleslideshow")

    resp = await client.post("/api/select", json={"content_id": "MY_F0001"})

    assert resp.json()["slideshow_stopped"] is True
    assert tv.state["value"] == "off"
    names = [c[0] for c in tv.method_calls]
    assert names.index("set_slideshow_status") < names.index("select_image")


async def test_showing_an_image_leaves_an_idle_slideshow_alone(client, tv):
    resp = await client.post("/api/select", json={"content_id": "MY_F0001"})

    assert resp.json()["slideshow_stopped"] is False
    tv.set_slideshow_status.assert_not_called()


async def test_showing_an_image_works_without_slideshow_support(client, tv):
    tv.get_slideshow_status.side_effect = ResponseError("not supported")

    resp = await client.post("/api/select", json={"content_id": "MY_F0001"})

    assert resp.status_code == 200
    tv.select_image.assert_called_once()
