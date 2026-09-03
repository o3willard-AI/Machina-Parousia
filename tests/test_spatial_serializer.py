"""
Tests for Spatial Data Object (SDOM) Serializer.
"""

import pytest
from parousia.spatial.serializer import SpatialSerializer
from parousia.spatial.sdom_models import SDOM, SdomMeta, InteractiveElement, ContentSection, Form, Navigation, Context

# Test fixtures
SAMPLE_PAGE_HTML = """
<html>
<head><title>Sample Page</title></head>
<body>
    <nav>
        <a href="/home">Home</a>
        <a href="/about">About</a>
    </nav>
    <h1>Welcome to Sample Page</h1>
    <p>This is the first paragraph of the sample page.</p>
    <p>This is the second paragraph with a <a href="/link">link</a> inside it.</p>
    <a href="/contact">Contact Us</a>
    <input type="text" id="search" placeholder="Search...">
    <button type="submit">Submit</button>
    <img src="/image.jpg" alt="Sample Image">
</body>
</html>
"""

LOGIN_PAGE_HTML = """
<html>
<head><title>Login Page</title></head>
<body>
    <h1>Login</h1>
    <form id="login-form">
        <input type="email" id="email" placeholder="Email">
        <input type="password" id="password" placeholder="Password">
        <button type="submit">Login</button>
        <a href="/forgot-password">Forgot Password?</a>
    </form>
</body>
</html>
"""

SEARCH_RESULTS_HTML = """
<html>
<head><title>Search Results</title></head>
<body>
    <h1>Search Results</h1>
    <form role="search">
        <input type="search" id="search-input" placeholder="Search...">
        <button type="submit">Search</button>
    </form>
    <div class="results">
        <a href="/result1">Result 1</a>
        <a href="/result2">Result 2</a>
    </div>
    <nav class="pagination">
        <a href="/page/1">1</a>
        <a href="/page/2">2</a>
    </nav>
</body>
</html>
"""

def test_basic_sdom():
    """Test basic SDOM conversion with 1 link, 1 input, 1 button."""
    serializer = SpatialSerializer()
    
    html = """
    <html>
    <body>
        <a href="/link">Link Text</a>
        <input type="text">
        <button type="button">Button Text</button>
    </body>
    </html>
    """
    
    sdom = serializer.to_sdom(html, "http://example.com/test")
    
    # Check that we have the expected elements with correct IDs
    assert len(sdom.interactive) == 3
    interactive_ids = [elem.id for elem in sdom.interactive]
    assert 'l1' in interactive_ids
    assert 'i1' in interactive_ids
    assert 'b1' in interactive_ids

def test_id_convention():
    """Test ID convention with multiple elements."""
    serializer = SpatialSerializer()
    
    html = """
    <html>
    <body>
        <a href="/link1">Link 1</a>
        <a href="/link2">Link 2</a>
        <a href="/link3">Link 3</a>
        <input type="text">
        <input type="text">
    </body>
    </html>
    """
    
    sdom = serializer.to_sdom(html, "http://example.com/test")
    
    # Should have 5 elements with IDs l1,l2,l3,i1,i2
    assert len(sdom.interactive) == 5
    interactive_ids = [elem.id for elem in sdom.interactive]
    assert 'l1' in interactive_ids
    assert 'l2' in interactive_ids
    assert 'l3' in interactive_ids
    assert 'i1' in interactive_ids
    assert 'i2' in interactive_ids


def test_preserves_real_ids():
    """Real element ids are preserved; only id-less elements get synthetic ids."""
    serializer = SpatialSerializer()

    html = """
    <html>
    <body>
        <form id="signup">
            <input type="email" id="email-address" name="email-address">
            <input type="password" id="password">
            <input type="text">
            <button type="submit">Join</button>
        </form>
    </body>
    </html>
    """

    sdom = serializer.to_sdom(html, "http://example.com/signup")

    ids = [elem.id for elem in sdom.interactive]
    # Real ids are preserved (previously overwritten to i1/i2).
    assert "email-address" in ids
    assert "password" in ids
    # Id-less elements still get synthetic ids.
    assert "i1" in ids
    assert "b1" in ids


def test_truncation():
    """Test text truncation for elements with long text."""
    serializer = SpatialSerializer()
    
    html = """
    <html>
    <body>
        <button type="button">This is a very long button text that should be truncated because it exceeds the maximum allowed characters limit set for interactive elements</button>
    </body>
    </html>
    """
    
    sdom = serializer.to_sdom(html, "http://example.com/test")
    
    # Should truncate text to 120 chars + ellipsis
    assert len(sdom.interactive) == 1
    button_text = sdom.interactive[0].text
    assert len(button_text) <= 123  # 120 chars + "…"
    assert button_text.endswith("…")

