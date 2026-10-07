"""JSON-object transport with local action validation for schema-incompatible gateways."""

import asyncio
from pathlib import Path
import sys

from .store import read_json
from .structured_proposer import StructuredActionEvolver


class JsonActionEvolver(StructuredActionEvolver):
    transport_name = "json_object_actions"
    response_format = {"type": "json_object"}


if __name__ == "__main__":
    asyncio.run(JsonActionEvolver(read_json(Path(sys.argv[1]))).run())
