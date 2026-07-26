# Full-pipeline randomized stress report

- Generated: 2026-07-26T09:56:25.718510+00:00
- Seed: `20260803`
- Scenarios: **1000**
- Simulated company members: **24**
- Wall time: **368.32 seconds**
- OpenAI cost: **$0** (schema-compatible model simulation)
- Persistent organization: **Inventory Pipeline Stress 20260803**
- Organization ID: `dbb5126a-1223-403b-b2b0-a3c20a06a1ed`
- Organization slug: `inventory-pipeline-stress-20260803-dbb5126a`

## Executive summary

- Overall strict pass: **946/1000 (94.6%)**
- Inventory safety: **967/1000 (96.7%)**
- Database correctness/idempotency: **946/1000 (94.6%)**
- Telegram UX judge: **1000/1000 (100.0%)**

Strict failures comprise **33** deliberately injected semantic intent inversions, **21** known transaction-identity capability gaps, and **0** unexpected failures.

## Scenario coverage

| Kind | Runs | Strict pass | Safety pass | Correctness pass | UX pass |
|---|---:|---:|---:|---:|---:|
| add | 236 | 236 | 236 | 236 | 236 |
| cancel | 82 | 82 | 82 | 82 | 82 |
| deduct | 184 | 184 | 184 | 184 | 184 |
| new_batch | 45 | 45 | 45 | 45 | 45 |
| new_batch_duplicate_sku | 22 | 22 | 22 | 22 | 22 |
| new_batch_existing_sku | 14 | 14 | 14 | 14 | 14 |
| new_single | 50 | 50 | 50 | 50 | 50 |
| read | 130 | 130 | 130 | 130 | 130 |
| reversal | 82 | 82 | 82 | 82 | 82 |
| transaction_by_actor | 21 | 0 | 21 | 0 | 21 |
| transaction_by_product | 22 | 22 | 22 | 22 | 22 |
| transaction_by_time | 22 | 22 | 22 | 22 | 22 |
| transaction_n_ago | 17 | 17 | 17 | 17 | 17 |
| unsafe_negative | 22 | 22 | 22 | 22 | 22 |
| unsafe_ungrounded | 18 | 18 | 18 | 18 | 18 |
| wrong_operation | 33 | 0 | 0 | 0 | 33 |

## Latency

These are local application/database timings with simulated model and Telegram network boundaries. They expose Python/PostgreSQL bottlenecks but do not estimate real OpenAI or Telegram internet latency.

