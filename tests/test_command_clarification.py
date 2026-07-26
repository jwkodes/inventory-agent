"""Command-clarification model and repository contract tests."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx

from inventory_agent.extraction.clarification import (
    COMMAND_CLARIFICATION_INSTRUCTIONS,
    COMMAND_CLARIFICATION_PROMPT_VERSION,
    CommandClarificationView,
    OpenAICommandClarificationInterpreter,
    StoredCommandExtraction,
    SupabaseCommandClarificationRepository,
)
from inventory_agent.extraction.interpreter import CommandExtractionResult
from inventory_agent.extraction.schema import ExtractedInventoryCommand, InventoryIntent

ORGANIZATION_ID = UUID("10000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("11000000-0000-0000-0000-000000000001")
IMAGE_EVENT_ID = UUID("50000000-0000-0000-0000-000000000091")
REPLY_EVENT_ID = UUID("50000000-0000-0000-0000-000000000092")
REQUEST_ID = UUID("70000000-0000-0000-0000-000000000091")
PROPOSAL_ID = UUID("40000000-0000-0000-0000-000000000091")


def extraction(*, resolved: bool = False) -> CommandExtractionResult:
    return CommandExtractionResult(
        command=ExtractedInventoryCommand.model_validate(
            {
                "schema_version": "1.0",
                "intent": "RECEIVE_STOCK" if resolved else "UNKNOWN",
                "location_hint": None,
                "lines": [
                    {
                        "source_text": "ABC-123 3 boxes",
                        "item_reference": {
                            "type": "PART_NUMBER",
                            "value": "ABC-123",
                        },
                        "description": "Invoice Widget",
                        "quantity": "3",
                        "unit": "box",
                        "attributes": [],
                    }
                ],
                "notes": "invoice",
                "needs_clarification": not resolved,
                "clarification_question": (
                    None if resolved else "Should these lines be recorded as received stock?"
                ),
            }
        ),
        response_id="response-resolved" if resolved else "response-image",
        model="gpt-test",
        prompt_version=(
            COMMAND_CLARIFICATION_PROMPT_VERSION if resolved else "inventory-invoice-image-v1"
        ),
    )


def view() -> CommandClarificationView:
    source = extraction()
    return CommandClarificationView(
        request_id=REQUEST_ID,
        organization_id=ORGANIZATION_ID,
        requested_by=ACTOR_ID,
        chat_id=123,
        source_event_id=IMAGE_EVENT_ID,
        question=source.command.clarification_question or "",
        extraction=StoredCommandExtraction.from_result(source),
    )


class FakeResponses:
    def __init__(self, response: object) -> None:
        self.response = response
        self.arguments: dict[str, Any] = {}

    async def parse(self, **kwargs: Any) -> object:
        self.arguments = kwargs
        return self.response


class FakeOpenAI:
    def __init__(self, response: object) -> None:
        self.responses = FakeResponses(response)


async def test_command_clarification_preserves_invoice_lines_and_resolves_intent() -> None:
    resolved = extraction(resolved=True).command
    response = SimpleNamespace(
        output_parsed=resolved,
        output=[],
        id="response-resolved",
        model="gpt-test",
        usage=None,
    )
    client = FakeOpenAI(response)
    interpreter = OpenAICommandClarificationInterpreter(
        client=client,  # type: ignore[arg-type]
        model="gpt-test",
    )

    result = await interpreter.resolve(
        view=view(),
        user_reply="Yes, all received stock.",
    )

    assert result.command.intent is InventoryIntent.RECEIVE_STOCK
    assert result.command.lines[0].item_reference.value == "ABC-123"
    assert result.command.lines[0].quantity == "3"
    assert result.prompt_version == COMMAND_CLARIFICATION_PROMPT_VERSION
    payload = json.loads(client.responses.arguments["input"])
    assert payload["original_extraction"]["lines"][0]["description"] == "Invoice Widget"
    assert payload["current_reply"] == "Yes, all received stock."
    assert "Never replace the original lines" in COMMAND_CLARIFICATION_INSTRUCTIONS


async def test_supabase_command_clarification_repository_contract() -> None:
    requests: list[httpx.Request] = []
    source = extraction()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/find_pending_command_clarification"):
            return httpx.Response(200, json=str(REQUEST_ID))
        if request.url.path.endswith("/get_command_clarification_view"):
            return httpx.Response(200, json=view().model_dump(mode="json"))
        return httpx.Response(200, json=str(REQUEST_ID))

    repository = SupabaseCommandClarificationRepository(
        supabase_url="https://example.supabase.co",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )

    assert (
        await repository.begin(
            source_event_id=IMAGE_EVENT_ID,
            actor_id=ACTOR_ID,
            chat_id=123,
            question=source.command.clarification_question or "",
            extraction=source,
        )
        == REQUEST_ID
    )
    assert await repository.find_pending(actor_id=ACTOR_ID, chat_id=123) == REQUEST_ID
    loaded = await repository.get_view(request_id=REQUEST_ID)
    assert loaded.extraction.command.lines[0].item_reference.value == "ABC-123"
    assert (
        await repository.continue_request(
            request_id=REQUEST_ID,
            event_id=REPLY_EVENT_ID,
            actor_id=ACTOR_ID,
            user_reply="Maybe.",
            question="Should stock be added or removed?",
            extraction=source,
        )
        == REQUEST_ID
    )
    assert (
        await repository.resolve(
            request_id=REQUEST_ID,
            event_id=REPLY_EVENT_ID,
            actor_id=ACTOR_ID,
            user_reply="All received.",
            extraction=extraction(resolved=True),
            proposal_id=PROPOSAL_ID,
        )
        == REQUEST_ID
    )

    assert requests[0].url.path.endswith("/begin_command_clarification")
    begin_body = json.loads(requests[0].read())
    assert begin_body["p_extraction"]["command"]["lines"][0]["quantity"] == "3"
    assert requests[1].url.path.endswith("/find_pending_command_clarification")
    assert requests[2].url.path.endswith("/get_command_clarification_view")
    assert requests[3].url.path.endswith("/continue_command_clarification")
    assert requests[4].url.path.endswith("/resolve_command_clarification")
    resolve_body = json.loads(requests[4].read())
    assert resolve_body["p_proposal_id"] == str(PROPOSAL_ID)