def test_form_detection():
    """Test form detection and field identification."""
    serializer = SpatialSerializer()
    
    html = """
    <html>
    <body>
        <form id="login-form">
            <input type="email" id="email-field">
            <input type="password" id="password-field">
            <button type="submit">Login</button>
        </form>
    </body>
    </html>
    """
    
    sdom = serializer.to_sdom(html, "http://example.com/login")
    
    # Should detect one form with correct fields
    assert len(sdom.forms) == 1
    form = sdom.forms[0]
    assert form.id == "login-form"
    assert len(form.fields) == 2
    assert "email-field" in form.fields  # real id preserved
    assert "password-field" in form.fields  # real id preserved
    assert form.submit_id == "b1"

def test_content_sections():
    """Test content section grouping by heading hierarchy."""
    serializer = SpatialSerializer()
    
    html = """
    <html>
    <body>
        <h1>Main Title</h1>
        <p>First paragraph under main title.</p>
        <p>Second paragraph under main title.</p>
        <h2>Subsection Title</h2>
        <p>Paragraph under subsection.</p>
    </body>
    </html>
    """
    
    sdom = serializer.to_sdom(html, "http://example.com/test")
    
    # Should have 2 content sections
    assert len(sdom.content) == 2
    assert sdom.content[0].heading == "Main Title"
    assert sdom.content[1].heading == "Subsection Title"

def test_nav_detection():
    """Test navigation detection from nav elements."""
    serializer = SpatialSerializer()
    
    html = """
    <html>
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
    
    sdom = serializer.to_sdom(html, "http://example.com/test")
    
    # Should detect navigation with main_nav populated
    assert len(sdom.navigation.main_nav) == 3
    nav_links = [link['text'] for link in sdom.navigation.main_nav]
    assert 'Home' in nav_links
    assert 'About' in nav_links
    assert 'Contact' in nav_links

def test_invisible_filtered():
    """Test that invisible elements are filtered out."""
    serializer = SpatialSerializer()
    
    html = """
    <html>
    <body>
        <button style="display:none">Hidden Button</button>
        <a href="/visible">Visible Link</a>
        <button>Normal Button</button>
    </body>
    </html>
    """
    
    sdom = serializer.to_sdom(html, "http://example.com/test")
    
    # Should only have 2 visible elements
    interactive_ids = [elem.id for elem in sdom.interactive]
    assert len(sdom.interactive) == 2
    assert 'l1' in interactive_ids  # link
    assert 'b1' in interactive_ids  # button

def test_empty_page():
    """Test handling of empty HTML page."""
    serializer = SpatialSerializer()
    
    html = "<html><body></body></html>"
    
    sdom = serializer.to_sdom(html, "http://example.com/empty")
    
    # Should not crash and have empty arrays
    assert len(sdom.interactive) == 0
    assert len(sdom.content) == 0
    assert len(sdom.forms) == 0

def test_page_type_classification():
    """Test page type classification."""
    serializer = SpatialSerializer()
    
    sdom = serializer.to_sdom(LOGIN_PAGE_HTML, "http://example.com/login")
    
    # Should classify as login page
    assert sdom.context.content_type == 'login'

def test_token_estimate():
    """Test token estimation."""
    serializer = SpatialSerializer()
    
    sdom = serializer.to_sdom(SAMPLE_PAGE_HTML, "http://example.com/sample")
    
    # Should estimate tokens
    token_count = serializer.estimate_tokens(sdom)
    assert isinstance(token_count, int)
    assert token_count <= 1000

def test_max_elements():
    """Test that elements are capped at 200 with annotation."""
    serializer = SpatialSerializer()
    
    # Create HTML with more than 200 elements
    html_elements = []
    for i in range(250):
        html_elements.append(f'<button id="btn{i}">Button {i}</button>')
    
    html = "<html><body>" + "".join(html_elements) + "</body></html>"
    
    sdom = serializer.to_sdom(html, "http://example.com/test")
    
    # Should cap at 200 elements
    assert len(sdom.interactive) == 200

def test_models_validation():
    """Test that SDOM models validate correctly."""
    # Test basic instantiation
    meta = SdomMeta(url="http://example.com", status=200, title="Test", loaded_at="2023-01-01T00:00:00Z")
    context = Context(cookies_set=False, session_active=False, content_type='generic')
    navigation = Navigation()
    
    sdom = SDOM(
        meta=meta,
        interactive=[],
        content=[],
        forms=[],
        navigation=navigation,
        context=context
    )
    
    # Should not raise validation errors
    assert sdom.meta.url == "http://example.com"
    assert sdom.context.content_type == 'generic'