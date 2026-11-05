"""Describe the outage response shared by every operation behind the gate."""
from copy import deepcopy


def describe_maintenance(result, generator, request, public):
    """Document actual middleware output without inventing business errors."""
    result.setdefault("components", {}).setdefault("schemas", {})["ServiceUnavailable"] = {
        "type": "object", "additionalProperties": False,
        "required": ["code", "detail"],
        "properties": {
            "code": {"type": "string", "enum": ["service_unavailable"]},
            "detail": {"type": "object", "additionalProperties": False,
                       "required": ["fr", "en"], "properties": {
                           "fr": {"type": "string"}, "en": {"type": "string"}}},
        },
    }
    response = {
        "description": (
            "FR : Service temporairement indisponible avant authentification : "
            "maintenance ou contrôle externe indisponible. Aucun traitement métier. "
            "Réessayer après le délai indiqué, en conservant l'UUID de l'opération. "
            "EN: Temporarily unavailable before authentication: maintenance or "
            "unavailable external gate. No business processing. Retry after the "
            "indicated delay, keeping the operation UUID."
        ),
        "headers": {
            "Retry-After": {"description": "Délai en secondes / Delay in seconds",
                            "schema": {"type": "integer", "minimum": 0}, "example": 30},
            "Cache-Control": {"schema": {"type": "string", "enum": ["no-store"]}},
        },
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ServiceUnavailable"}}},
    }
    methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    for item in result.get("paths", {}).values():
        for method, operation in item.items():
            if method in methods:
                responses=operation.setdefault("responses", {})
                if "503" not in responses:
                    responses["503"]=deepcopy(response)
                    continue
                # A view can also fail after authentication with its own 503.
                # Keep that contract and the middleware's distinct wire shape.
                existing=responses["503"]
                for name,value in response["headers"].items():
                    existing.setdefault("headers",{}).setdefault(name,deepcopy(value))
                body=existing.setdefault("content",{}).setdefault("application/json",{})
                current=body.get("schema")
                outage=response["content"]["application/json"]["schema"]
                if current is None:
                    body["schema"]=deepcopy(outage)
                elif current!=outage and outage not in current.get("oneOf",[]):
                    body["schema"]={"oneOf":[current,deepcopy(outage)]}
                if response["description"] not in existing.get("description",""):
                    existing["description"]=existing.get("description","")+"\n"+response["description"]
    return result
