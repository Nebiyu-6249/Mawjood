"""BSP selection.

One place that maps a configured provider name to an adapter. Everything else
depends on the ``BSPAdapter`` protocol and never names a vendor.
"""

from __future__ import annotations

from mawjood.config import Settings
from mawjood.services.bsp.base import BSPAdapter
from mawjood.services.bsp.console import ConsoleBSP
from mawjood.services.bsp.wati import WatiBSP
from mawjood.services.bsp.whatsapp_cloud import WhatsAppCloudBSP


def build_bsp(settings: Settings) -> BSPAdapter:
    """Construct the configured provider."""
    if settings.bsp_provider == "console":
        return ConsoleBSP()
    if settings.bsp_provider == "wati":
        return WatiBSP()
    return WhatsAppCloudBSP(
        api_base=settings.bsp_api_base,
        api_key=settings.bsp_api_key.get_secret_value() if settings.bsp_api_key else None,
    )


__all__ = ["build_bsp"]
