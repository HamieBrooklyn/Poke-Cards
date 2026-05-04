"""Starter slash commands — add more cogs alongside this one."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class General(commands.Cog):
    """General-purpose user commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="help",
        description="PokePon: what the short commands mean (slash vs c+command in chat, like gachapon’s g+…)",
    )
    async def pokepon_help(self, ctx: commands.Context) -> None:
        e = discord.Embed(
            title="PokePon commands",
            description=(
                "Use **slash** (`/cd`, `/cs`, `/cv`, `/colv`, `/coll`, `/cevolve`, `/duel`, `/deck`, `/trade`, `/help`) from the command menu. In a server, you can also type the "
                "**`c` prefix** the same way [Gachapon](https://alternative.me/discord/bots/gachapon/commands) uses "
                " **`g`**: **`cd`**, **`cs`**, **`cv`**, **`colv`**, **`coll`**, **`cevolve`**, **`duel`**, **`deck`**, **`trade`**, **`chelp`**. That needs "
                "**`DISCORD_MESSAGE_CONTENT_INTENT=1` in .env** and **Message Content Intent** on the Developer Portal "
                " **Bot** tab → **Privileged Gateway Intents** (not OAuth2 scopes, not invite permissions)."
            ),
        )
        e.add_field(
            name="`cd` — card drop",
            value="Open a pack and keep **one** of the revealed cards. Slash **`private`** hides the pack from the channel.",
            inline=False,
        )
        e.add_field(
            name="`cs` — catalog / collection **search** (text list)",
            value="**`scope`**: `g` = the global imported catalog; `c` = cards *you* saved from drops. "
            "Add filters (name, rarity, Pokédex #, set fields for `g`, `card_ref` for an exact id, etc.).",
            inline=False,
        )
        e.add_field(
            name="`cv` — **card view** (large image)",
            value="**`scope`**: `g` to browse art and stats from the catalog (◀▶ in the first 100 matches), "
            "`c` to inspect a copy in your collection.",
            inline=False,
        )
        e.add_field(
            name="`colv` — **collection view** (flip through your cards)",
            value="Browse your saved cards newest-first with **◀** **▶** (optional **`limit`**). "
            "Same card details as **`cv c`**.",
            inline=False,
        )
        e.add_field(
            name="`coll` — **collection list**",
            value="Plain **text**, one line per card (Card ID in `` ` ``). **◀** **▶** pages.",
            inline=False,
        )
        e.add_field(
            name="`cevolve` — **card evolve**",
            value="Shows a **preview** (your printing → evolution target, with **cost**), then **Evolve** or **Cancel**. "
            "Pass your **Card ID** (same id as **`cv c`** / **`colv`**).",
            inline=False,
        )
        e.add_field(
            name="`duel` / `deck` — **PvP duels**",
            value="**`/deck set`** — save **1–6** Pokémon you own (Card IDs, lead first). **`/deck view`** — your bench.\n"
            "**`/duel challenge`** — challenge someone, optional **bet**; both **Accept** / **Ready**; decks stay hidden until the fight. "
            "Turn-based: pick moves from the card’s attacks (damage from the printing; complex text is shown but not simulated yet). "
            "Winner takes the **pot**.",
            inline=False,
        )
        e.add_field(
            name="`trade` — **player trades**",
            value="**`/trade offer`** — you and another member list **Card IDs** (and optional **Pokedollars** on each side). "
            "They **Accept** or **Decline**; you can **Cancel offer**. **`/trade gift`** — give cards/₽ for nothing back. "
            "Prefix: **`ctrade`** (same pattern as **`cd`**).",
            inline=False,
        )
        e.set_footer(text="Other slash: /ping, /hello, /daily, /balance. In chat: chelp (prefix c + help) for this embed.")
        await ctx.send(embed=e, ephemeral=False)

    @app_commands.command(name="ping", description="Check bot latency")
    async def ping(self, interaction: discord.Interaction) -> None:
        ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong — **{ms}** ms", ephemeral=False)

    @app_commands.command(name="hello", description="Greet you")
    async def hello(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"Hello, {interaction.user.mention}!",
            ephemeral=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
