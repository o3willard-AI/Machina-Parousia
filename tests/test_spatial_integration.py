"""
Integration tests for Spatial Data Object (SDOM) Serializer.
"""

import pytest
import os
from parousia.spatial.serializer import SpatialSerializer
from parousia.spatial.sdom_models import SDOM, SdomMeta, InteractiveElement, ContentSection, Form, Navigation, Context

# Test fixtures - inline HTML strings following the pattern in test_spatial_serializer.py

TEST_PAGE_HTML = """
<html>
<head><title>Multi-Page Site</title></head>
<body>
    <nav>
        <a href="page2.html">Page 2</a>
        <a href="form.html">Form</a>
    </nav>
    <h1>Welcome</h1>
    <p>This is the home page with some content and a <a href="https://example.com">link</a>.</p>
</body>
</html>
"""

LOGIN_HTML = """
<html>
<head><title>Login Page</title></head>
<body>
    <h1>Login</h1>
    <form id="login-form">
        <input type="email" id="email" placeholder="Email">
        <input type="password" id="password" placeholder="Password">
        <button type="submit">Login</button>
    </form>
</body>
</html>
"""

NAV_HTML = """
<html>
<head><title>Navigation Test</title></head>
<body>
    <nav>
        <a href="/home">Home</a>
        <a href="/about">About</a>
        <a href="/contact">Contact</a>
    </nav>
    <h1>Page Title</h1>
    <p>Page content.</p>
</body>
</html>
"""

ERROR_HTML = """
<html>
<head><title>Error 404</title></head>
<body>
    <h1>404 Not Found</h1>
    <p>The requested page could not be found.</p>
</body>
</html>
"""

INVISIBLE_HTML = """
<html>
<head><title>Invisible Elements</title></head>
<body>
    <button>Visible Button</button>
    <button style="display:none">Hidden Button</button>
</body>
</html>
"""

def test_browse_to_and_extract_flow():
    """Test browsing to a page and extracting content with navigation and links."""
    serializer = SpatialSerializer()
    
    sdom = serializer.to_sdom(TEST_PAGE_HTML, "http://example.com/index.html")
    
    # Assert interactive elements count > 0
    assert len(sdom.interactive) > 0
    
    # Assert content sections exist
    assert len(sdom.content) > 0
    
    # Assert SDOM meta.url matches
    assert sdom.meta.url == "http://example.com/index.html"


def test_form_detection_and_fields():
    """Test form detection and field identification with username/password fields."""
    serializer = SpatialSerializer()
    
    sdom = serializer.to_sdom(LOGIN_HTML, "http://example.com/login")
    
    # Assert form detected
    assert len(sdom.forms) == 1
    
    # Assert fields list not empty
    form = sdom.forms[0]
    assert len(form.fields) > 0


def test_navigation_detection():
    """Test navigation detection from nav elements with multiple links."""
    serializer = SpatialSerializer()
    
    sdom = serializer.to_sdom(NAV_HTML, "http://example.com/test")
    
    # Assert navigation.main_nav has expected links
    assert len(sdom.navigation.main_nav) >= 2
    nav_links = [link['text'] for link in sdom.navigation.main_nav]
    assert 'Home' in nav_links
    assert 'About' in nav_links
    assert 'Contact' in nav_links


def test_error_page_handling():
    """Test handling of error pages with specific status codes."""
    serializer = SpatialSerializer()
    
    sdom = serializer.to_sdom(ERROR_HTML, "http://example.com/error", status=404)
    
    # Assert SDOM meta.status == 404
    assert sdom.meta.status == 404


def test_invisible_elements_filtered():
    """Test that invisible elements are filtered out."""
    serializer = SpatialSerializer()
    
    sdom = serializer.to_sdom(INVISIBLE_HTML, "http://example.com/invisible")
    
    # Assert only visible button in interactive[]
    assert len(sdom.interactive) == 1
    assert sdom.interactive[0].text == "Visible Button"


def test_empty_and_minimal_pages():
    """Test handling of empty and minimal HTML pages."""
    serializer = SpatialSerializer()
    
    # Test empty string
    sdom_empty = serializer.to_sdom("", "http://example.com/empty")
    
    # Should have empty arrays, no crash
    assert len(sdom_empty.interactive) == 0
    assert len(sdom_empty.content) == 0
    assert len(sdom_empty.forms) == 0
    
    # Test minimal HTML
    sdom_minimal = serializer.to_sdom("<html><body></body></html>", "http://example.com/minimal")
    
    # Should have empty arrays, no crash
    assert len(sdom_minimal.interactive) == 0
    assert len(sdom_minimal.content) == 0
    assert len(sdom_minimal.forms) == 0

SEARCH_HTML = """
<html>
<head><title>Search Results</title></head>
<body>
    <form id="search-form">
        <input type="search" id="search-input" placeholder="Search">
        <button type="submit">Search</button>
    </form>
</body>
</html>
"""

