"""View modules, one per route group.

core.py holds the shared helpers (health, 404, _page_param); the rest are
the per-page modules. Collected here so config/urls.py can address every
view as views.<name>.
"""

from .core import _page_param, health, not_found
from .dashboard import dashboard
from .dataset import dataset
from .datasets import datasets
from .harvesters import harvester, harvesters
from .links import links
from .metadata import metadata_detail, metadata_overview
from .organisation import organisation
from .organisations import organisations
from .reports import report
from .reviews import reviews
from .series import series_detail, series_list
from .suggestions import suggestions

__all__ = [
    "_page_param",
    "dashboard",
    "dataset",
    "datasets",
    "harvester",
    "harvesters",
    "health",
    "links",
    "metadata_detail",
    "metadata_overview",
    "not_found",
    "organisation",
    "organisations",
    "report",
    "reviews",
    "series_detail",
    "series_list",
    "suggestions",
]
