"""Unit tests for chase_bot retry and resilience logic.

Covers the four functions that make outbound HTTP requests with retry:
  - _fetch_image_b64
  - _tool_get_available_runs
  - sounding_service_available
  - _tool_get_sounding
"""

import base64
from unittest.mock import MagicMock, call, patch

import pytest
import requests

import chase_bot

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

# A minimal valid PNG (1×1 transparent pixel) so base64 round-trips cleanly.
FAKE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVQI12Ng"
    "AAIABQAABjkB6QAAAABJRU5ErkJggg=="
)
FAKE_B64 = base64.standard_b64encode(FAKE_PNG).decode()

SOUNDING_HTML_OK = (
    "<html><body>" '<div id="snd_token" data-token="tok_abc123"></div>' "</body></html>"
)
SOUNDING_HTML_NO_TOKEN = "<html><body><div id='other'></div></body></html>"
SOUNDING_XML_OK = '<sounding lat="38.1234" lon="-97.5678" image="hrrr_20260403.png" />'
SOUNDING_XML_ERROR = '<sounding error="Model data unavailable" />'

FAKE_RUNS = [
    {"rh": "2026040312", "fh": 18},
    {"rh": "2026040306", "fh": 18},
]

SOUNDING_INP = {"rh": "2026040312", "fh": 6, "lat": 38.0, "lon": -97.5}


def mock_response(status=200, text="", content=b"", json_data=None):
    """Build a minimal mock requests.Response."""
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.content = content
    if json_data is not None:
        r.json.return_value = json_data
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(response=r)
    else:
        r.raise_for_status = MagicMock()
    return r


@pytest.fixture(autouse=True)
def reset_sounding_counter():
    """Reset the module-level sounding counter before each test."""
    chase_bot._sounding_counter = 0
    yield


# ---------------------------------------------------------------------------
# _fetch_image_b64
# ---------------------------------------------------------------------------


class TestFetchImageB64:
    def test_happy_path_returns_b64(self):
        with patch.object(
            chase_bot._session, "get", return_value=mock_response(content=FAKE_PNG)
        ):
            result = chase_bot._fetch_image_b64("http://example.com/img.png")
        assert result == FAKE_B64

    def test_404_returns_none_without_retry(self):
        with (
            patch.object(
                chase_bot._session, "get", return_value=mock_response(404)
            ) as mock_get,
            patch("chase_bot.time.sleep") as mock_sleep,
        ):
            result = chase_bot._fetch_image_b64("http://example.com/img.png")
        assert result is None
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    def test_transient_error_retries_and_succeeds(self):
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch("chase_bot.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [
                requests.RequestException("timeout"),
                mock_response(content=FAKE_PNG),
            ]
            result = chase_bot._fetch_image_b64("http://example.com/img.png")
        assert result == FAKE_B64
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once_with(5)

    def test_all_three_attempts_fail_returns_none(self):
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch("chase_bot.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = requests.RequestException("timeout")
            result = chase_bot._fetch_image_b64("http://example.com/img.png")
        assert result is None
        assert mock_get.call_count == 3
        assert mock_sleep.call_count == 2


# ---------------------------------------------------------------------------
# _tool_get_available_runs
# ---------------------------------------------------------------------------


class TestGetAvailableRuns:
    def test_happy_path_returns_latest_rh(self):
        with (
            patch.object(
                chase_bot._session,
                "get",
                return_value=mock_response(json_data=FAKE_RUNS),
            ),
            patch.object(chase_bot._session, "head", return_value=mock_response()),
        ):
            result = chase_bot._tool_get_available_runs({})
        assert result["latest_rh"] == "2026040312"
        assert "error" not in result

    def test_status_api_retries_with_backoff(self):
        good = mock_response(json_data=FAKE_RUNS)
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch.object(chase_bot._session, "head", return_value=mock_response()),
            patch("chase_bot.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [
                requests.ConnectionError("refused"),
                requests.ConnectionError("refused"),
                good,
            ]
            result = chase_bot._tool_get_available_runs({})
        assert result["latest_rh"] == "2026040312"
        assert mock_get.call_count == 3
        assert mock_sleep.call_args_list == [call(5), call(10)]

    def test_all_status_api_attempts_fail_returns_error(self):
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch("chase_bot.time.sleep"),
        ):
            mock_get.side_effect = requests.ConnectionError("refused")
            result = chase_bot._tool_get_available_runs({})
        assert "error" in result
        assert mock_get.call_count == 3

    def test_falls_back_to_older_run_when_latest_image_missing(self):
        runs = [{"rh": "2026040318", "fh": 18}, {"rh": "2026040312", "fh": 18}]

        def head_side_effect(url, **kwargs):
            return mock_response(200 if "2026040312" in url else 404)

        with (
            patch.object(
                chase_bot._session, "get", return_value=mock_response(json_data=runs)
            ),
            patch.object(chase_bot._session, "head", side_effect=head_side_effect),
        ):
            result = chase_bot._tool_get_available_runs({})
        assert result["latest_rh"] == "2026040312"

    def test_no_verified_run_returns_error(self):
        with (
            patch.object(
                chase_bot._session,
                "get",
                return_value=mock_response(json_data=FAKE_RUNS),
            ),
            patch.object(chase_bot._session, "head", return_value=mock_response(404)),
        ):
            result = chase_bot._tool_get_available_runs({})
        assert "error" in result


