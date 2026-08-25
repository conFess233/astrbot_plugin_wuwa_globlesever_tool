"""AstrBot Dashboard 路由。"""

from .health import WebManager
from .routes import DashboardWebManager

__all__ = ["DashboardWebManager", "WebManager"]
