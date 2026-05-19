"""MCP server exposing the extractor to any non-CrewAI consumer.

Use cases:
- LangGraph nodes that don't speak CrewAI tool-calling.
- The future Buchungs-Agent if it's spun out into its own process.
- Hand-rolled scripts in customer support that need to re-extract.

The server speaks MCP over stdio (default) or SSE.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path
from typing import Any

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from putsch_docs.exceptions import ExtractionError
from putsch_docs.extractor import DoclingExtractor
from putsch_docs.observability import configure_logging, get_logger

log = get_logger(__name__)


_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Absolute path to invoice file (PDF, image, or XRechnung XML).",
        },
        "bytes_b64": {
            "type": "string",
            "description": "Base64-encoded file contents. Alternative to `path`.",
        },
        "document_id": {
            "type": "string",
            "description": "Correlation id for observability; minted if omitted.",
        },
    },
    "additionalProperties": False,
}


def _build_server(extractor: DoclingExtractor | None = None) -> Server:
    server: Server = Server("putsch-docs")
    ext = extractor or DoclingExtractor()

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name="extract_invoice",
                description=(
                    "Extract structured German Eingangsrechnung fields. "
                    "Returns InvoiceFields + confidence report + trace, or a typed error."
                ),
                inputSchema=_INPUT_SCHEMA,
            )
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        if name != "extract_invoice":
            return [TextContent(type="text", text=f"unknown tool: {name}")]

        path_arg = arguments.get("path")
        bytes_b64 = arguments.get("bytes_b64")
        doc_id = arguments.get("document_id")

        if (path_arg is None) == (bytes_b64 is None):
            return [
                TextContent(
                    type="text",
                    text='{"error_type":"InputValidationError",'
                    '"message":"exactly one of path or bytes_b64 required"}',
                )
            ]

        source: Path | bytes
        if path_arg is not None:
            source = Path(path_arg)
        else:
            try:
                assert bytes_b64 is not None
                source = base64.b64decode(bytes_b64, validate=True)
            except (ValueError, TypeError) as exc:
                return [
                    TextContent(
                        type="text",
                        text=f'{{"error_type":"InputValidationError","message":"{exc}"}}',
                    )
                ]

        try:
            result = await ext.extract(source, document_id=doc_id)
        except ExtractionError as exc:
            return [TextContent(type="text", text=str(exc.to_dict()))]
        return [TextContent(type="text", text=result.model_dump_json())]

    return server


async def _run_stdio() -> None:
    configure_logging()
    server = _build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="putsch-docs",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    """Entry point for `putsch-docs-mcp` console script."""
    try:
        asyncio.run(_run_stdio())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