LOGIN_HTML = """
<html>
<head><title>Login Page</title></head>
<body>
    <h1>Login</h1>
    <form id="login-form">
        <input type="email" id="email" placeholder="Email">
        <input type="password" id="password" placeholder="Password">
        <button type="submit">Login</button>
    </form>
</body>
</html>
"""

ARTICLE_HTML = """
<html>
<head><title>Article Page</title></head>
<body>
    <h1>Article Title</h1>
    <p>This is the article content.</p>
    <p>More content here.</p>
</body>
</html>
"""

BIG_HTML = """
<html>
<head><title>Big Page</title></head>
<body>
    <h1>Big Page with Many Buttons</h1>
    <p>Content before buttons.</p>
    {}
    <p>Content after buttons.</p>
</body>
</html>
"""

def test_page_type_classification():
    """Test that page types are correctly classified based on content."""
    serializer = SpatialSerializer()
    
    # Test search results page
    sdom_search = serializer.to_sdom(SEARCH_HTML, "http://example.com/search")
    assert sdom_search.context.content_type == "search_results"
    
    # Test login page
    sdom_login = serializer.to_sdom(LOGIN_HTML, "http://example.com/login")
    assert sdom_login.context.content_type == "login"
    
    # Test article page (generic)
    sdom_article = serializer.to_sdom(ARTICLE_HTML, "http://example.com/article")
    assert sdom_article.context.content_type == "generic"


def test_compression_and_truncation():
    """Test that large pages are compressed and truncated to reasonable limits."""
    # Build a big HTML with 250+ buttons
    buttons_html = ""
    for i in range(250):
        buttons_html += f'<button id="btn-{i}">Button {i}</button>\n'
    
    big_html = BIG_HTML.format(buttons_html)
    
    serializer = SpatialSerializer()
    sdom = serializer.to_sdom(big_html, "http://example.com/big")
    
    # Assert interactive elements are capped at 200
    assert len(sdom.interactive) <= 200
    
    # Assert estimate_tokens returns numeric value > 0
    token_count = serializer.estimate_tokens(sdom)
    assert isinstance(token_count, (int, float))
    assert token_count > 0


def test_agent_isolation_in_sdom():
    """Test that different pages produce SDOMs with isolated metadata."""
    serializer = SpatialSerializer()
    
    # Two different HTML pages with different URLs
    page1_html = """
    <html>
    <head><title>Page One</title></head>
    <body>
        <h1>Content of Page One</h1>
    </body>
    </html>
    """
    
    page2_html = """
    <html>
    <head><title>Page Two</title></head>
    <body>
        <h1>Content of Page Two</h1>
    </body>
    </html>
    """
    
    sdom1 = serializer.to_sdom(page1_html, "http://example.com/page1")
    sdom2 = serializer.to_sdom(page2_html, "http://example.com/page2")
    
    # Assert different meta.url
    assert sdom1.meta.url != sdom2.meta.url
    
    # Assert different meta.title
    assert sdom1.meta.title != sdom2.meta.title


def test_multi_page_navigation_flow():
    """Test multi-page navigation flow with fixture files."""
    serializer = SpatialSerializer()
    
    # Read the 3 fixture files from tests/fixtures/multi_page_site/
    base_path = "tests/fixtures/multi_page_site/"
    
    index_html = open(os.path.join(base_path, "index.html")).read()
    page2_html = open(os.path.join(base_path, "page2.html")).read()
    form_html = open(os.path.join(base_path, "form.html")).read()
    
    # Serialize each
    sdom_index = serializer.to_sdom(index_html, "http://example.com/index.html")
    sdom_page2 = serializer.to_sdom(page2_html, "http://example.com/page2.html")
    sdom_form = serializer.to_sdom(form_html, "http://example.com/form.html")
    
    # Assert all 3 produce valid SDOM objects
    assert isinstance(sdom_index, SDOM)
    assert isinstance(sdom_page2, SDOM)
    assert isinstance(sdom_form, SDOM)
    
    # Assert titles are correct
    assert sdom_index.meta.title == "Multi-Page Site"
    assert sdom_page2.meta.title == "Page Two"
    assert sdom_form.meta.title == "Contact Form"


def test_full_regression_module_imports():
    """Test that all spatial modules and key classes can be imported successfully."""
    # Import all spatial modules
    from parousia.spatial import sdom_models, serializer, browser_pool, tools
    
    # Import all key classes
    from parousia.spatial.sdom_models import SDOM, SdomMeta
    from parousia.spatial.serializer import SpatialSerializer
    from parousia.spatial.browser_pool import BrowserPoolManager
    from parousia.spatial.tools import SpatialToolHandlers
    
    # Test that imports succeed — the import statements above are the test
    assert SDOM is not None
    assert SdomMeta is not None
    assert SpatialSerializer is not None
    assert BrowserPoolManager is not None
    assert SpatialToolHandlers is not None