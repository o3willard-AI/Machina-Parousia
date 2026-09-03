import json
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

from parousia.spatial.tools import (
    browse_to_schema,
    interact_schema,
    extract_page_state_schema,
    ALL_SPATIAL_SCHEMAS,
    SpatialToolHandlers
)
from parousia.config import ParousiaConfig


def _mock_sdom(dump_value):
    """Return a mock SDOM object whose .model_dump() yields a JSON-safe dict."""
    mock = MagicMock()
    mock.model_dump.return_value = dump_value
    return mock


def _make_handler(mock_browser_pool_class, mock_serializer_class, mock_browser, mock_page):
    """Build handlers with async get_browser -> mock_browser and .page -> mock_page."""
    mock_browser.page = mock_page
    mock_page.goto = AsyncMock()
    mock_browser_pool_class.get_browser = AsyncMock(return_value=mock_browser)
    config = ParousiaConfig()
    return SpatialToolHandlers(config, mock_browser_pool_class, mock_serializer_class)


class TestSpatialTools(unittest.TestCase):

    def test_browse_to_schema(self):
        """Test browse_to schema validation."""
        schema = browse_to_schema()
        self.assertEqual(schema["name"], "browse_to")
        self.assertIn("url", schema["inputSchema"]["properties"])
        self.assertIn("timeout_ms", schema["inputSchema"]["properties"])
        self.assertIn("extract_mode", schema["inputSchema"]["properties"])
        self.assertTrue("url" in schema["inputSchema"]["required"])

    def test_interact_schema(self):
        """Test interact schema validation."""
        schema = interact_schema()
        self.assertEqual(schema["name"], "interact")
        self.assertIn("id", schema["inputSchema"]["properties"])
        self.assertIn("action", schema["inputSchema"]["properties"])
        self.assertIn("text", schema["inputSchema"]["properties"])
        self.assertIn("timeout_ms", schema["inputSchema"]["properties"])
        self.assertTrue("id" in schema["inputSchema"]["required"])
        self.assertTrue("action" in schema["inputSchema"]["required"])

    def test_extract_page_state_schema(self):
        """Test extract_page_state schema validation."""
        schema = extract_page_state_schema()
        self.assertEqual(schema["name"], "extract_page_state")
        self.assertIn("mode", schema["inputSchema"]["properties"])

    def test_all_spatial_schemas(self):
        """Test that ALL_SPATIAL_SCHEMAS contains correct schemas."""
        self.assertEqual(len(ALL_SPATIAL_SCHEMAS), 3)
        names = [schema["name"] for schema in ALL_SPATIAL_SCHEMAS]
        self.assertIn("browse_to", names)
        self.assertIn("interact", names)
        self.assertIn("extract_page_state", names)

    # ── Handler tests ────────────────────────────────────────────────
    # Handlers store mocks directly (no .return_value indirection).
    # _handle_* call self.browser_pool.get_browser() and use the browser's
    # persistent .page, plus self.serializer.to_sdom(). The serializer now
    # returns an SDOM object, so handlers must call .model_dump() before the
    # result is json.dumps'd by dispatch().

    @patch('parousia.spatial.tools.BrowserPoolManager')
    @patch('parousia.spatial.tools.SpatialSerializer')
    def test_handle_browse_to_success(self, mock_serializer_class, mock_browser_pool_class):
        """Test browse_to handler success case."""
        mock_browser = MagicMock()
        mock_page = MagicMock()
        mock_page.content = AsyncMock(return_value="<html><body>Hello World</body></html>")
        mock_serializer_class.to_sdom.return_value = _mock_sdom({"type": "sdom", "content": "test"})

        handlers = _make_handler(mock_browser_pool_class, mock_serializer_class, mock_browser, mock_page)
        result = asyncio.run(handlers.dispatch("browse_to", {"url": "https://example.com"}, "agent1"))
        parsed_result = json.loads(result)

        self.assertTrue(parsed_result["extracted"])
        self.assertEqual(parsed_result["url"], "https://example.com")
        self.assertEqual(parsed_result["sdom"], {"type": "sdom", "content": "test"})

    @patch('parousia.spatial.tools.BrowserPoolManager')
    @patch('parousia.spatial.tools.SpatialSerializer')
    def test_handle_interact_click_success(self, mock_serializer_class, mock_browser_pool_class):
        """Test interact handler with click action success case."""
        mock_browser = MagicMock()
        mock_page = MagicMock()
        mock_page.click = AsyncMock()

        handlers = _make_handler(mock_browser_pool_class, mock_serializer_class, mock_browser, mock_page)
        result = asyncio.run(handlers.dispatch("interact", {"id": "button1", "action": "click"}, "agent1"))
        parsed_result = json.loads(result)

        self.assertTrue(parsed_result["success"])
        self.assertEqual(parsed_result["id"], "button1")
        self.assertEqual(parsed_result["action"], "click")

    @patch('parousia.spatial.tools.BrowserPoolManager')
    @patch('parousia.spatial.tools.SpatialSerializer')
    def test_handle_interact_type_success(self, mock_serializer_class, mock_browser_pool_class):
        """Test interact handler with type action success case."""
        mock_browser = MagicMock()
        mock_page = MagicMock()
        mock_page.type = AsyncMock()

        handlers = _make_handler(mock_browser_pool_class, mock_serializer_class, mock_browser, mock_page)
        result = asyncio.run(handlers.dispatch("interact", {"id": "input1", "action": "type", "text": "test"}, "agent1"))
        parsed_result = json.loads(result)

        self.assertTrue(parsed_result["success"])
        self.assertEqual(parsed_result["id"], "input1")
        self.assertEqual(parsed_result["action"], "type")

    @patch('parousia.spatial.tools.BrowserPoolManager')
    @patch('parousia.spatial.tools.SpatialSerializer')
    def test_handle_extract_page_state_success(self, mock_serializer_class, mock_browser_pool_class):
        """Test extract_page_state handler success case."""
        mock_browser = MagicMock()
        mock_page = MagicMock()
        mock_page.content = AsyncMock(return_value="<html><body>Hello World</body></html>")
        mock_page.url = "https://example.com"
        mock_serializer_class.to_sdom.return_value = _mock_sdom({"type": "sdom", "content": "test"})

        handlers = _make_handler(mock_browser_pool_class, mock_serializer_class, mock_browser, mock_page)
        result = asyncio.run(handlers.dispatch("extract_page_state", {"mode": "full"}, "agent1"))
        parsed_result = json.loads(result)

        self.assertTrue(parsed_result["extracted"])
        self.assertEqual(parsed_result["mode"], "full")

    @patch('parousia.spatial.tools.BrowserPoolManager')
    @patch('parousia.spatial.tools.SpatialSerializer')
    def test_handle_browser_unavailable(self, mock_serializer_class, mock_browser_pool_class):
        """Test handler when browser is unavailable."""
        mock_browser_pool_class.get_browser = AsyncMock(return_value=None)

        config = ParousiaConfig()
        handlers = SpatialToolHandlers(config, mock_browser_pool_class, mock_serializer_class)
        result = asyncio.run(handlers.dispatch("browse_to", {"url": "https://example.com"}, "agent1"))
        parsed_result = json.loads(result)

        self.assertIn("error", parsed_result)

    @patch('parousia.spatial.tools.BrowserPoolManager')
    @patch('parousia.spatial.tools.SpatialSerializer')
    def test_agent_isolation(self, mock_serializer_class, mock_browser_pool_class):
        """Test that different agents get isolated browsers."""
        mock_browser1 = MagicMock()
        mock_page1 = MagicMock()
        mock_browser1.page = mock_page1
        mock_page1.goto = AsyncMock()
        mock_page1.content = AsyncMock(return_value="<html><body>Agent 1</body></html>")
        mock_browser2 = MagicMock()
        mock_page2 = MagicMock()
        mock_browser2.page = mock_page2
        mock_page2.goto = AsyncMock()
        mock_page2.content = AsyncMock(return_value="<html><body>Agent 2</body></html>")

        mock_browser_pool_class.get_browser = AsyncMock(side_effect=[mock_browser1, mock_browser2])
        mock_serializer_class.to_sdom.return_value = _mock_sdom({"type": "sdom"})

        config = ParousiaConfig()
        handlers = SpatialToolHandlers(config, mock_browser_pool_class, mock_serializer_class)

        result1 = asyncio.run(handlers.dispatch("browse_to", {"url": "https://a.example.com"}, "agent1"))
        result2 = asyncio.run(handlers.dispatch("browse_to", {"url": "https://b.example.com"}, "agent2"))

        parsed_result1 = json.loads(result1)
        parsed_result2 = json.loads(result2)

        self.assertTrue(parsed_result1["extracted"])
        self.assertTrue(parsed_result2["extracted"])
        self.assertEqual(mock_browser_pool_class.get_browser.call_count, 2)

    def test_unknown_tool(self):
        """Test handling of unknown tools."""
        config = ParousiaConfig()
        handlers = SpatialToolHandlers(config, MagicMock(), MagicMock())
        result = asyncio.run(handlers.dispatch("unknown_tool", {}, "agent1"))
        parsed_result = json.loads(result)
        self.assertIn("error", parsed_result)

    @patch('parousia.spatial.tools.BrowserPoolManager')
    def test_browse_to_with_real_serializer_returns_json(self, mock_browser_pool_class):
        """Regression: a real serializer returns an SDOM object (not a dict), which
        must be serialized before dispatch() json.dumps it. This is the bug that
        surfaced as 'Object of type SDOM is not JSON serializable'."""
        from parousia.spatial.serializer import SpatialSerializer

        mock_browser = MagicMock()
        mock_page = MagicMock()
        mock_page.goto = AsyncMock(return_value=MagicMock(status=200))
        mock_page.content = AsyncMock(
            return_value=(
                "<html><head><title>Signup</title></head><body>"
                "<a href='/login'>Sign in</a>"
                "<form><input id='email-address' type='text'><button type='submit'>Join</button></form>"
                "</body></html>"
            )
        )
        mock_browser.page = mock_page
        mock_browser_pool_class.get_browser = AsyncMock(return_value=mock_browser)

        config = ParousiaConfig()
        handlers = SpatialToolHandlers(config, mock_browser_pool_class, SpatialSerializer())
        result = asyncio.run(handlers.dispatch("browse_to", {"url": "https://www.linkedin.com/signup"}, "tina"))

        parsed = json.loads(result)  # must not raise
        self.assertTrue(parsed["extracted"])
        self.assertEqual(parsed["url"], "https://www.linkedin.com/signup")
        self.assertIn("sdom", parsed)
        self.assertIn("interactive", parsed["sdom"])
        self.assertEqual(parsed["sdom"]["meta"]["url"], "https://www.linkedin.com/signup")


if __name__ == '__main__':
    unittest.main()