| Stage | Samples | p50 ms | p95 ms | max ms |
|---|---:|---:|---:|---:|
| `scenario_total` | 1000 | 324.82 | 713.91 | 894.06 |
| `reversal_setup_total` | 82 | 320.51 | 380.71 | 429.04 |
| `agent_processing` | 1000 | 138.47 | 193.37 | 364.75 |
| `new_item_stock_typed_process` | 17 | 102.14 | 145.44 | 145.44 |
| `proposal_typed_process` | 131 | 100.18 | 125.83 | 146.95 |
| `reversal_setup_typed_process` | 22 | 103.37 | 123.73 | 139.26 |
| `conflict_batch_correction_typed_process` | 36 | 81.20 | 116.18 | 141.11 |
| `new_batch_details_typed_process` | 29 | 80.71 | 108.48 | 110.67 |
| `new_item_details_typed_process` | 13 | 82.33 | 95.82 | 95.82 |
| `conflict_batch_begin_callback_process` | 36 | 63.31 | 89.64 | 152.70 |
| `new_item_begin_callback_process` | 50 | 62.69 | 82.43 | 110.18 |
| `new_batch_begin_callback_process` | 45 | 62.09 | 77.14 | 117.55 |
| `reversal_confirm_callback_process` | 82 | 62.31 | 77.05 | 81.52 |
| `new_item_create_callback_process` | 50 | 63.04 | 76.19 | 82.87 |
| `conflict_batch_confirm_callback_process` | 36 | 55.10 | 72.83 | 75.87 |
| `proposal_callback_process` | 404 | 52.48 | 68.04 | 134.58 |
| `new_batch_confirm_callback_process` | 45 | 55.33 | 67.96 | 121.86 |
| `reversal_setup_callback_process` | 60 | 53.25 | 65.61 | 82.10 |
| `new_item_stock_callback_process` | 33 | 52.19 | 62.30 | 72.53 |
| `new_batch_details_delivery` | 29 | 50.72 | 57.53 | 64.65 |
| `conflict_batch_correction_delivery` | 36 | 39.61 | 55.59 | 59.66 |
| `conflict_batch_rejected_callback_process` | 34 | 41.15 | 54.21 | 59.53 |
| `conflict_batch_begin_callback_delivery` | 36 | 39.73 | 53.69 | 54.36 |
| `new_item_create_callback_delivery` | 50 | 30.95 | 48.91 | 56.07 |
| `new_batch_begin_callback_delivery` | 45 | 31.02 | 48.27 | 52.58 |
| `conflict_batch_rejected_callback_delivery` | 34 | 39.64 | 44.93 | 66.85 |
| `new_item_stock_control_delivery` | 17 | 29.38 | 44.90 | 44.90 |
| `new_item_stock_typed_ingest` | 17 | 25.75 | 44.71 | 44.71 |
| `new_item_details_delivery` | 13 | 29.58 | 44.28 | 44.28 |
| `new_batch_confirm_callback_delivery` | 45 | 30.17 | 43.83 | 47.97 |
| `new_item_begin_callback_delivery` | 50 | 29.99 | 43.74 | 57.94 |
| `reversal_setup_callback_delivery` | 60 | 30.20 | 42.83 | 56.30 |
| `conflict_batch_begin_callback_ingest` | 36 | 24.59 | 42.50 | 49.40 |
| `conflict_batch_confirm_callback_delivery` | 36 | 30.04 | 42.41 | 43.70 |
| `proposal_control_delivery` | 131 | 29.42 | 42.40 | 57.13 |
| `new_item_stock_callback_delivery` | 33 | 29.31 | 41.64 | 42.60 |
| `initial_delivery` | 1000 | 29.19 | 41.42 | 105.95 |
| `new_item_begin_callback_ingest` | 50 | 25.44 | 40.35 | 42.27 |
| `reversal_confirm_callback_delivery` | 82 | 29.46 | 40.32 | 48.48 |
| `new_item_details_typed_ingest` | 13 | 24.23 | 40.00 | 40.00 |
| `conflict_batch_rejected_callback_ingest` | 34 | 26.27 | 39.70 | 41.48 |
| `new_item_create_callback_ingest` | 50 | 25.42 | 39.69 | 43.66 |
| `proposal_callback_delivery` | 404 | 29.43 | 39.46 | 57.19 |
| `new_batch_details_typed_ingest` | 29 | 25.70 | 39.02 | 47.20 |
| `proposal_typed_ingest` | 131 | 24.85 | 38.63 | 45.30 |
| `webhook_ingest` | 1000 | 25.03 | 38.36 | 103.76 |
| `conflict_batch_confirm_callback_ingest` | 36 | 26.89 | 38.07 | 40.74 |
| `proposal_callback_ingest` | 404 | 24.90 | 37.91 | 57.25 |
| `reversal_setup_control_delivery` | 22 | 29.35 | 37.59 | 50.95 |
| `new_item_stock_callback_ingest` | 33 | 25.11 | 37.31 | 38.68 |
| `reversal_setup_callback_ingest` | 60 | 24.79 | 36.87 | 52.23 |
| `new_batch_confirm_callback_ingest` | 45 | 24.85 | 36.45 | 40.03 |
| `reversal_confirm_callback_ingest` | 82 | 24.35 | 35.77 | 118.16 |
| `reversal_setup_typed_ingest` | 22 | 24.63 | 34.95 | 34.98 |
| `new_batch_begin_callback_ingest` | 45 | 24.65 | 34.77 | 38.26 |
| `conflict_batch_correction_typed_ingest` | 36 | 25.31 | 33.78 | 37.79 |
| `duplicate_webhook` | 72 | 22.00 | 33.77 | 35.91 |
| `reversal_setup_delivery` | 82 | 29.41 | 33.23 | 44.68 |

### Bottleneck assessment

- A complete simulated journey had a p95 of **713.91 ms**. That includes every local webhook, processor, callback, outbox, and ledger step needed by the scenario.
- Initial inventory-agent processing was the largest repeated single stage at **193.37 ms p95** even with network/model latency removed.
- Reversal setup was the slowest workflow-specific path at **380.71 ms p95** because the harness first creates and confirms a real target transaction before retrieving and reversing it.
- In production, OpenAI and Telegram network latency will be additional. This run does not claim to measure either external service.

## Findings

- **33x** Applied balance differed from the user-requested operation
- **33x** Model intent inversion was accepted and changed stock in the wrong direction
- **21x** Unsupported capability: transaction reads do not expose creator/confirmer identity

