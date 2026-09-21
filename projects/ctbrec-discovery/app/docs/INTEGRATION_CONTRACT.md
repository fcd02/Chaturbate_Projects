# Discovery Integration Contract — v0.4.1

## v0.4.1 additions

- Mobile Reviewer health check: prefer read-only `GET /api/auth`; 401/403 counts as reachable/auth-required.
- Mobile Reviewer cache: recursively walk nested mode/root containers; never interpret structural bucket keys as usernames.
- Live Control and Recu local collectors may run independently; duplicate runs of the same source remain coalesced.
- Recu remains local-cache-only inside Discovery.

---


## Read-only ecosystem inputs

Discovery may read:

- Mobile Reviewer `mobile_catalog_cache.json`;
- Live Control `GET /api/models` on the configured loopback service;
- Rapid Sorter / Mobile Reviewer local Recu cache files;
- explicitly configured local Recu discovery, neighbor and continuity exports;
- Chaturbate's official affiliate endpoint only as an explicitly configured/manual fallback.

No input may be modified by Discovery.

## Live Control loopback contract

Default base URL: `http://127.0.0.1:8792`.

v0.4 consumes `/api/models` conservatively and supports common list/object wrappers. Useful fields include username/name/model or URL-derived slug, favorite flag, numeric/priority fields, online/public state and `affiliateRoomInfo` public metadata.

A failure of this local source must leave existing cached recommendations usable. The collector status reports `network_scope=loopback_only` and must never imply it performed a Chaturbate request.

## Official affiliate fallback contract

The direct affiliate collector is disabled by default. When a WM ID remains configured from an earlier install, an explicit user `Run one-shot` may instantiate one bounded official-API run without changing persistent scheduling.

A future Live Control sanitized global-snapshot endpoint should supersede this fallback.

## Mobile Reviewer catalog contract

Discovery accepts the current map-style cache with `catalog` plus legacy list/map layouts. It must treat the file as a derived read-only cache and use file fingerprinting to avoid reparsing an unchanged multi-megabyte cache every scheduler tick.

Discovery must never trigger a second recurring deep library scan merely to populate recommendations.

## Recu contract

`recu_local_archives` is local-file-only. It may parse cached metadata and Best-Moments-related metadata already present locally, but it must not navigate Recu, launch another Recu browser, solve/automate verification, capture credentials itself or bypass membership/access controls.

Live/authenticated Recu acquisition remains owned by the existing authorized Reviewer/Rapid Sorter workflow.

## Recommendation filter contract

The browser may mutate only the safe `recommendation_filters` object through `/api/settings`.

The settings API must not return or overwrite WM IDs, cookies/session tokens, PINs/secrets or unrelated collector configuration.

Unknown-gender eligibility is separate from allowed known gender codes. Couples are separate because a generic couple code does not prove the composition of the room.

## Discovery → CTBRec contract

v0.4 continues to create durable **intent only** outbox rows for preference changes.

Current action type: `preference_sync_request`.

Expected payload includes label, numeric priority, account identities, target `verified_mobile_reviewer_live_bridge_adapter`, `execute=false`, and optional reason such as Undo.

There is no action consumer in v0.4.

A future consumer must use the exact current identity-verified Live Control/JVM bridge contract, confirm target-installation fingerprint, be idempotent, keep retention/recording-state semantics independent from preference unless explicitly requested, and never silently fall back to direct `models.json` mutation.

## Live-room media contract (next increment)

Do not implement a second Chaturbate player stack. Reuse Live Control's proven server-side thumbnail and user-triggered HLS resolver/proxy while preserving its tracked-model authorization and service authentication.

For untracked discovery candidates, either Live Control must explicitly support a discovery-safe preview endpoint sourced from its complete affiliate snapshot, or Discovery must use another expressly authorized design. Do not weaken Live Control's existing tracked-model check merely for convenience.

## Best Moments contract (next increment)

Prefer already-local Rapid Sorter media under `runtime/recu_best_moments/`: index lazily, serve with HTTP Range support, do not copy the full file to the phone, require no new Recu request for local media, and if live media is needed later use only the existing authorized session broker/current entitlement.
