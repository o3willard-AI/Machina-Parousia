"""
Pydantic models for Spatial Data Object (SDOM) schema.
"""

from typing import Optional, List, Dict, Literal
from pydantic import BaseModel, Field


class Rect(BaseModel):
    """Rectangle coordinates for spatial positioning."""
    x: int
    y: int
    width: int
    height: int


class InteractiveElement(BaseModel):
    """Represents an interactive element in the web page."""
    id: str
    type: Literal['link', 'button', 'input', 'select', 'checkbox', 'radio', 'textarea']
    label: Optional[str] = None
    text: Optional[str] = None
    role: Optional[str] = None
    attributes: Dict[str, str] = Field(default_factory=dict)
    rect: Optional[Rect] = None


class ContentSection(BaseModel):
    """Represents a section of content grouped by heading hierarchy."""
    heading: Optional[str] = None
    level: Optional[int] = None  # 1-4 for h1-h4
    text: str
    images: List[Dict] = Field(default_factory=list)
    links: List[Dict] = Field(default_factory=list)


class Form(BaseModel):
    """Represents a form with its fields and submission information."""
    id: str
    action: Optional[str] = None
    method: Optional[str] = None
    fields: List[str] = Field(default_factory=list)
    submit_id: Optional[str] = None


class Navigation(BaseModel):
    """Represents navigation elements in the page."""
    main_nav: List[Dict] = Field(default_factory=list)
    breadcrumbs: List[str] = Field(default_factory=list)


class Context(BaseModel):
    """Represents context information about the page."""
    cookies_set: bool
    session_active: bool
    authenticated_as: Optional[str] = None
    content_type: Literal[
        'article', 'search_results', 'product', 'form', 'dashboard', 'error', 'login', 'generic'
    ]


class SdomMeta(BaseModel):
    """Metadata about the SDOM."""
    url: str
    status: int
    title: str
    loaded_at: str


class SDOM(BaseModel):
    """Spatial Data Object - complete representation of a web page."""
    meta: SdomMeta
    interactive: List[InteractiveElement] = Field(default_factory=list)
    content: List[ContentSection] = Field(default_factory=list)
    forms: List[Form] = Field(default_factory=list)
    navigation: Navigation = Field(default_factory=Navigation)
    context: Context