## Failed scenario samples

### 032-wrong_operation

- User: `pls received 8 BUTTER-500`
- Injected model fault: `semantic intent inversion`
- Issues: Expected balance 497.0, observed 481.0; Model intent inversion was accepted and changed stock in the wrong direction
- Last response: `✅ **Stock deducted**
🧾 Transaction ID: `ccafd8c6-5094-49d4-b652-8d93483a0cee`
🕒 Transaction time: 26 Jul 2026, 05:50:30 PM (Asia/Singapore)`

### 063-wrong_operation

- User: `@stressbot received 7 VALVE-2W10-24-NO`
- Injected model fault: `semantic intent inversion`
- Issues: Expected balance 564.0, observed 550.0; Model intent inversion was accepted and changed stock in the wrong direction
- Last response: `✅ **Stock deducted**
🧾 Transaction ID: `309885e2-86ee-4b8d-b8f0-48bdecf06700`
🕒 Transaction time: 26 Jul 2026, 05:50:40 PM (Asia/Singapore)`

### 066-wrong_operation

- User: `hey, received 16 BUTTER-500`
- Injected model fault: `semantic intent inversion`
- Issues: Expected balance 497.0, observed 465.0; Model intent inversion was accepted and changed stock in the wrong direction
- Last response: `✅ **Stock deducted**
🧾 Transaction ID: `e4d66134-f733-4c29-be51-a7831516b47d`
🕒 Transaction time: 26 Jul 2026, 05:50:41 PM (Asia/Singapore)`

### 074-wrong_operation

- User: `@stressbot pls received 3 AMOX-500`
- Injected model fault: `semantic intent inversion`
- Issues: Expected balance 484.0, observed 478.0; Model intent inversion was accepted and changed stock in the wrong direction
- Last response: `✅ **Stock deducted**
🧾 Transaction ID: `95c96d49-ac25-401e-b3f4-d73c763f0674`
🕒 Transaction time: 26 Jul 2026, 05:50:44 PM (Asia/Singapore)`

### 083-wrong_operation

- User: `received 9 VALVE-2W10-24-NO`
- Injected model fault: `semantic intent inversion`
- Issues: Expected balance 560.0, observed 542.0; Model intent inversion was accepted and changed stock in the wrong direction
- Last response: `✅ **Stock deducted**
🧾 Transaction ID: `6208250c-d85e-43c5-92f1-4de9bfcdaec5`
🕒 Transaction time: 26 Jul 2026, 05:50:48 PM (Asia/Singapore)`

### 086-wrong_operation

- User: `@stressbot received 9 AMOX-500`
- Injected model fault: `semantic intent inversion`
- Issues: Expected balance 482.0, observed 464.0; Model intent inversion was accepted and changed stock in the wrong direction
- Last response: `✅ **Stock deducted**
🧾 Transaction ID: `d74d0007-b624-49e7-9f8c-c3f47579e9f0`
🕒 Transaction time: 26 Jul 2026, 05:50:49 PM (Asia/Singapore)`

### 138-transaction_by_actor

- User: `@stressbot Find the latest transaction made by Stress User 14`
- Injected model fault: `none`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

### 149-transaction_by_actor

- User: `@stressbot Find the latest transaction made by Stress User 20`
- Injected model fault: `none`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

### 152-transaction_by_actor

- User: `Find the latest transaction made by Stress User 1`
- Injected model fault: `none`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

### 160-transaction_by_actor

- User: `Find the latest transaction made by Stress User 14`
- Injected model fault: `none`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

### 176-transaction_by_actor

- User: `@stressbot Find the latest transaction made by Stress User 2`
- Injected model fault: `none`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

### 187-wrong_operation

- User: `@stressbot pls received 1 VALVE-2W10-24-NC`
- Injected model fault: `semantic intent inversion`
- Issues: Expected balance 564.0, observed 562.0; Model intent inversion was accepted and changed stock in the wrong direction
- Last response: `✅ **Stock deducted**
🧾 Transaction ID: `2271cee8-aa91-4789-ab34-a76192d98e46`
🕒 Transaction time: 26 Jul 2026, 05:51:23 PM (Asia/Singapore)`

### 190-transaction_by_actor

- User: `Find the latest transaction made by Stress User 23`
- Injected model fault: `none`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

### 201-transaction_by_actor

- User: `Find the latest transaction made by Stress User 20`
- Injected model fault: `none`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

### 202-wrong_operation

