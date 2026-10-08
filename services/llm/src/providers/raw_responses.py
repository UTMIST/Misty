import base64
import json
import sys
from collections.abc import Mapping

from botocore.model import OperationModel, Shape

from contracts.tool_validation import MAX_TOOL_PAYLOAD_BYTES, validate_redacted_data
from src.providers.tool_blocks import validate_payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def decode_json_object(body: bytes) -> dict:
    if not isinstance(body, bytes) or len(body) > MAX_TOOL_PAYLOAD_BYTES:
        raise ValueError("invalid JSON response size")
    try:
        value = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, RecursionError):
        raise ValueError("invalid JSON response") from None
    if not isinstance(value, dict):
        raise ValueError("invalid JSON response object")
    validate_payload(value)
    return value


def _validate_shape(value, shape: Shape) -> None:
    kind = shape.type_name
    if kind == "structure" and shape.is_document_type:
        return
    measure = None
    if kind == "structure":
        if not isinstance(value, dict):
            raise ValueError("invalid response structure")
        members = {
            member.serialization.get("name", name): member for name, member in shape.members.items()
        }
        required = {
            shape.members[name].serialization.get("name", name) for name in shape.required_members
        }
        if shape.name == "ReasoningTextBlock" and "signature" in value:
            required.discard("text")
        if value.keys() - members.keys() or not required <= value.keys():
            raise ValueError("invalid response fields")
        if shape.is_tagged_union and len(value) != 1:
            raise ValueError("invalid response union")
        for name, item in value.items():
            _validate_shape(item, members[name])
    elif kind == "list":
        if not isinstance(value, list):
            raise ValueError("invalid response list")
        measure = len(value)
        for item in value:
            _validate_shape(item, shape.member)
    elif kind == "map":
        if not isinstance(value, dict):
            raise ValueError("invalid response map")
        measure = len(value)
        for key, item in value.items():
            _validate_shape(key, shape.key)
            _validate_shape(item, shape.value)
    elif kind in ("integer", "long"):
        if type(value) is not int:
            raise ValueError("invalid response integer")
        measure = value
    elif kind in ("double", "float"):
        if (
            type(value) not in (int, float)
            or not -sys.float_info.max <= value <= sys.float_info.max
        ):
            raise ValueError("invalid response number")
        measure = value
    elif kind == "boolean":
        if type(value) is not bool:
            raise ValueError("invalid response boolean")
    elif kind == "string":
        if not isinstance(value, str):
            raise ValueError("invalid response string")
        measure = len(value)
    elif kind == "blob":
        validate_redacted_data(value)
        measure = len(base64.b64decode(value, validate=True))
    else:
        raise ValueError("unsupported response shape")
    if measure is not None:
        if "min" in shape.metadata and measure < shape.metadata["min"]:
            raise ValueError("response value below its modeled limit")
        if "max" in shape.metadata and measure > shape.metadata["max"]:
            raise ValueError("response value exceeds its modeled limit")


def _error_code(value: str) -> str:
    return value.split(":", 1)[0].rsplit("#", 1)[-1]


def _validate_error(body: dict, headers: Mapping, status: int, operation: OperationModel) -> None:
    for field in ("__type", "message", "Message", "code", "Code"):
        if field in body and not isinstance(body[field], str):
            raise ValueError("invalid error response field")
    code = _error_code(body.get("__type", str(status)))
    query_error = headers.get("x-amzn-query-error", "").split(";")
    if len(query_error) == 2 and query_error[0]:
        code = query_error[0]
    override = headers.get("x-amzn-errortype", body.get("code", body.get("Code")))
    if override is not None:
        code = _error_code(override)
    shape = operation.service_model.shape_for_error_code(code)
    if shape is not None:
        modeled = {
            member.serialization.get("name", name): member for name, member in shape.members.items()
        }
        _validate_shape({key: item for key, item in body.items() if key in modeled}, shape)


def validate_converse_response(response: dict, operation: OperationModel) -> None:
    if not isinstance(response, dict) or not {"body", "headers", "status_code"} <= response.keys():
        raise ValueError("invalid HTTP response envelope")
    body = decode_json_object(response["body"])
    headers = response["headers"]
    if not isinstance(headers, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()
    ):
        raise ValueError("invalid HTTP response headers")
    status = response["status_code"]
    if type(status) is not int:
        raise ValueError("invalid HTTP response status")
    if status >= 301:
        _validate_error(body, headers, status, operation)
    elif 200 <= status < 300:
        _validate_shape(body, operation.output_shape)
    else:
        raise ValueError("unexpected HTTP response status")
