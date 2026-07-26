"""Server-side diagnostic and safe-configuration models for the development dashboard."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from inventory_agent.telegram.callbacks import decode_callback


class DashboardRepository:
    """Read organization-scoped diagnostics and call allowlisted settings RPCs."""

    def __init__(
        self,
        *,
        supabase_url: str,
        secret_key: str,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._rest_url = f"{supabase_url.rstrip('/')}/rest/v1"
        self._headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def list_organizations(self) -> list[dict[str, object]]:
        return await self._get(
            "organizations",
            {"select": "id,name,slug,inventory_profile", "order": "name.asc"},
        )

    async def list_events(
        self,
        *,
        organization_id: UUID,
        limit: int,
    ) -> list[dict[str, object]]:
        rows = await self._get(
            "source_events",
            {
                "select": (
                    "id,organization_id,external_event_id,event_type,status,error_message,"
                    "processing_attempts,received_at,processed_at,payload"
                ),
                "organization_id": f"eq.{organization_id}",
                "order": "received_at.desc",
                "limit": str(limit),
            },
        )
        return [
            {
                **row,
                "summary": _event_summary(row.get("payload")),
                "callback": _callback_details(row.get("payload")),
                "telegram_message_id": _nested(row.get("payload"), "message", "message_id")
                or _nested(row.get("payload"), "callback_query", "message", "message_id"),
            }
            for row in rows
        ]

    async def get_flow(self, *, event_id: UUID) -> dict[str, object] | None:
        events = await self._get(
            "source_events",
            {"select": "*", "id": f"eq.{event_id}", "limit": "1"},
        )
        if not events:
            return None
        event = events[0]
        organization_id = str(event["organization_id"])
        payload = event.get("payload")
        chat_id = _nested(payload, "message", "chat", "id") or _nested(
            payload, "callback_query", "message", "chat", "id"
        )

        proposals = await self._get(
            "transaction_proposals",
            {
                "select": "*,proposal_lines(*)",
                "source_event_id": f"eq.{event_id}",
                "order": "created_at.asc",
            },
        )
        outbox = await self._get(
            "processing_outbox",
            {
                "select": "*",
                "source_event_id": f"eq.{event_id}",
                "order": "created_at.asc",
            },
        )
        artifacts = await self._get(
            "source_artifacts",
            {
                "select": (
                    "id,source_event_id,media_type,storage_bucket,storage_path,sha256,"
                    "transcript,metadata,created_at"
                ),
                "source_event_id": f"eq.{event_id}",
                "order": "created_at.asc",
            },
        )
        conversations: list[dict[str, object]] = []
        conversation_turns: list[dict[str, object]] = []
        if chat_id is not None:
            conversations = await self._get(
                "inventory_agent_conversations",
                {
                    "select": "*",
                    "organization_id": f"eq.{organization_id}",
                    "chat_id": f"eq.{chat_id}",
                    "order": "updated_at.desc",
                    "limit": "1",
                },
            )
            if conversations:
                conversation_turns = await self._get(
                    "inventory_agent_turns",
                    {
                        "select": (
                            "id,source_event_id,history,estimated_tokens,input_tokens,"
                            "output_tokens,total_tokens,created_at,compacted_at,"
                            "compaction_policy"
                        ),
                        "conversation_id": f"eq.{conversations[0]['id']}",
                        "order": "created_at.desc",
                        "limit": "200",
                    },
                )

        proposal_ids = [str(proposal["id"]) for proposal in proposals]
        proposal_ids.extend(
            str(outcome["aggregate_id"])
            for outcome in outbox
            if outcome.get("outcome_type") == "proposal_ready"
            and outcome.get("aggregate_id") is not None
            and str(outcome["aggregate_id"]) not in proposal_ids
        )
        if proposal_ids and not proposals:
            proposals = await self._get(
                "transaction_proposals",
                {
                    "select": "*,proposal_lines(*)",
                    "id": f"in.({','.join(proposal_ids)})",
                    "order": "created_at.asc",
                },
            )
        line_ids = [
            str(line["id"])
            for proposal in proposals
            for line in _dict_list(proposal.get("proposal_lines"))
        ]
        catalog_requests = await self._get_for_ids(
            "catalog_item_creation_requests",
            "proposal_line_id",
            line_ids,
        )
        catalog_request_ids = [
            str(outcome["aggregate_id"])
            for outcome in outbox
            if outcome.get("outcome_type")
            in {"catalog_item_details_required", "catalog_item_confirmation"}
            and outcome.get("aggregate_id") is not None
        ]
        known_catalog_ids = {str(request["id"]) for request in catalog_requests}
        missing_catalog_ids = [
            request_id for request_id in catalog_request_ids if request_id not in known_catalog_ids
        ]
        if missing_catalog_ids:
            catalog_requests.extend(
                await self._get_for_ids(
                    "catalog_item_creation_requests",
                    "id",
                    missing_catalog_ids,
                )
            )
        clarifications = await self._get_for_ids(
            "match_clarification_requests",
            "proposal_line_id",
            line_ids,
        )
        command_clarifications = await self._get(
            "command_clarification_requests",
            {
                "select": "*",
                "or": (f"(source_event_id.eq.{event_id},last_source_event_id.eq.{event_id})"),
                "order": "created_at.asc",
            },
        )
        transactions = await self._get_for_ids(
            "inventory_transactions",
            "proposal_id",
            proposal_ids,
        )
        transaction_ids_from_outbox = [
            str(outcome["aggregate_id"])
            for outcome in outbox
            if outcome.get("outcome_type") == "transaction_applied"
            and outcome.get("aggregate_id") is not None
        ]
        known_transaction_ids = {str(transaction["id"]) for transaction in transactions}
        missing_transaction_ids = [
            transaction_id
            for transaction_id in transaction_ids_from_outbox
            if transaction_id not in known_transaction_ids
        ]
        if missing_transaction_ids:
            transactions.extend(
                await self._get_for_ids(
                    "inventory_transactions",
                    "id",
                    missing_transaction_ids,
                )
            )
        transaction_ids = [str(transaction["id"]) for transaction in transactions]
        transaction_lines = await self._get_for_ids(
            "transaction_lines",
            "transaction_id",
            transaction_ids,
        )

        return {
            "event": {
                **event,
                "summary": _event_summary(payload),
                "callback": _callback_details(payload),
            },
            "conversation": conversations[0] if conversations else None,
            "conversation_turns": conversation_turns,
            "proposals": proposals,
            "outbox": outbox,
            "artifacts": artifacts,
            "catalog_requests": catalog_requests,
            "clarifications": clarifications,
            "command_clarifications": command_clarifications,
            "transactions": transactions,
            "transaction_lines": transaction_lines,
        }

    async def get_inventory(self, *, organization_id: UUID) -> dict[str, object]:
        organization_filter = f"eq.{organization_id}"
        items = await self._get(
            "items",
            {
                "select": "*",
                "organization_id": organization_filter,
                "order": "name.asc",
            },
        )
        variants = await self._get(
            "item_variants",
            {
                "select": "*",
                "organization_id": organization_filter,
                "order": "sku.asc",
            },
        )
        balances = await self._get(
            "inventory_balances",
            {
                "select": "*",
                "organization_id": organization_filter,
                "order": "updated_at.desc",
            },
        )
        locations = await self._get(
            "locations",
            {
                "select": "id,code,name,active,attributes",
                "organization_id": organization_filter,
                "order": "name.asc",
            },
        )
        conversions = await self._get(
            "item_unit_conversions",
            {
                "select": "item_variant_id,from_unit,factor_to_base",
                "organization_id": organization_filter,
                "order": "from_unit.asc",
            },
        )
        transactions = await self._get(
            "inventory_transactions",
            {
                "select": "*",
                "organization_id": organization_filter,
                "order": "applied_at.desc",
                "limit": "50",
            },
        )
        resets = await self._get(
            "organization_data_resets",
            {
                "select": "id,requested_by,reset_scope,deleted_counts,created_at",
                "organization_id": organization_filter,
                "order": "created_at.desc",
                "limit": "10",
            },
        )
        transaction_ids = [str(transaction["id"]) for transaction in transactions]
        transaction_lines = await self._get_for_ids(
            "transaction_lines",
            "transaction_id",
            transaction_ids,
        )

        items_by_id = {str(item["id"]): item for item in items}
        locations_by_id = {str(location["id"]): location for location in locations}
        balances_by_variant: dict[str, list[dict[str, object]]] = {}
        for balance in balances:
            variant_id = str(balance["item_variant_id"])
            balances_by_variant.setdefault(variant_id, []).append(
                {
                    **balance,
                    "location": locations_by_id.get(str(balance["location_id"])),
                }
            )
        conversions_by_variant: dict[str, list[dict[str, object]]] = {}
        for conversion in conversions:
            conversions_by_variant.setdefault(str(conversion["item_variant_id"]), []).append(
                conversion
            )

        inventory_rows = []
        total_on_hand = 0.0
        for variant in variants:
            variant_id = str(variant["id"])
            variant_balances = balances_by_variant.get(variant_id, [])
            on_hand = sum(_as_float(balance.get("quantity")) for balance in variant_balances)
            total_on_hand += on_hand
            inventory_rows.append(
                {
                    **variant,
                    "item": items_by_id.get(str(variant["item_id"])),
                    "balances": variant_balances,
                    "conversions": conversions_by_variant.get(variant_id, []),
                    "on_hand": on_hand,
                }
            )

        return {
            "metrics": {
                "active_skus": sum(1 for row in inventory_rows if row.get("active")),
                "total_on_hand": total_on_hand,
                "locations": len(locations),
                "transactions": len(transactions),
            },
            "items": inventory_rows,
            "locations": locations,
            "transactions": transactions,
            "transaction_lines": transaction_lines,
            "resets": resets,
        }

    async def reset_inventory_data(
        self,
        *,
        organization_id: UUID,
        confirmation: str,
    ) -> dict[str, object]:
        actor_id = await self._active_admin_id(organization_id=organization_id)
        result = await self._rpc(
            "reset_organization_inventory_data",
            {
                "p_organization_id": str(organization_id),
                "p_actor_id": str(actor_id),
                "p_confirmation": confirmation,
            },
        )
        if not isinstance(result, dict):
            raise ValueError("Supabase returned an invalid inventory reset result")
        return result

    async def get_membership_administration(
        self,
        *,
        organization_id: UUID,
    ) -> dict[str, object]:
        organization_filter = f"eq.{organization_id}"
        invites = await self._get(
            "organization_registration_invites",
            {
                "select": (
                    "id,organization_id,code_hint,expires_at,max_uses,use_count,active,"
                    "created_by,created_at,revoked_at"
                ),
                "organization_id": organization_filter,
                "order": "created_at.desc",
                "limit": "50",
            },
        )
        requests = await self._get(
            "organization_registration_requests",
            {
                "select": (
                    "id,organization_id,telegram_user_id,telegram_username,display_name,"
                    "status,created_at,updated_at"
                ),
                "organization_id": organization_filter,
                "order": "created_at.asc",
            },
        )
        members = await self._get(
            "organization_users",
            {
                "select": ("id,telegram_user_id,display_name,role,active,created_at"),
                "organization_id": organization_filter,
                "order": "created_at.asc",
            },
        )
        telegram_user_ids = [
            str(row["telegram_user_id"])
            for row in [*requests, *members]
            if row.get("telegram_user_id") is not None
        ]
        personas = (
            await self._get(
                "telegram_dev_personas",
                {
                    "select": (
                        "id,controller_telegram_user_id,alias,synthetic_telegram_user_id,"
                        "display_name,created_at,last_used_at"
                    ),
                    "synthetic_telegram_user_id": f"in.({','.join(telegram_user_ids)})",
                    "order": "alias.asc",
                },
            )
            if telegram_user_ids
            else []
        )
        personas_by_user_id = {
            str(persona["synthetic_telegram_user_id"]): persona for persona in personas
        }
        return {
            "invites": invites,
            "requests": [
                {
                    **request,
                    "simulated_persona": personas_by_user_id.get(
                        str(request.get("telegram_user_id"))
                    ),
                }
                for request in requests
            ],
            "members": [
                {
                    **member,
                    "simulated_persona": personas_by_user_id.get(
                        str(member.get("telegram_user_id"))
                    ),
                }
                for member in members
            ],
            "dev_personas": personas,
        }

    async def create_registration_invite(
        self,
        *,
        organization_id: UUID,
        code_hash: str,
        code_hint: str,
        expires_at: str,
        max_uses: int,
    ) -> dict[str, object]:
        actor_id = await self._active_admin_id(organization_id=organization_id)
        result = await self._rpc(
            "create_organization_registration_invite",
            {
                "p_organization_id": str(organization_id),
                "p_actor_id": str(actor_id),
                "p_code_hash": code_hash,
                "p_code_hint": code_hint,
                "p_expires_at": expires_at,
                "p_max_uses": max_uses,
            },
        )
        if not isinstance(result, dict):
            raise ValueError("Supabase returned an invalid registration invite")
        return result

    async def approve_registration(
        self,
        *,
        organization_id: UUID,
        registration_request_id: UUID,
        role: str,
    ) -> dict[str, object]:
        actor_id = await self._active_admin_id(organization_id=organization_id)
        result = await self._rpc(
            "approve_organization_registration",
            {
                "p_registration_request_id": str(registration_request_id),
                "p_actor_id": str(actor_id),
                "p_role": role,
            },
        )
        if not isinstance(result, dict):
            raise ValueError("Supabase returned an invalid registration approval")
        return result

    async def reject_registration(
        self,
        *,
        organization_id: UUID,
        registration_request_id: UUID,
    ) -> dict[str, object]:
        actor_id = await self._active_admin_id(organization_id=organization_id)
        result = await self._rpc(
            "reject_organization_registration",
            {
                "p_registration_request_id": str(registration_request_id),
                "p_actor_id": str(actor_id),
            },
        )
        if not isinstance(result, dict):
            raise ValueError("Supabase returned an invalid registration rejection")
        return result

    async def get_context_settings(
        self,
        *,
        organization_id: UUID,
    ) -> dict[str, object] | None:
        result = await self._rpc(
            "load_organization_agent_context_settings",
            {"p_organization_id": str(organization_id)},
        )
        if result is None:
            return None
        if not isinstance(result, dict):
            raise ValueError("Supabase returned invalid organization context settings")
        return result

    async def set_context_settings(
        self,
        *,
        organization_id: UUID,
        policy: str,
        retention_days: int,
        max_tokens: int,
        max_items: int,
        changed_by: str,
    ) -> dict[str, object]:
        result = await self._rpc(
            "set_organization_agent_context_settings",
            {
                "p_organization_id": str(organization_id),
                "p_policy": policy,
                "p_retention_days": retention_days,
                "p_max_tokens": max_tokens,
                "p_max_items": max_items,
                "p_changed_by": changed_by,
            },
        )
        if not isinstance(result, dict):
            raise ValueError("Supabase returned invalid saved context settings")
        return result

    async def clear_context_settings(
        self,
        *,
        organization_id: UUID,
        changed_by: str,
    ) -> dict[str, object] | None:
        result = await self._rpc(
            "clear_organization_agent_context_settings",
            {
                "p_organization_id": str(organization_id),
                "p_changed_by": changed_by,
            },
        )
        if result is None:
            return None
        if not isinstance(result, dict):
            raise ValueError("Supabase returned invalid cleared context settings")
        return result

    async def list_setting_changes(
        self,
        *,
        organization_id: UUID,
        limit: int = 30,
    ) -> list[dict[str, object]]:
        return await self._get(
            "organization_setting_changes",
            {
                "select": "id,setting_key,old_value,new_value,changed_by,created_at",
                "organization_id": f"eq.{organization_id}",
                "order": "created_at.desc",
                "limit": str(limit),
            },
        )

    async def list_conversations(
        self,
        *,
        organization_id: UUID,
    ) -> list[dict[str, object]]:
        conversations = await self._get(
            "inventory_agent_conversations",
            {
                "select": (
                    "id,organization_id,organization_user_id,chat_id,history,summary,"
                    "model_name,created_at,updated_at,context_compacted_at"
                ),
                "organization_id": f"eq.{organization_id}",
                "order": "updated_at.desc",
            },
        )
        if not conversations:
            return []
        user_ids = [str(row["organization_user_id"]) for row in conversations]
        users = await self._get(
            "organization_users",
            {
                "select": "id,telegram_user_id,display_name,role,active",
                "organization_id": f"eq.{organization_id}",
                "id": f"in.({','.join(user_ids)})",
            },
        )
        users_by_id = {str(row["id"]): row for row in users}
        conversation_ids = [str(row["id"]) for row in conversations]
        turns = await self._get(
            "inventory_agent_turns",
            {
                "select": "id,conversation_id,estimated_tokens,created_at,compacted_at",
                "conversation_id": f"in.({','.join(conversation_ids)})",
                "order": "created_at.desc",
                "limit": "1000",
            },
        )
        turns_by_conversation: dict[str, list[dict[str, object]]] = {}
        for turn in turns:
            turns_by_conversation.setdefault(str(turn["conversation_id"]), []).append(turn)

        result: list[dict[str, object]] = []
        for conversation in conversations:
            conversation_turns = turns_by_conversation.get(str(conversation["id"]), [])
            active_turns = [turn for turn in conversation_turns if turn["compacted_at"] is None]
            result.append(
                {
                    **conversation,
                    "history_items": len(_dict_list(conversation.get("history"))),
                    "active_turns": len(active_turns),
                    "compacted_turns": len(conversation_turns) - len(active_turns),
                    "active_estimated_tokens": sum(
                        int(_as_float(turn.get("estimated_tokens"))) for turn in active_turns
                    ),
                    "user": users_by_id.get(str(conversation["organization_user_id"])),
                }
            )
        return result

    async def get_conversation(
        self,
        *,
        organization_id: UUID,
        conversation_id: UUID,
    ) -> dict[str, object] | None:
        rows = await self._get(
            "inventory_agent_conversations",
            {
                "select": "*",
                "id": f"eq.{conversation_id}",
                "organization_id": f"eq.{organization_id}",
                "limit": "1",
            },
        )
        if not rows:
            return None
        conversation = rows[0]
        users = await self._get(
            "organization_users",
            {
                "select": "id,telegram_user_id,display_name,role,active,created_at",
                "id": f"eq.{conversation['organization_user_id']}",
                "organization_id": f"eq.{organization_id}",
                "limit": "1",
            },
        )
        turns = await self._get(
            "inventory_agent_turns",
            {
                "select": (
                    "id,source_event_id,history,estimated_tokens,input_tokens,output_tokens,"
                    "total_tokens,created_at,compacted_at,compaction_policy"
                ),
                "conversation_id": f"eq.{conversation_id}",
                "order": "created_at.desc",
                "limit": "1000",
            },
        )
        return {
            "conversation": conversation,
            "user": users[0] if users else None,
            "active_turns": [turn for turn in turns if turn["compacted_at"] is None],
            "compacted_turns": [turn for turn in turns if turn["compacted_at"] is not None],
        }

    async def _get_for_ids(
        self,
        table: str,
        column: str,
        values: list[str],
    ) -> list[dict[str, object]]:
        if not values:
            return []
        return await self._get(
            table,
            {
                "select": "*",
                column: f"in.({','.join(values)})",
            },
        )

    async def _active_admin_id(self, *, organization_id: UUID) -> UUID:
        rows = await self._get(
            "organization_users",
            {
                "select": "id",
                "organization_id": f"eq.{organization_id}",
                "role": "eq.admin",
                "active": "eq.true",
                "order": "created_at.asc",
                "limit": "1",
            },
        )
        if not rows:
            raise ValueError("This organization has no active admin")
        return UUID(str(rows[0]["id"]))

    async def _get(
        self,
        table: str,
        params: dict[str, str],
    ) -> list[dict[str, object]]:
        async with httpx.AsyncClient(
            base_url=self._rest_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.get(f"/{table}", params=params)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
            raise ValueError(f"Supabase returned invalid dashboard rows for {table}")
        return result

    async def _rpc(self, function_name: str, body: dict[str, object]) -> object:
        async with httpx.AsyncClient(
            base_url=self._rest_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post(f"/rpc/{function_name}", json=body)
        response.raise_for_status()
        return response.json()


def _nested(value: object, *path: str) -> object | None:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _event_summary(payload: object) -> str:
    text = _nested(payload, "message", "text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    caption = _nested(payload, "message", "caption")
    if isinstance(caption, str) and caption.strip():
        return caption.strip()
    callback = _nested(payload, "callback_query", "data")
    if isinstance(callback, str) and callback:
        details = _callback_details(payload)
        if details is not None:
            return f"Button · {details['label']}"
        return f"Button callback · {callback}"
    if _nested(payload, "message", "photo") is not None:
        return "Invoice photo"
    return "Telegram event"


def _dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _callback_details(payload: object) -> dict[str, object] | None:
    callback_data = _nested(payload, "callback_query", "data")
    if not isinstance(callback_data, str):
        return None
    try:
        command = decode_callback(callback_data)
    except ValueError:
        return {"label": "invalid callback", "raw": callback_data}
    labels = {
        "s": "select variant",
        "c": "confirm proposal",
        "x": "cancel proposal",
        "a": "add new item",
        "e": "choose existing item",
        "k": "create catalog item",
        "d": "cancel catalog item",
        "r": "reverse transaction",
        "v": "confirm reversal",
        "z": "cancel reversal",
    }
    return {
        "action": command.action.name.casefold(),
        "label": labels.get(command.action.value, command.action.name.replace("_", " ").casefold()),
        "target_id": str(command.target_id),
        "choice_id": str(command.choice_id) if command.choice_id else None,
        "raw": callback_data,
    }
