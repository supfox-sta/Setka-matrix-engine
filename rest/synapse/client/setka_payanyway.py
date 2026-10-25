#
# Setka Plus PayAnyWay public endpoints — mounted outside /_matrix/client.
#

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from twisted.web.resource import Resource

from synapse.http.server import JsonResource
from synapse.rest.client.setka_plus import (
    SetkaPayAnyWayAppReturnServlet,
    SetkaPayAnyWayBrowserReturnServlet,
    SetkaPayAnyWayCheckoutServlet,
    SetkaPlusPayAnyWayCheckServlet,
    SetkaPlusPayAnyWayPayServlet,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer

_PUBLIC_PATHS = (
    "/setka/payanyway/pay",
    "/setka/payanyway/check",
    "/setka/payanyway/checkout",
    "/setka/payanyway/success",
    "/setka/payanyway/fail",
    "/setka/payanyway/return/success",
    "/setka/payanyway/return/fail",
)


def _register_public_servlets(hs: "HomeServer", http_server: JsonResource) -> None:
    SetkaPlusPayAnyWayPayServlet(hs).register(http_server)
    SetkaPlusPayAnyWayCheckServlet(hs).register(http_server)
    SetkaPayAnyWayCheckoutServlet(hs).register(http_server)
    SetkaPayAnyWayBrowserReturnServlet(hs, success=True).register(http_server)
    SetkaPayAnyWayBrowserReturnServlet(hs, success=False).register(http_server)
    SetkaPayAnyWayAppReturnServlet(hs, success=True).register(http_server)
    SetkaPayAnyWayAppReturnServlet(hs, success=False).register(http_server)


def build_setka_payanyway_resources(hs: "HomeServer") -> Mapping[str, Resource]:
    """Mount PayAnyWay webhooks on the HTTP root (not under /_matrix/client)."""
    resource = JsonResource(hs, canonical_json=False)
    _register_public_servlets(hs, resource)
    return {path: resource for path in _PUBLIC_PATHS}
