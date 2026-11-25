"""The built site in a real browser, with the CSP it ships."""

from __future__ import annotations

import json
from importlib.resources import files
from urllib.request import urlopen

import pytest
from playwright.sync_api import expect, sync_playwright

from record.__main__ import TAPE
from scripts import build
from tests.serve import OUT, start

KEYS = ["contention", "eviction", "spill", "crash", "stall"]


@pytest.fixture(scope="module")
def site():
    build.main()
    server = start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page(viewport={"width": 1400, "height": 900})
    problems: list[str] = []
    pg.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: problems.append(str(e)))
    pg.problems = problems
    yield pg
    pg.close()
    assert [p for p in problems if "403" not in p] == []


def test_dashboard_is_the_published_one(site):
    ui = files("argus_iris") / "ui"
    for name in ("app.js", "app.css"):
        assert (OUT / "dash/ui" / name).read_bytes() == (ui / name).read_bytes()
    with urlopen(site) as r:
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]


@pytest.mark.parametrize("key", KEYS)
def test_every_step_of_every_scene(site, page, key):
    beats = json.loads((TAPE / f"{key}.json").read_text())["beats"]
    page.goto(f"{site}#{key}")
    dash = page.frame_locator("#dash")
    expect(dash.locator("#node-rtx3090")).to_be_visible()
    expect(dash.locator("#live")).to_have_text("recorded")
    for beat in beats:
        page.click("#next")
        expect(page.locator("#beat-title")).to_have_text(beat["title"])
        if beat["focus"]:
            expect(dash.locator(f"#node-{beat['focus']}.demo-focus")).to_have_count(1)


def test_the_dashboard_frame_gets_no_second_beacon(site):
    frame = (OUT / "_headers").read_text().split("/dash/ui/*")[1]
    assert "no-transform" in frame


def test_there_is_a_404_page(site):
    # Without one, Cloudflare Pages answers unknown paths with the tour and a 200.
    assert "Not found" in (OUT / "404.html").read_text()


def leases(page, node="rtx3090"):
    return page.frame_locator("#dash").locator(f"#node-{node} table.leases tbody")


def test_going_back_undoes_what_happened(site, page):
    page.goto(f"{site}#contention")
    page.click("#next")
    page.click("#next")
    expect(leases(page)).to_contain_text("argus-forge")
    page.click("#prev")
    expect(leases(page)).not_to_contain_text("argus-forge")
    expect(leases(page)).to_contain_text("argus-proof")


def test_it_plays_by_itself(site, page):
    page.goto(f"{site}#contention")
    expect(page.locator("#play")).to_be_enabled()
    page.click("#play")
    expect(page.locator("#beat-title")).to_have_text("A batch job takes most of the card", timeout=8000)
    expect(leases(page)).to_contain_text("argus-proof")
    expect(page.locator("#beat-title")).to_have_text("The next one does not fit, so it waits", timeout=30000)


def test_the_stall_is_marked(site, page):
    page.goto(f"{site}#stall")
    for _ in range(4):
        page.click("#next")
    expect(leases(page)).to_contain_text("granted · stalled")
    expect(leases(page)).to_contain_text("asked to yield")


def test_actions_say_it_is_a_recording(site, page):
    page.goto(f"{site}#eviction")
    page.frame_locator("#dash").get_by_role("button", name="Evict").first.click()
    expect(page.frame_locator("#dash").locator(".result").first).to_contain_text("This is a recording")


def test_dashboard_alone_goes_to_the_tour(site, page):
    page.goto(f"{site}dash/ui/index.html")
    page.wait_for_url(lambda u: u.split("#")[0] == site)
    expect(page.locator("#play")).to_be_enabled()


def test_a_missing_tape_is_reported(site, browser):
    pg = browser.new_page()
    pg.route("**/tape/*.json", lambda r: r.abort())
    pg.goto(site)
    expect(pg.locator("#beat-title")).to_have_text("The recording did not load")
    pg.close()


def test_phone_width_does_not_scroll_sideways(site, browser):
    pg = browser.new_page(viewport={"width": 390, "height": 844})
    for key in KEYS:
        pg.goto(f"{site}#{key}")
        expect(pg.locator("#play")).to_be_enabled()
        assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    pg.close()
