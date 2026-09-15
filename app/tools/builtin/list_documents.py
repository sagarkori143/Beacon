"""List the documents available to the caller.

Answers "what do you actually know about?" without a retrieval round trip, and
gives the model a vocabulary to search with when a first search comes back empty.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.repositories.document import list_documents
from app.tools.base import Tool, ToolContext, ToolDefinition, ToolResult


class ListDocumentsArgs(BaseModel):
    document_type: str | None = Field(
        default=None, description="Optional filter, e.g. 'policy' or 'handbook'.", max_length=64
    )
    limit: int = Field(default=25, ge=1, le=100)


class ListDocumentsTool(Tool):
    definition = ToolDefinition(
        name="list_documents",
        description=(
            "List the documents available for this organization and location, "
            "with their titles and scope. Use when you need to know what "
            "documentation exists, or when a search returned nothing."
        ),
        input_model=ListDocumentsArgs,
        scopes=frozenset({"knowledge:read"}),
        timeout_s=10.0,
    )

    async def execute(self, args: ListDocumentsArgs, context: ToolContext) -> ToolResult:
        async with context.uow.begin() as session:
            documents = await list_documents(
                session,
                context.tenant,
                location_id=context.tenant.location_id,
                document_type=args.document_type,
                limit=args.limit,
            )

        if not documents:
            return ToolResult.success("No documents are available.", data={"documents": []})

        lines = []
        payload = []
        for document in documents:
            scope = "location-specific" if document.location_id else "organization-wide"
            lines.append(
                f"- {document.title} ({scope}"
                f"{f', {document.document_type}' if document.document_type else ''})"
            )
            payload.append(
                {
                    "id": str(document.id),
                    "title": document.title,
                    "scope": scope,
                    "document_type": document.document_type,
                }
            )

        return ToolResult.success("\n".join(lines), data={"documents": payload})


TOOL = ListDocumentsTool()