# ---------------------------------------------------------------------------
# sounding_service_available
# ---------------------------------------------------------------------------


class TestSoundingServiceAvailable:
    """Each probe attempt makes 3 _session.get calls: homepage, model page, sounding page."""

    def _probe_sequence(self, sounding_html=SOUNDING_HTML_OK):
        return [mock_response(), mock_response(), mock_response(text=sounding_html)]

    def test_service_up_first_try(self):
        with patch.object(chase_bot._session, "get") as mock_get:
            mock_get.side_effect = self._probe_sequence()
            assert chase_bot.sounding_service_available("2026040312") is True
        assert mock_get.call_count == 3

    def test_retries_on_connection_error(self):
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch("chase_bot.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [
                requests.ConnectionError(),  # attempt 1 fails at homepage
                *self._probe_sequence(),  # attempt 2 succeeds
            ]
            assert chase_bot.sounding_service_available("2026040312") is True
        mock_sleep.assert_called_once_with(30)

    def test_retries_when_token_missing(self):
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch("chase_bot.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [
                *self._probe_sequence(
                    sounding_html=SOUNDING_HTML_NO_TOKEN
                ),  # attempt 1: no token
                *self._probe_sequence(),  # attempt 2: ok
            ]
            assert chase_bot.sounding_service_available("2026040312") is True
        mock_sleep.assert_called_once_with(30)

    def test_all_attempts_fail_returns_false(self):
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch("chase_bot.time.sleep"),
        ):
            mock_get.side_effect = requests.ConnectionError()
            assert chase_bot.sounding_service_available("2026040312") is False


# ---------------------------------------------------------------------------
# _tool_get_sounding
# ---------------------------------------------------------------------------


class TestToolGetSounding:
    def _success_sequence(self):
        """Full _session.get side-effect list for one successful sounding fetch.
        Order: homepage, model_page, sounding_page, make_sounding, image."""
        return [
            mock_response(),  # homepage (session cookie)
            mock_response(),  # model page (referrer)
            mock_response(text=SOUNDING_HTML_OK),  # sounding page → token
            mock_response(text=SOUNDING_XML_OK),  # make_sounding.php → XML
            mock_response(content=FAKE_PNG),  # sounding image
        ]

    def test_happy_path_returns_image_and_text(self):
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch("chase_bot._save_daily_image"),
        ):
            mock_get.side_effect = self._success_sequence()
            result = chase_bot._tool_get_sounding(SOUNDING_INP)
        assert len(result) == 2
        assert result[0]["type"] == "image"
        assert result[1]["type"] == "text"
        assert "38.1234" in result[1]["text"]

    def test_retry_on_connection_error(self):
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch("chase_bot._save_daily_image"),
            patch("chase_bot.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [
                requests.ConnectionError("fail"),  # attempt 1 dies at homepage
                *self._success_sequence(),  # attempt 2 succeeds
            ]
            result = chase_bot._tool_get_sounding(SOUNDING_INP)
        assert result[0]["type"] == "image"
        mock_sleep.assert_called_once_with(15)

    def test_retry_when_snd_token_missing(self):
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch("chase_bot._save_daily_image"),
            patch("chase_bot.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [
                mock_response(),  # attempt 1: homepage
                mock_response(),  # attempt 1: model page
                mock_response(
                    text=SOUNDING_HTML_NO_TOKEN
                ),  # attempt 1: no token → ValueError
                *self._success_sequence(),  # attempt 2 succeeds
            ]
            result = chase_bot._tool_get_sounding(SOUNDING_INP)
        assert result[0]["type"] == "image"
        mock_sleep.assert_called_once_with(15)

    def test_retry_when_make_sounding_returns_error(self):
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch("chase_bot._save_daily_image"),
            patch("chase_bot.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [
                mock_response(),  # attempt 1: homepage
                mock_response(),  # attempt 1: model page
                mock_response(text=SOUNDING_HTML_OK),  # attempt 1: token ok
                mock_response(
                    text=SOUNDING_XML_ERROR
                ),  # attempt 1: make_sounding error
                *self._success_sequence(),  # attempt 2 succeeds
            ]
            result = chase_bot._tool_get_sounding(SOUNDING_INP)
        assert result[0]["type"] == "image"
        mock_sleep.assert_called_once_with(15)

    def test_all_attempts_fail_returns_error_text(self):
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch("chase_bot.time.sleep"),
        ):
            mock_get.side_effect = requests.ConnectionError("always fails")
            result = chase_bot._tool_get_sounding(SOUNDING_INP)
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "Error" in result[0]["text"]

    def test_saves_sounding_image_on_success(self):
        with (
            patch.object(chase_bot._session, "get") as mock_get,
            patch("chase_bot._save_daily_image") as mock_save,
        ):
            mock_get.side_effect = self._success_sequence()
            chase_bot._tool_get_sounding(SOUNDING_INP)
        mock_save.assert_called_once()
        saved_name = mock_save.call_args[0][1]
        assert saved_name.startswith("sounding_")
