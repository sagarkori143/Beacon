"""Read a specific document section in full, and list available documents.

Retrieval returns fragments ranked by relevance. Sometimes the model needs the
whole of one section instead -- "what else does the cancellation policy say?" --
and re-searching for it returns the same fragment it already has.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from app.repositories.chunk import get_chunks, get_section
from app.tools.base import Tool, ToolContext, ToolDefinition, ToolResult


class DocumentLookupArgs(BaseModel):
    chunk_id: str | None = Field(
        default=None,
        description="A chunk id from an earlier search result, to read its full section.",
    )
    document_id: str | None = Field(
        default=None, description="Document id, when used together with section_key."
    )
    section_key: str | None = Field(
        default=None, description="Section key from an earlier search result."
    )


class DocumentLookupTool(Tool):
    definition = ToolDefinition(
        name="document_lookup",
        description=(
            "Read a full document section, given a chunk id from a previous "
            "search result. Use when a retrieved passage is clearly part of "
            "something longer and you need the rest of it."
        ),
        input_model=DocumentLookupArgs,
        scopes=frozenset({"knowledge:read"}),
        timeout_s=15.0,
    )

    async def execute(self, args: DocumentLookupArgs, context: ToolContext) -> ToolResult:
        document_id = args.document_id
        section_key = args.section_key

        async with context.uow.begin() as session:
            if args.chunk_id:
                try:
                    chunk_uuid = UUID(args.chunk_id)
                except ValueError:
                    return ToolResult.failure(
                        "invalid_arguments", f"'{args.chunk_id}' is not a valid chunk id."
                    )
                # Scoped to the caller's organization: the id came from model
                # output, so it is not trusted to be one they may read.
                found = await get_chunks(session, context.tenant, [chunk_uuid])
                if not found:
                    return ToolResult.failure("not_found", "No such passage is available.")
                document_id = str(found[0].document_id)
                section_key = found[0].section_key

            if not document_id or not section_key:
                return ToolResult.failure(
                    "invalid_arguments",
                    "Provide either chunk_id, or both document_id and section_key.",
                )

            try:
                document_uuid = UUID(document_id)
            except ValueError:
                return ToolResult.failure(
                    "invalid_arguments", f"'{document_id}' is not a valid document id."
                )

            chunks = await get_section(
                session,
                context.tenant,
                document_id=document_uuid,
                section_key=section_key,
            )

        if not chunks:
            return ToolResult.failure("not_found", "That section is not available.")

        first = chunks[0]
        body = "\n\n".join(chunk.content for chunk in chunks)
        return ToolResult.success(
            f"{first.source_name} - {first.breadcrumb or first.heading or 'section'}\n\n{body}",
            data={
                "document_id": str(first.document_id),
                "section": first.breadcrumb,
                "chunks": len(chunks),
                "version": first.document_version,
            },
        )


TOOL = DocumentLookupTool()
