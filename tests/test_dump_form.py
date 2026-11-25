"""MP-4: dump_form form introspection + captcha detection — tests (issue #18).

Two layers:

- **Pure parser tests** (always run, no browser): the captcha-signal
  normalizer and the report assembly's belt-and-braces password masking.
- **Real-browser tests** (skip when playwright/chromium is unavailable —
  the repo's existing convention, see test_browser_pool.py): drive the REAL
  SpatialToolHandlers dispatch over file:// fixture pages with a real
  headless Chromium — labels, flags, limits, select options, radio groups,
  password masking, captcha presence/absence, and the probe_limits
  click-until-no-op cap probe (fixture enforces a SILENT JS cap of 3 on a
  5-option multi-select, exactly the Empower failure mode).
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from parousia.config import DEFAULT_CHROME_UA, ParousiaConfig
from parousia.spatial.tools import (
    ALL_SPATIAL_SCHEMAS,
    CAPTCHA_JS,
    DUMP_FORM_JS,
    MULTISELECT_PROBE_JS,
    SpatialToolHandlers,
    _assemble_form_report,
    _captcha_from_signals,
    dump_form_schema,
)

try:
    from playwright.async_api import async_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


FIXTURES = Path(__file__).parent / "fixtures"
DUMP_FORM_URI = (FIXTURES / "dump_form_page.html").resolve().as_uri()
CAPTCHA_URI = (FIXTURES / "captcha_page.html").resolve().as_uri()


def _handlers(page):
    """Real handlers over a (mocked-pool) REAL Playwright page."""
    pool = MagicMock()
    pool.get_browser = AsyncMock(return_value=SimpleNamespace(page=page))
    pool.proxy_for = MagicMock(return_value="")
    serializer = MagicMock()
    sdom = MagicMock()
    sdom.model_dump.return_value = {"url": "u", "status": 200}
    serializer.to_sdom.return_value = sdom
    return SpatialToolHandlers(ParousiaConfig(), pool, serializer)


async def _dispatch(handlers, name, **arguments):
    return json.loads(await handlers.dispatch(name, arguments, "agent-x"))


async def _selected(page):
    return await page.evaluate(
        "() => Array.from(document.getElementById('sectors')"
        ".selectedOptions).map((o) => o.value)"
    )


# ── Pure parser tests (no browser needed) ────────────────────────


class TestCaptchaSignalParser:
    def test_recaptcha_v2_visible_with_sitekey(self):
        out = _captcha_from_signals(
            {"present": True, "vendor": "recaptcha_v2", "type": "visible", "sitekey": "6Lk"}
        )
        assert out == {
            "present": True,
            "vendor": "recaptcha_v2",
            "type": "visible",
            "sitekey": "6Lk",
        }

    def test_invisible_type_passthrough(self):
        out = _captcha_from_signals(
            {"present": True, "vendor": "recaptcha_v2", "type": "invisible"}
        )
        assert out["present"] is True
        assert out["type"] == "invisible"
        assert out["sitekey"] is None

    def test_absent_is_none_vendor(self):
        out = _captcha_from_signals({"present": False, "vendor": "none"})
        assert out == {
            "present": False,
            "vendor": "none",
            "type": None,
            "sitekey": None,
        }

    def test_empty_or_none_signals(self):
        for empty in ({}, None):
            out = _captcha_from_signals(empty)
            assert out["present"] is False
            assert out["vendor"] == "none"

    def test_present_with_none_vendor_is_not_present(self):
        """present=true with vendor none would be a parse bug — normalize."""
        out = _captcha_from_signals({"present": True, "vendor": "none"})
        assert out["present"] is False

    def test_vendor_normalization(self):
        out = _captcha_from_signals({"present": True, "vendor": " HCAPTCHA "})
        assert out["vendor"] == "hcaptcha"


class TestAssembleMasksSecrets:
    """Belt-and-braces: the JS nulls passwords in-page; Python re-masks."""

    RAW = {
        "form_count": 1,
        "forms": [
            {
                "name": "f",
                "controls": [
                    {
                        "type": "password",
                        "name": "password",
                        "is_secret": True,
                        "value": None,
                        "current_value": None,
                    },
                    # Sabotage shape: a password WITHOUT is_secret and with a
                    # live value — the assembly must still mask it.
                    {
                        "type": "password",
                        "name": "evil",
                        "is_secret": False,
                        "value": "hunter2",
                        "current_value": "hunter2",
                    },
                    {
                        "type": "text",
                        "name": "email",
                        "is_secret": False,
                        "value": "a@b.c",
                        "current_value": "a@b.c",
                    },
                ],
            }
        ],
    }

    def test_passwords_nulled_even_if_raw_leaked(self):
        report = _assemble_form_report(dict(self.RAW))
        controls = report["forms"][0]["controls"]
        pw = next(c for c in controls if c["name"] == "password")
        evil = next(c for c in controls if c["name"] == "evil")
        assert pw["is_secret"] is True and pw["value"] is None
        assert evil["is_secret"] is True
        assert evil["value"] is None and evil["current_value"] is None
        assert "hunter2" not in json.dumps(report)

    def test_non_secret_values_pass_through(self):
        report = _assemble_report = _assemble_form_report(dict(self.RAW))
        email = next(
            c for c in report["forms"][0]["controls"] if c["name"] == "email"
        )
        assert email["value"] == "a@b.c"

    def test_empty_raw(self):
        report = _assemble_form_report(None)
        assert report == {"form_count": 0, "forms": []}

    def test_form_count_from_raw(self):
        report = _assemble_form_report({"form_count": 7, "forms": []})
        assert report["form_count"] == 7


class TestSchema:
    def test_dump_form_listed(self):
        names = {s["name"] for s in ALL_SPATIAL_SCHEMAS}
        assert "dump_form" in names
        assert "fill_credentials" in names  # MP-3 tool still registered

    def test_probe_flag_defaults_off(self):
        schema = dump_form_schema()
        probe = schema["inputSchema"]["properties"]["probe_limits"]
        assert probe["default"] is False

    def test_no_secret_arguments(self):
        props = dump_form_schema()["inputSchema"]["properties"]
        assert set(props) == {"probe_limits"}


# ── Real-browser tests (skip without playwright/chromium) ──────────


@pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="playwright not installed")
class TestDumpFormReal:
    @pytest_asyncio.fixture
    async def page(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            page = await browser.new_page()
            yield page
            await browser.close()

    async def _dump(self, page, uri=DUMP_FORM_URI, **extra):
        await page.goto(uri)
        handlers = _handlers(page)
        return await _dispatch(handlers, "dump_form", **extra)

    # ── Part A: form introspection ──

    @pytest.mark.asyncio
    async def test_form_count_and_grouping(self, page):
        report = await self._dump(page)
        assert report["form_count"] == 2
        names = [f["name"] for f in report["forms"]]
        assert names == ["enroll_form", "search_form"]

    @pytest.mark.asyncio
    async def test_labels_required_hidden_limits(self, page):
        """The DONE-gate fixture assertions: labels, required/hidden flags,
        and the limits section come back per control."""
        report = await self._dump(page)
        enroll = report["forms"][0]
        controls = {c["name"]: c for c in enroll["controls"]}

        email = controls["email"]
        assert email["label"] == "Email address"
        assert email["required"] is True
        assert email["limits"] == {
            "maxlength": "100",
            "pattern": "[^@]+@[^@]+",
        }
        assert email["value"] == "ricky@grid.example"
        assert email["visible"] is True

        age = controls["age"]
        assert age["label"] == "Age"
        assert age["limits"]["min"] == "18"
        assert age["limits"]["max"] == "99"

        hidden = controls["csrf"]
        assert hidden["type"] == "hidden"
        assert hidden["visible"] is False
        assert hidden["value"] == "tok-123"

        notes = controls["notes"]
        assert notes["tag"] == "textarea"
        assert notes["limits"]["minlength"] == "2"
        assert notes["placeholder"] == "Optional notes"

    @pytest.mark.asyncio
    async def test_select_options_carry_human_labels(self, page):
        report = await self._dump(page)
        sectors = next(
            c for c in report["forms"][0]["controls"] if c["name"] == "sectors"
        )
        assert sectors["multiple"] is True
        opts = {o["value"]: o for o in sectors["select_options"]}
        assert opts["fin"]["label"] == "Financial Services"
        assert opts["health"]["label"] == "Healthcare"
        assert opts["health"]["selected"] is True  # original selection
        assert opts["tech"]["label"] == "Technology"
        assert len(sectors["select_options"]) == 5

    @pytest.mark.asyncio
    async def test_radio_group_options_have_labels(self, page):
        """Radio values are opaque ids in the wild — the human labels ride
        along in group_options (Ricky's P2)."""
        report = await self._dump(page)
        radios = [
            c for c in report["forms"][0]["controls"] if c["type"] == "radio"
        ]
        group = {o["value"]: o["label"] for c in radios for o in c["group_options"]}
        assert group == {
            "free": "Free tier",
            "pro": "Professional",
            "ent": "Enterprise",
        }
        pro = next(c for c in radios if c["value"] == "pro")
        assert pro["checked"] is True

    @pytest.mark.asyncio
    async def test_checkbox_group_and_label(self, page):
        report = await self._dump(page)
        box = next(
            c for c in report["forms"][0]["controls"] if c["name"] == "notify[]"
        )
        assert box["type"] == "checkbox"
        assert box["label"] == "Notify me by email"
        assert box["checked"] is False

    @pytest.mark.asyncio
    async def test_password_is_secret_never_a_value(self, page):
        """The fixture pre-fills the password input — the report must carry
        is_secret + NO value, ever (DONE gate)."""
        report = await self._dump(page)
        enroll = report["forms"][0]
        pw = next(c for c in enroll["controls"] if c["name"] == "password")
        assert pw["type"] == "password"
        assert pw["is_secret"] is True
        assert pw["value"] is None
        assert pw["current_value"] is None
        raw = json.dumps(report)
        assert "prefilled-secret" not in raw
        # The DOM really does hold the secret — so the masking is real,
        # not a fixture artifact:
        dom_value = await page.evaluate(
            "() => document.getElementById('password').value"
        )
        assert dom_value == "prefilled-secret"

    @pytest.mark.asyncio
    async def test_disabled_flag(self, page):
        await page.goto(DUMP_FORM_URI)
        await page.evaluate(
            "() => document.getElementById('email').disabled = true"
        )
        handlers = _handlers(page)
        report = await _dispatch(handlers, "dump_form")
        email = next(
            c for c in report["forms"][0]["controls"] if c["name"] == "email"
        )
        assert email["disabled"] is True

    # ── Part B: captcha ──

    @pytest.mark.asyncio
    async def test_captcha_absent(self, page):
        report = await self._dump(page)
        assert report["captcha"] == {
            "present": False,
            "vendor": "none",
            "type": None,
            "sitekey": None,
        }

    @pytest.mark.asyncio
    async def test_captcha_invisible_recaptcha_v2(self, page):
        """Empower's exact case: invisible reCAPTCHA v2 with a sitekey."""
        report = await self._dump(page, uri=CAPTCHA_URI)
        assert report["captcha"]["present"] is True
        assert report["captcha"]["vendor"] == "recaptcha_v2"
        assert report["captcha"]["type"] == "invisible"
        assert report["captcha"]["sitekey"] == "6Lfixture_sitekey_not_real"

    @pytest.mark.asyncio
    async def test_captcha_visible_recaptcha_v2(self, page):
        await page.goto(DUMP_FORM_URI)
        await page.evaluate(
            "() => { const d = document.createElement('div');"
            " d.className = 'g-recaptcha';"
            " d.dataset.sitekey = '6Lvis';"
            " document.body.appendChild(d); }"
        )
        handlers = _handlers(page)
        report = await _dispatch(handlers, "dump_form")
        assert report["captcha"]["present"] is True
        assert report["captcha"]["type"] == "visible"
        assert report["captcha"]["sitekey"] == "6Lvis"

    @pytest.mark.asyncio
    async def test_hcaptcha_and_turnstile_markers(self, page):
        for vendor, js in (
            ("hcaptcha", "d.className = 'h-captcha'; d.dataset.sitekey = 'hc-key';"),
            (
                "turnstile",
                "d.className = 'cf-turnstile'; d.dataset.sitekey = 'ts-key';",
            ),
        ):
            await page.goto(DUMP_FORM_URI)
            await page.evaluate(
                "() => { const d = document.createElement('div');"
                f" {js}"
                " document.body.appendChild(d); }"
            )
            handlers = _handlers(page)
            report = await _dispatch(handlers, "dump_form")
            assert report["captcha"]["present"] is True, vendor
            assert report["captcha"]["vendor"] == vendor, report["captcha"]

    @pytest.mark.asyncio
    async def test_captcha_in_browse_to_result(self, page):
        """browse_to also carries the captcha field (no second call)."""
        handlers = _handlers(page)
        result = await _dispatch(
            handlers, "browse_to", url=CAPTCHA_URI, timeout_ms=10000
        )
        assert "captcha" in result, sorted(result)
        assert result["captcha"]["vendor"] == "recaptcha_v2"
        assert result["captcha"]["type"] == "invisible"
        assert result["captcha"]["sitekey"] == "6Lfixture_sitekey_not_real"
        assert "egress" in result  # MP-3 field still present
        assert "user_agent" in result["egress"]
        assert result["url"] == CAPTCHA_URI

    # ── probe_limits ──

    @pytest.mark.asyncio
    async def test_probe_off_by_default_never_mutates(self, page):
        """Default dump_form is read-only: the multi-select selection is
        untouched and no probe runs (DONE gate: never mutates
        unexpectedly)."""
        await page.goto(DUMP_FORM_URI)
        before = await _selected(page)
        assert before == ["health"]

        handlers = _handlers(page)
        calls = []
        original_evaluate = page.evaluate

        async def track_evaluate(script, *a, **kw):
            calls.append(script)
            return await original_evaluate(script, *a, **kw)

        page.evaluate = track_evaluate  # spy on the real page

        report = await _dispatch(handlers, "dump_form")
        after = await _selected(page)
        assert after == before
        assert "probe" not in report
        # Only the extraction + captcha scripts ran — never the probe.
        assert all(script != MULTISELECT_PROBE_JS for script in calls), [
            s[:60] for s in calls
        ]
        assert any(s == DUMP_FORM_JS for s in calls)

    @pytest.mark.asyncio
    async def test_probe_on_observes_silent_js_cap(self, page):
        """The Empower failure mode, end to end: the fixture enforces a
        SILENT JS cap of 3 on a 5-option multi-select — nowhere in the
        HTML. probe_limits=true observes 3, names the refusal, and
        restores the original selection."""
        await page.goto(DUMP_FORM_URI)
        before = await _selected(page)

        handlers = _handlers(page)
        report = await _dispatch(handlers, "dump_form", probe_limits=True)

        probed = report["probe"]
        assert len(probed) == 1, probed
        probe = probed[0]
        assert probe["name"] == "sectors"
        assert probe["form"] == "enroll_form"
        assert probe["option_count"] == 5  # all five options exist in the DOM
        assert probe["observed_cap"] == 3  # …but only 3 stay selectable
        assert "refused selection #4" in probe["capped_by"]

        after = await _selected(page)
        assert after == before  # selection restored exactly

    @pytest.mark.asyncio
    async def test_probe_uncapped_select_reports_no_cap(self, page):
        """An UNCAPPED multi-select (fresh, no page listeners) reports
        observed_cap == option_count, honestly labelled not enforced."""
        await page.goto(DUMP_FORM_URI)
        await page.evaluate(
            "() => { const f = document.getElementById('search');"
            " const s = document.createElement('select');"
            " s.name = 'tags'; s.multiple = true;"
            " ['a','b','c','d'].forEach((v) => {"
            "   const o = document.createElement('option');"
            "   o.value = v; o.textContent = 'Tag ' + v; s.appendChild(o); });"
            " f.appendChild(s); }"
        )
        handlers = _handlers(page)
        report = await _dispatch(handlers, "dump_form", probe_limits=True)
        probes = {p["name"]: p for p in report["probe"]}
        assert "tags" in probes, probes
        tags = probes["tags"]
        assert tags["option_count"] == 4
        assert tags["observed_cap"] == 4  # nothing refused: full count
        assert "none observed" in tags["capped_by"]
        # The capped fixture select is still probed (and still capped).
        assert probes["sectors"]["observed_cap"] == 3

    @pytest.mark.asyncio
    async def test_probe_restores_prefilled_multi_selection(self, page):
        """A page arriving with TWO pre-selected options gets them back."""
        await page.goto(DUMP_FORM_URI)
        await page.evaluate(
            "() => { const s = document.getElementById('sectors');"
            " for (const o of s.options) o.selected = ['fin','tech'].includes(o.value);"
            " s.dispatchEvent(new Event('change', {bubbles:true})); }"
        )
        # The fixture cap (3) allows 2 → both stay selected.
        before = await _selected(page)
        assert sorted(before) == ["fin", "tech"]

        handlers = _handlers(page)
        report = await _dispatch(handlers, "dump_form", probe_limits=True)
        after = await _selected(page)
        assert sorted(after) == ["fin", "tech"]

    # ── dispatch + integration ──

    @pytest.mark.asyncio
    async def test_dump_form_via_real_dispatch(self, page):
        """The full MCP path: dispatch('dump_form') routes, runs, and
        returns parseable JSON with the assembled report."""
        await page.goto(DUMP_FORM_URI)
        handlers = _handlers(page)
        raw = await handlers.dispatch("dump_form", {}, "agent-x")
        report = json.loads(raw)
        assert report["form_count"] == 2
        assert report["captcha"]["present"] is False

    @pytest.mark.asyncio
    async def test_unknown_tool_still_errors(self, page):
        handlers = _handlers(page)
        raw = await handlers.dispatch("no_such_tool", {}, "agent-x")
        assert json.loads(raw)["error"].startswith("Unknown spatial tool")