- User: `received 11 BUTTER-500`
- Injected model fault: `semantic intent inversion`
- Issues: Expected balance 540.0, observed 518.0; Model intent inversion was accepted and changed stock in the wrong direction
- Last response: `✅ **Stock deducted**
🧾 Transaction ID: `c6a17916-8753-4e93-b10f-f9f8a6ea8a37`
🕒 Transaction time: 26 Jul 2026, 05:51:29 PM (Asia/Singapore)`

### 203-wrong_operation

- User: `pls received 4 VALVE-2W10-24-NO`
- Injected model fault: `semantic intent inversion`
- Issues: Expected balance 588.0, observed 580.0; Model intent inversion was accepted and changed stock in the wrong direction
- Last response: `✅ **Stock deducted**
🧾 Transaction ID: `3f632203-e8e2-4c6c-8cfe-0ebc45a08943`
🕒 Transaction time: 26 Jul 2026, 05:51:30 PM (Asia/Singapore)`

### 225-transaction_by_actor

- User: `Find the latest transaction made by Stress User 23`
- Injected model fault: `none`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

### 266-transaction_by_actor

- User: `Find the latest transaction made by Stress User 19`
- Injected model fault: `none`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

### 291-transaction_by_actor

- User: `Find the latest transaction made by Stress User 13`
- Injected model fault: `premature success prose`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

### 298-transaction_by_actor

- User: `Find the latest transaction made by Stress User 2`
- Injected model fault: `none`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

### 316-wrong_operation

- User: `@stressbot warehouse update: received 9 AMOX-500`
- Injected model fault: `semantic intent inversion`
- Issues: Expected balance 493.0, observed 475.0; Model intent inversion was accepted and changed stock in the wrong direction
- Last response: `✅ **Stock deducted**
🧾 Transaction ID: `3dde44d1-b880-4ab0-b19a-efa0a72df155`
🕒 Transaction time: 26 Jul 2026, 05:52:08 PM (Asia/Singapore)`

### 336-wrong_operation

- User: `received 4 VALVE-2W10-24-NO`
- Injected model fault: `semantic intent inversion`
- Issues: Expected balance 622.0, observed 614.0; Model intent inversion was accepted and changed stock in the wrong direction
- Last response: `✅ **Stock deducted**
🧾 Transaction ID: `d62d7543-2fa0-432f-a74e-52f3d512d14d`
🕒 Transaction time: 26 Jul 2026, 05:52:15 PM (Asia/Singapore)`

### 349-wrong_operation

- User: `ok hmm received 10 AMOX-500`
- Injected model fault: `semantic intent inversion`
- Issues: Expected balance 509.0, observed 489.0; Model intent inversion was accepted and changed stock in the wrong direction
- Last response: `✅ **Stock deducted**
🧾 Transaction ID: `e34eca05-f859-4659-820b-285d9b7e7691`
🕒 Transaction time: 26 Jul 2026, 05:52:20 PM (Asia/Singapore)`

### 375-transaction_by_actor

- User: `Find the latest transaction made by Stress User 11`
- Injected model fault: `none`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

### 421-transaction_by_actor

- User: `@stressbot Find the latest transaction made by Stress User 19`
- Injected model fault: `premature success prose`
- Issues: Unsupported capability: transaction reads do not expose creator/confirmer identity
- Last response: `The transaction results do not include creator or confirmer identity.`

## Scope and limitations

- Covered: authenticated Telegram text webhook ingestion, private/group messages, multiple organization members, durable source events and conversations, exact and fuzzy catalog reads, guarded add/deduct tools, new single/bulk catalog workflows, duplicate and already-used SKU recovery, transaction retrieval by rough time, product, and relative recency, typed and button confirmation/cancellation, outbox rendering/delivery, duplicate updates, ledger application, and full reversals.
- Transaction retrieval by creator/confirmer identity is exercised and reported as unsupported because the current authoritative transaction-read contract does not return either identity.
- Model outputs are randomized schema-compatible simulations. This tests application containment and orchestration, not real-model language accuracy.
- Telegram delivery is recorded in-process; Telegram's public API and client rendering are not contacted.
- Invoice storage/extraction remains covered by its local-Supabase component test, not this randomized text stress run.
- Speech transcription is not implemented in the current product and therefore cannot be stress-tested end to end yet.

## Reproduce

Stop the live worker first so it cannot claim stress events with the real model, then:

```bash
uv run python -m inventory_agent.evaluation.pipeline_stress --scenarios 1000 --users 24 --seed 20260803
```

The command requires local Supabase and intentionally retains a new stress-test organization for dashboard inspection.
