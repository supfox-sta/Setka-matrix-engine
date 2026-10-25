#
# Setka Plus Robokassa public endpoints — mounted outside /_matrix/client.
#

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from twisted.web.resource import Resource

from synapse.http.server import JsonResource
from synapse.rest.client.setka_plus import (
    SetkaPlusRobokassaResultServlet,
    SetkaPlusYooMoneyWebhookServlet,
    SetkaRobokassaAppReturnServlet,
    SetkaRobokassaBrowserReturnServlet,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer

_PUBLIC_PATHS = (
    "/setka/robokassa/result",
    "/setka/robokassa/success",
    "/setka/robokassa/fail",
    "/setka/robokassa/return/success",
    "/setka/robokassa/return/fail",
    "/_synapse/client/setka_plus/payments/robokassa/result",
    "/_synapse/client/setka_plus/payments/yoomoney/webhook",
)


def _register_public_servlets(hs: "HomeServer", http_server: JsonResource) -> None:
    SetkaPlusRobokassaResultServlet(hs).register(http_server)
    SetkaRobokassaBrowserReturnServlet(hs, success=True).register(http_server)
    SetkaRobokassaBrowserReturnServlet(hs, success=False).register(http_server)
    SetkaRobokassaAppReturnServlet(hs, success=True).register(http_server)
    SetkaRobokassaAppReturnServlet(hs, success=False).register(http_server)
    SetkaPlusYooMoneyWebhookServlet(hs).register(http_server)


def build_setka_robokassa_resources(hs: "HomeServer") -> Mapping[str, Resource]:
    """Mount Robokassa/YooMoney webhooks on the HTTP root (not under /_matrix/client)."""
    resource = JsonResource(hs, canonical_json=False)
    _register_public_servlets(hs, resource)
    return {path: resource for path in _PUBLIC_PATHS}
