"""
NATS message handlers — mordomo-people.

Subjects:
  mordomo.people.resolve         → resolve name/alias to full profile + contacts
  mordomo.people.permissions.get → get permissions for a person_id
  mordomo.people.upsert          → create or update person
"""
import json
import logging
from nats.aio.msg import Msg
from src import db, cache

logger = logging.getLogger(__name__)


def _ok(data: dict) -> bytes:
    return json.dumps({"ok": True, **data}).encode()


def _err(message: str) -> bytes:
    return json.dumps({"ok": False, "error": message}).encode()


async def handle_resolve(msg: Msg) -> None:
    """
    Resolves a name/alias to a full person profile including decrypted contacts.
    Cache hit: Redis. Miss: Postgres, then populate cache.
    Publishes mordomo.people.resolved after responding.
    """
    try:
        payload = json.loads(msg.data)
        name: str = payload.get("name", "").strip()
        if not name:
            await msg.respond(_err("'name' is required"))
            return

        # Cache hit
        person = await cache.get_cached_person(name)
        if person is None:
            person = await db.resolve_person(name)
            if person:
                await cache.set_cached_person(name, person)
                # Also cache by canonical name in case the lookup was by alias
                if person["name"].lower() != name.lower():
                    await cache.set_cached_person(person["name"], person)

        if person is None:
            await msg.respond(_err(f"Person '{name}' not found"))
            # Still publish resolved event with found=false so callers don't hang
            await msg._client.publish(
                "mordomo.people.resolved",
                json.dumps({"query": name, "found": False}).encode(),
            )
            return

        await msg.respond(_ok({"person": person}))
        await msg._client.publish(
            "mordomo.people.resolved",
            json.dumps({"query": name, "person_id": person["id"], "found": True}).encode(),
        )

    except Exception as e:
        logger.exception("handle_resolve error")
        await msg.respond(_err(str(e)))


async def handle_permissions_get(msg: Msg) -> None:
    """Returns the permission map for a given person_id."""
    try:
        payload = json.loads(msg.data)
        person_id: str = payload.get("person_id", "").strip()
        if not person_id:
            await msg.respond(_err("'person_id' is required"))
            return

        perms = await cache.get_cached_permissions(person_id)
        if perms is None:
            perms = await db.get_permissions(person_id)
            await cache.set_cached_permissions(person_id, perms)

        await msg.respond(_ok({"person_id": person_id, "permissions": perms}))

    except Exception as e:
        logger.exception("handle_permissions_get error")
        await msg.respond(_err(str(e)))


async def handle_upsert(msg: Msg) -> None:
    """
    Create or update a person. Invalidates cache for the person's name and id.
    Required: name
    Optional: aliases, voice_profile_id, face_profile_id, is_owner,
              contacts (list of {type, value, label}),
              permissions (dict)
    """
    try:
        payload = json.loads(msg.data)
        if not payload.get("name"):
            await msg.respond(_err("'name' is required"))
            return

        person_id = await db.upsert_person(payload)

        # Invalidate stale cache entries
        await cache.invalidate_person(payload["name"])
        for alias in payload.get("aliases", []):
            await cache.invalidate_person(alias)
        await cache.invalidate_permissions(person_id)

        await msg.respond(_ok({"person_id": person_id}))

    except Exception as e:
        logger.exception("handle_upsert error")
        await msg.respond(_err(str(e)))
