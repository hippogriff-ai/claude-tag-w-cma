"""
provision.py — one-time CMA provisioning (SPEC C1). Safe to re-run: idempotent.

    python provision.py         # creates environment + agent + memory store, saves IDs

IDs land in cma_config.json (gitignored). Re-running with the config present creates
nothing new — it just prints the existing IDs. Requires ANTHROPIC_API_KEY (.env.local).
"""
import asyncio

from slack_app import _load_dotenv
import cma_broker


async def main():
    _load_dotenv()
    from anthropic import AsyncAnthropic
    print("provisioning weekend-window on CMA (idempotent)…")
    await cma_broker.provision(AsyncAnthropic())
    print("done — IDs in cma_config.json. Next: python slack_app.py")


if __name__ == "__main__":
    asyncio.run(main())
