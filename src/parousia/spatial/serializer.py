"""
Spatial Data Object (SDOM) Serializer.
Converts HTML to SDOM using BeautifulSoup4.
"""

from datetime import datetime, timezone
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from parousia.spatial.sdom_models import (
    SDOM, SdomMeta, InteractiveElement, Rect, ContentSection, 
    Form, Navigation, Context
)

class SpatialSerializer:
    """Converts HTML to Spatial Data Object (SDOM)."""
    
    def __init__(self):
        """Initialize the serializer with BeautifulSoup parser."""
        self.parser = 'html.parser'
    
    def to_sdom(self, html: str, url: str, status: int = 200) -> SDOM:
        """
        Convert HTML string to SDOM.
        
        Args:
            html: HTML content as string
            url: URL of the page
            status: HTTP status code
            
        Returns:
            SDOM object representing the page structure
        """
        soup = BeautifulSoup(html, self.parser)
        
        # Extract title
        title = soup.title.string if soup.title else ""
        
        # Get all interactive elements
        interactive_elements = []
        
        # Find all interactive elements: a, button, input, select, textarea, [role=checkbox], [role=radio]
        interactive_selectors = [
            'a[href]', 'button', 'input', 'select', 'textarea',
            '[role="checkbox"]', '[role="radio"]'
        ]
        
        # Collect all elements matching the selectors
        for selector in interactive_selectors:
            elements = soup.select(selector)
            interactive_elements.extend(elements)
        
        # Filter invisible elements BEFORE assigning IDs
        visible_elements = []
        for element in interactive_elements:
            if self._is_visible(element):
                visible_elements.append(element)
        interactive_elements = visible_elements
        
        # Assign IDs to interactive elements
        interactive_elements = self._assign_ids(interactive_elements, prefix="")
        
        # Cap at 200 elements
        if len(interactive_elements) > 200:
            interactive_elements = interactive_elements[:200]

        # Build interactive list for SDOM
        interactive_list = []
        element_count = 0

        for element in interactive_elements:
                
            element_type = self._get_element_type(element)
            
            # Get text content and truncate if needed
            text_content = ""
            if element_type == 'link':
                text_content = element.get_text(strip=True)
            elif element_type in ['button', 'input', 'select', 'checkbox', 'radio', 'textarea']:
                text_content = element.get('value', '') or element.get_text(strip=True)
            
            # Truncate text
            if len(text_content) > 120:
                text_content = self._truncate_text(text_content, 120)
            
            # Get attributes
            attributes = {}
            for key, value in element.attrs.items():
                attributes[key] = str(value) if isinstance(value, list) else value
            
            # Create interactive element
            interactive_element = InteractiveElement(
                id=element.get('id', ''),
                type=element_type,
                text=text_content if text_content else None,
                role=element.get('role'),
                attributes=attributes
            )
            
            interactive_list.append(interactive_element)
            element_count += 1
        
        # Build content sections
        content_sections = []
        
        # Find all headings and their subsequent paragraphs
        headings = soup.find_all(['h1', 'h2', 'h3', 'h4'])
        
        for heading in headings:
            # Get the text of the heading
            heading_text = heading.get_text(strip=True)
            
            # Collect content until next heading or end of document
            content = []
            sibling = heading.find_next_sibling()
            
            while sibling and sibling.name not in ['h1', 'h2', 'h3', 'h4']:
                if sibling.name == 'p':
                    content.append(sibling.get_text(strip=True))
                elif sibling.name in ['img', 'a']:
                    # Add simple image/link info
                    pass  # We'll handle this more thoroughly below
                sibling = sibling.find_next_sibling()
            
            # Join paragraphs with spaces
            text_content = " ".join(content).strip()
            
            if len(text_content) > 500:
                text_content = self._truncate_text(text_content, 500)
            
            section = ContentSection(
                heading=heading_text,
                level=int(heading.name[1]) if heading.name.startswith('h') else None,
                text=text_content
            )
            
            content_sections.append(section)
        
        # Detect forms
        forms = []
        form_elements = soup.find_all('form')
        
        for form in form_elements:
            form_id = form.get('id', f'form_{len(forms) + 1}')
            
            # Find form fields
            fields = []
            field_selectors = ['input', 'select', 'textarea']
            for selector in field_selectors:
                field_elements = form.select(selector)
                for field in field_elements:
                    field_id = field.get('id', '')
                    if field_id:
                        fields.append(field_id)
            
            # Find submit button
            submit_button = form.find('input', type='submit') or form.find('button', type='submit')
            submit_id = submit_button.get('id', None) if submit_button else None
            
            form_obj = Form(
                id=form_id,
                action=form.get('action'),
                method=form.get('method'),
                fields=fields,
                submit_id=submit_id
            )
            
            forms.append(form_obj)
        
        # Detect navigation
        main_nav = []
        breadcrumbs = []
        
        # Look for nav elements
        nav_elements = soup.find_all('nav')
        for nav in nav_elements:
            nav_links = nav.find_all('a', href=True)
            for link in nav_links:
                main_nav.append({
                    'text': link.get_text(strip=True),
                    'url': link.get('href', '')
                })
        
        # Look for breadcrumbs (common patterns)
        breadcrumb_selectors = [
            '.breadcrumb', '.breadcrumbs', '[aria-label="breadcrumb"]',
            '.path', '.trail'
        ]
        
        for selector in breadcrumb_selectors:
            breadcrumbs_elements = soup.select(selector)
            for element in breadcrumbs_elements:
                if element.name == 'nav':
                    # Handle nav with breadcrumbs
                    links = element.find_all('a')
                    breadcrumbs.extend([link.get_text(strip=True) for link in links])
                else:
                    # Handle other breadcrumb containers
                    links = element.find_all('a')
                    breadcrumbs.extend([link.get_text(strip=True) for link in links])
        
        # Get context information
        cookies_set = bool(soup.find('meta', attrs={'name': 'cookies'}))
        session_active = bool(soup.find('meta', attrs={'name': 'session'}))
        authenticated_as = None
        
        # Classify page type
        content_type = self._detect_page_type(soup, None)  # We'll pass SDOM when we have it
        
        # Create SDOM object
        sdom_meta = SdomMeta(
            url=url,
            status=status,
            title=title,
            loaded_at=datetime.now(timezone.utc).isoformat()
        )
        
        context = Context(
            cookies_set=cookies_set,
            session_active=session_active,
            authenticated_as=authenticated_as,
            content_type=content_type
        )
        
        navigation = Navigation(
            main_nav=main_nav,
            breadcrumbs=breadcrumbs
        )
        
        # Create SDOM with truncated interactive elements if needed
        final_interactive = interactive_list[:200] if len(interactive_list) > 200 else interactive_list
        
        sdom = SDOM(
            meta=sdom_meta,
            interactive=final_interactive,
            content=content_sections,
            forms=forms,
            navigation=navigation,
            context=context
        )
        
        # Update page type after SDOM creation
        sdom.context.content_type = self._detect_page_type(soup, sdom)
        
        return sdom
    
    def _assign_ids(self, elements: List, prefix: str) -> List:
        """
        Assign monotonic IDs to elements based on their type.
        
        Args:
            elements: List of BeautifulSoup elements
            prefix: Prefix for ID assignment (e.g., 'l' for links, 'i' for inputs)
            
        Returns:
            List of elements with IDs assigned
        """
        # Counters for each element type
        counters = {
            'link': 0,
            'button': 0,
            'input': 0,
            'select': 0,
            'checkbox': 0,
            'radio': 0,
            'textarea': 0,
            'form': 0
        }
        
        # Map element types to their prefixes
        type_mapping = {
            'a': 'link',
            'button': 'button', 
            'input': 'input',
            'select': 'select',
            'textarea': 'textarea',
            '[role="checkbox"]': 'checkbox',
            '[role="radio"]': 'radio'
        }
        
        for element in elements:
            # Determine element type
            element_type = None
            
            # Check for role attributes first
            if element.get('role') == 'checkbox':
                element_type = 'checkbox'
            elif element.get('role') == 'radio':
                element_type = 'radio'
            else:
                # Try to determine from tag name
                if element.name in type_mapping:
                    element_type = type_mapping[element.name]
                elif element.name == 'input':
                    input_type = element.get('type', 'text')
                    if input_type in ['checkbox', 'radio']:
                        element_type = input_type
                    else:
                        element_type = 'input'
            
            # If we have a valid type, assign a synthetic ID only when the
            # element has no usable id of its own. Real `id` attributes MUST be
            # preserved so `interact` can target the live DOM via `#<id>` — the
            # previous behaviour overwrote them (e.g. LinkedIn's `email-address`
            # became `i1`), leaving `interact` with nothing to select.
            if element_type and element_type in counters:
                if not element.get('id'):
                    counters[element_type] += 1
                    element['id'] = f"{element_type[0]}{counters[element_type]}"
        
        return elements
    
    def _truncate_text(self, text: str, max_chars: int) -> str:
        """
        Truncate text with ellipsis.
        
        Args:
            text: Text to truncate
            max_chars: Maximum number of characters
            
        Returns:
            Truncated text with ellipsis
        """
        if len(text) <= max_chars:
            return text
        return text[:max_chars-1] + "…"
    
    def _is_visible(self, element) -> bool:
        """
        Check if an element is visible (not hidden by CSS).
        
        Args:
            element: BeautifulSoup element
            
        Returns:
            True if element is visible, False otherwise
        """
        # Check for display:none or visibility:hidden
        style = element.get('style', '')
        if 'display:none' in style or 'visibility:hidden' in style:
            return False
        
        # Check for aria-hidden attribute
        if element.get('aria-hidden') == 'true':
            return False
        
        # Check parent elements for hidden styles
        parent = element.parent
        while parent:
            parent_style = parent.get('style', '')
            if 'display:none' in parent_style or 'visibility:hidden' in parent_style:
                return False
            parent = parent.parent
            
        return True
    
    def _get_element_type(self, element) -> str:
        """
        Determine the type of interactive element.
        
        Args:
            element: BeautifulSoup element
            
        Returns:
            Type string ('link', 'button', 'input', 'select', 'checkbox', 'radio', 'textarea')
        """
        if element.name == 'a':
            return 'link'
        elif element.name == 'button':
            return 'button'
        elif element.name == 'input':
            input_type = element.get('type', 'text')
            if input_type == 'checkbox':
                return 'checkbox'
            elif input_type == 'radio':
                return 'radio'
            else:
                return 'input'
        elif element.name == 'select':
            return 'select'
        elif element.name == 'textarea':
            return 'textarea'
        elif element.get('role') == 'checkbox':
            return 'checkbox'
        elif element.get('role') == 'radio':
            return 'radio'
        else:
            return 'input'  # Default fallback
    
    def _detect_page_type(self, soup, sdom: SDOM) -> str:
        """
        Classify the content type of the page.
        
        Args:
            soup: BeautifulSoup object
            sdom: SDOM object (optional)
            
        Returns:
            Content type string
        """
        # Check for login-related elements
        if soup.find('input', {'type': 'email'}) and soup.find('input', {'type': 'password'}):
            return 'login'
        
        # Check for search forms
        search_form = soup.find('form', {'role': 'search'}) or soup.find('input', {'type': 'search'})
        if search_form:
            return 'search_results'
        
        # Check for forms with specific actions
        forms = soup.find_all('form')
        for form in forms:
            action = form.get('action', '').lower()
            if any(keyword in action for keyword in ['login', 'auth']):
                return 'login'
        
        # Check for product-related content (common patterns)
        product_indicators = ['product', 'item', 'sku', 'price']
        for indicator in product_indicators:
            if soup.find(string=lambda text: text and indicator.lower() in text.lower()):
                return 'product'
        
        # Check for dashboard indicators
        dashboard_indicators = ['dashboard', 'admin', 'panel', 'control']
        for indicator in dashboard_indicators:
            if soup.find(string=lambda text: text and indicator.lower() in text.lower()):
                return 'dashboard'
        
        # Check for error page patterns
        error_patterns = ['error', '404', 'not found']
        for pattern in error_patterns:
            if soup.find(string=lambda text: text and pattern.lower() in text.lower()):
                return 'error'
        
        # Default to generic
        return 'generic'
    
    def estimate_tokens(self, sdom: SDOM) -> int:
        """
        Estimate token count of the SDOM.
        
        Args:
            sdom: SDOM object
            
        Returns:
            Estimated token count
        """
        import json
        json_str = sdom.model_dump_json()
        return len(json_str) // 4