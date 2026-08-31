"""Web API and server.

The API handlers are tested directly (no socket), then the HTTP layer is tested
over a real loopback server for the things only it can get wrong: auth, static
serving, path confinement and body limits.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from trading_bot.config import load_config
from trading_bot.web import api
from trading_bot.web.api import ApiError, dispatch
from trading_bot.web.server import STATIC_DIR, build_server

from test_backtest import make_signal


@pytest.fixture
def cfg(tmp_path):
    """Config whose journal points at a temp file, so tests never touch the repo."""
    from dataclasses import replace

    base = load_config("config/default.toml")
    return replace(base, journal_path=str(tmp_path / "j.jsonl"))


# ------------------------------------------------------------------ handlers


class TestRouting:
    def test_unknown_endpoint(self, cfg):
        status, body = dispatch("/api/nope", "GET", {}, cfg)
        assert status == 404
        assert "no such endpoint" in body["error"]

    def test_method_not_allowed(self, cfg):
        status, body = dispatch("/api/journal/close", "GET", {}, cfg)
        assert status == 405

    def test_every_route_is_callable(self, cfg):
        """Guards against a route pointing at a function that no longer exists."""
        for path, (handler, methods) in api.ROUTES.items():
            assert callable(handler), path
            assert methods


class TestHealthAndSettings:
    def test_health(self, cfg):
        status, body = dispatch("/api/health", "GET", {}, cfg)
        assert status == 200
        assert body["status"] == "ok"

    def test_health_states_it_cannot_trade(self, cfg):
        _, body = dispatch("/api/health", "GET", {}, cfg)
        assert body["executes_trades"] is False

    def test_settings_never_leak_the_api_key(self, cfg, monkeypatch):
        """The key is reported as present or absent, never by value."""
        monkeypatch.setenv("TRADING_BOT_API_KEY", "super-secret-value")
        _, body = dispatch("/api/settings", "GET", {}, cfg)
        assert body["data"]["has_api_key"] is True
        assert "super-secret-value" not in json.dumps(body)

    def test_symbols(self, cfg):
        _, body = dispatch("/api/symbols", "GET", {}, cfg)
        assert any(s["symbol"] == "EURUSD" for s in body["symbols"])
        assert "H1" in body["timeframes"]


class TestScan:
    def test_scan_synthetic(self, cfg):
        status, body = dispatch(
            "/api/scan", "GET", {"source": "synthetic", "symbols": "EURUSD"}, cfg
        )
        assert status == 200
        assert len(body["results"]) == 1
        assert body["results"][0]["status"] in ("signal", "no_setup")

    def test_chart_data_is_included_and_capped(self, cfg):
        _, body = dispatch("/api/scan", "GET", {"source": "synthetic", "symbols": "EURUSD"}, cfg)
        candles = body["results"][0]["candles"]
        assert 0 < len(candles) <= api.MAX_CHART_BARS
        assert {"t", "o", "h", "l", "c"} <= set(candles[0])

    def test_chart_can_be_switched_off(self, cfg):
        _, body = dispatch(
            "/api/scan", "GET", {"source": "synthetic", "symbols": "EURUSD", "chart": "0"}, cfg
        )
        assert "candles" not in body["results"][0]

    def test_bad_symbol_reports_per_row_without_failing_the_scan(self, cfg):
        _, body = dispatch("/api/scan", "GET", {"source": "csv", "symbols": "ZZZZZZ"}, cfg)
        assert body["results"][0]["status"] == "error"

    def test_symbol_limit(self, cfg):
        status, body = dispatch(
            "/api/scan", "GET", {"symbols": ",".join(f"SYM{i}" for i in range(25))}, cfg
        )
        assert status == 400
        assert "too many symbols" in body["error"]

    def test_unknown_source(self, cfg):
        status, body = dispatch("/api/scan", "GET", {"source": "carrier-pigeon"}, cfg)
        assert status == 400

    def test_journalling_is_off_by_default(self, cfg):
        dispatch("/api/scan", "GET", {"source": "synthetic", "symbols": "EURUSD"}, cfg)
        assert not Path(cfg.journal_path).exists() or not Path(cfg.journal_path).read_text()


class TestBacktestEndpoint:
    def test_runs(self, cfg):
        status, body = dispatch(
            "/api/backtest", "GET",
            {"source": "synthetic", "symbol": "EURUSD", "bars": 900, "split": ""}, cfg,
        )
        assert status == 200
        assert "metrics" in body["result"]
        assert "gate" in body["result"]

    def test_split_returns_both_halves(self, cfg):
        _, body = dispatch(
            "/api/backtest", "GET",
            {"source": "synthetic", "symbol": "EURUSD", "bars": 900, "split": "0.7"}, cfg,
        )
        assert "in_sample" in body and "out_of_sample" in body

    def test_rejects_an_out_of_range_split(self, cfg):
        status, _ = dispatch(
            "/api/backtest", "GET",
            {"source": "synthetic", "bars": 900, "split": "0.99"}, cfg,
        )
        assert status == 400

    def test_rejects_absurd_bar_counts(self, cfg):
        for bars in ("10", "999999", "abc"):
            status, _ = dispatch(
                "/api/backtest", "GET", {"source": "synthetic", "bars": bars}, cfg
            )
            assert status == 400, bars


class TestRiskEndpoint:
    def test_from_an_assumed_rate(self, cfg):
        status, body = dispatch("/api/risk", "GET", {"win_rate": "0.3", "trials": "300"}, cfg)
        assert status == 200
        assert body["profitable"] is True
        assert body["recommended_risk"] <= body["kelly"]

    def test_negative_edge_is_reported(self, cfg):
        _, body = dispatch("/api/risk", "GET", {"win_rate": "0.1", "trials": "300"}, cfg)
        assert body["profitable"] is False

    def test_rejects_a_nonsense_rate(self, cfg):
        status, _ = dispatch("/api/risk", "GET", {"win_rate": "5"}, cfg)
        assert status == 400


class TestJournalEndpoints:
    def test_empty_journal(self, cfg):
        status, body = dispatch("/api/journal", "GET", {}, cfg)
        assert status == 200
        assert body["count"] == 0

    def test_close_round_trip(self, cfg):
        from trading_bot.journal import Journal

        entry = Journal(cfg.journal_path).record(make_signal())
        status, body = dispatch(
            "/api/journal/close", "POST", {"id": entry.entry_id, "exit_price": 1.1085}, cfg
        )
        assert status == 200
        assert body["outcome"] == "win"

        _, listing = dispatch("/api/journal", "GET", {}, cfg)
        assert listing["closed"] == 1
        assert listing["live"]["trades"] == 1

    def test_close_requires_an_id(self, cfg):
        status, body = dispatch("/api/journal/close", "POST", {"exit_price": 1.1}, cfg)
        assert status == 400
        assert "id is required" in body["error"]

    def test_close_requires_a_price(self, cfg):
        status, _ = dispatch("/api/journal/close", "POST", {"id": "X@Y"}, cfg)
        assert status == 400

    def test_unknown_id_is_404(self, cfg):
        status, _ = dispatch(
            "/api/journal/close", "POST", {"id": "NOPE@2024", "exit_price": 1.1}, cfg
        )
        assert status == 404

    def test_double_close_is_409(self, cfg):
        from trading_bot.journal import Journal

        entry = Journal(cfg.journal_path).record(make_signal())
        dispatch("/api/journal/close", "POST", {"id": entry.entry_id, "exit_price": 1.1085}, cfg)
        status, _ = dispatch(
            "/api/journal/close", "POST", {"id": entry.entry_id, "exit_price": 1.1085}, cfg
        )
        assert status == 409


class TestPayloadsAreSerialisable:
    def test_every_get_endpoint_returns_json(self, cfg):
        for path in ("/api/health", "/api/settings", "/api/symbols", "/api/journal"):
            _, body = dispatch(path, "GET", {}, cfg)
            json.dumps(body)  # raises if a dataclass or enum leaked through


# -------------------------------------------------------------- HTTP layer


class LiveServer:
    """Runs the real server on a loopback port for the duration of a test."""

    def __init__(self, config, token=None):
        self.server = build_server(config, "127.0.0.1", 0, token)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path, headers=None):
        request = urllib.request.Request(self.url(path), headers=headers or {})
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read(), dict(response.headers)

    def post(self, path, payload, headers=None):
        body = json.dumps(payload).encode()
        head = {"Content-Type": "application/json", **(headers or {})}
        request = urllib.request.Request(self.url(path), data=body, headers=head, method="POST")
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())


@pytest.fixture
def live(cfg):
    with LiveServer(cfg) as server:
        yield server


class TestStaticServing:
    @pytest.mark.parametrize(
        "path,fragment",
        [
            ("/", b"trading.bot"),
            ("/index.html", b"<title>"),
            ("/app.css", b"--accent"),
            ("/app.js", b"function"),
            ("/sw.js", b"addEventListener"),
            ("/manifest.webmanifest", b"short_name"),
            ("/icon.svg", b"<svg"),
        ],
    )
    def test_assets_are_served(self, live, path, fragment):
        status, body, _ = live.get(path)
        assert status == 200
        assert fragment in body

    def test_manifest_is_valid_json(self, live):
        _, body, headers = live.get("/manifest.webmanifest")
        parsed = json.loads(body)
        assert parsed["start_url"] == "/"
        assert "manifest" in headers["Content-Type"]

    def test_shell_assets_listed_by_the_service_worker_exist(self, live):
        """A cached asset that 404s would break the offline install."""
        import re

        _, body, _ = live.get("/sw.js")
        shell = re.search(r"const SHELL = \[(.*?)\]", body.decode(), re.S)
        assert shell, "sw.js no longer declares a SHELL list"
        listed = re.findall(r"'(/[^']*)'", shell.group(1))
        assert listed, "SHELL list is empty"
        for asset in listed:
            status, _, _ = live.get(asset)
            assert status == 200, f"service worker caches {asset}, which the server does not serve"

    def test_missing_file_is_404(self, live):
        with pytest.raises(urllib.error.HTTPError) as exc:
            live.get("/nope.txt")
        assert exc.value.code == 404

    @pytest.mark.parametrize(
        "path", ["/../pyproject.toml", "/../../etc/passwd", "/%2e%2e/pyproject.toml"]
    )
    def test_path_traversal_is_refused(self, live, path):
        """Nothing outside the static directory may ever be served."""
        try:
            status, body, _ = live.get(path)
        except urllib.error.HTTPError as exc:
            assert exc.code in (400, 404)
            return
        assert status == 404 or b"[project]" not in body

    def test_security_headers_are_present(self, live):
        _, _, headers = live.get("/")
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"


class TestHttpApi:
    def test_health_over_http(self, live):
        status, body, _ = live.get("/api/health")
        assert status == 200
        assert json.loads(body)["status"] == "ok"

    def test_post_with_json_body(self, live):
        status, body = live.post("/api/risk", {"win_rate": 0.3, "trials": 300})
        assert status == 200
        assert body["kelly"] > 0

    def test_error_returns_json_not_a_traceback(self, live):
        with pytest.raises(urllib.error.HTTPError) as exc:
            live.get("/api/scan?source=nonsense")
        payload = json.loads(exc.value.read())
        assert "error" in payload
        assert "Traceback" not in payload["error"]

    def test_oversized_body_is_refused(self, live):
        with pytest.raises(urllib.error.HTTPError) as exc:
            live.post("/api/risk", {"pad": "x" * 400_000})
        assert exc.value.code == 413


class TestAuth:
    def test_no_token_means_open(self, cfg):
        with LiveServer(cfg) as server:
            status, _, _ = server.get("/api/health")
            assert status == 200

    def test_token_blocks_unauthenticated_requests(self, cfg):
        with LiveServer(cfg, token="s3cret") as server:
            with pytest.raises(urllib.error.HTTPError) as exc:
                server.get("/api/health")
            assert exc.value.code == 401

    def test_bearer_header_is_accepted(self, cfg):
        with LiveServer(cfg, token="s3cret") as server:
            status, _, _ = server.get("/api/health", {"Authorization": "Bearer s3cret"})
            assert status == 200

    def test_query_token_is_accepted(self, cfg):
        with LiveServer(cfg, token="s3cret") as server:
            status, _, _ = server.get("/api/health?token=s3cret")
            assert status == 200

    def test_wrong_token_is_refused(self, cfg):
        with LiveServer(cfg, token="s3cret") as server:
            with pytest.raises(urllib.error.HTTPError) as exc:
                server.get("/api/health?token=wrong")
            assert exc.value.code == 401

    def test_static_files_are_gated_too(self, cfg):
        """The UI itself must not be readable without the token."""
        with LiveServer(cfg, token="s3cret") as server:
            with pytest.raises(urllib.error.HTTPError) as exc:
                server.get("/")
            assert exc.value.code == 401

    def test_the_suite_is_insulated_from_the_developers_shell(self):
        """Pins the autouse fixture in conftest.

        Without it, exporting TRADING_BOT_TOKEN — exactly what SETUP.md tells a
        user to do — turns five tests in this file red for reasons that have
        nothing to do with the code.
        """
        assert "TRADING_BOT_TOKEN" not in os.environ
        assert "TRADING_BOT_API_KEY" not in os.environ

    def test_token_comes_from_the_environment(self, cfg, monkeypatch):
        monkeypatch.setenv("TRADING_BOT_TOKEN", "from-env")
        with LiveServer(cfg) as server:
            with pytest.raises(urllib.error.HTTPError):
                server.get("/api/health")
            status, _, _ = server.get("/api/health?token=from-env")
            assert status == 200


class TestStaticDirectory:
    def test_every_referenced_asset_exists(self):
        """index.html must not reference an asset that is not shipped."""
        html = (STATIC_DIR / "index.html").read_text()
        for asset in ("app.css", "app.js", "manifest.webmanifest", "icon.svg"):
            assert asset in html
            assert (STATIC_DIR / asset).exists()
