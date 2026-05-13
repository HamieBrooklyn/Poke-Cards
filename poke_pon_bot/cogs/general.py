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
        description="PokePon: what the short commands mean (slash menu vs typing the command name in chat)",
    )
    async def pokepon_help(self, ctx: commands.Context) -> None:
        e = discord.Embed(
            title="PokePon commands",
            description=(
                "Use **slash** from the **`/`** menu (**`/cd`**, **`/cs`**, **`/auction create`**, **`/drop_boost`**, …) — pick the command so Discord shows the **blue pill**. Typing **`/auction`** as **plain chat text** is **not** a command.\n"
                "**Chat commands** — just **type the command name directly** (**`cd`**, **`coll`**, **`packd`**, **`trade`**, **`help`**, …). This needs **Message Content Intent** on in the Developer Portal → **Bot** → **Privileged Gateway Intents** (not OAuth2 scopes). The bot requests that intent by default; set **`DISCORD_MESSAGE_CONTENT_INTENT=0`** in `.env` only if you want slash-only."
            ),
        )
        e.add_field(
            name="`cd` — card drop",
            value="Open a pack and keep **one** of the revealed cards. Slash **`private`** hides the pack from the channel. "
            "Optional **half cooldown** durable SKU: **`/drop_boost`** (and configure `DISCORD_DROP_BOOST_SKU_ID`).",
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
            name="`packd` / `packv` / `packcolv` / `packcat` — **booster packs**",
            value=(
                "**`/packd`** — drop a random pack: **5,000** ₽, **10** 💎, or the consumable SKU.\n"
                "**`/packv`** — search a specific series, flip pack visuals, and buy with **💎**.\n"
                "**`/packcolv`** — flip through your **unopened** packs; **Open** rolls **10 + 1** card and grants 💎 from the code card.\n"
                "**`/packcat`** — browse the catalog with **`sort:`** `popular` / `rarest` / cost, and **`scope:`** `server` / `global`."
            ),
            inline=False,
        )
        e.add_field(
            name="`duel` / `deck` — **PvP duels**",
            value="**`/deck edit`** — interactive bench (**1–6** Pokémon): slot dropdown, reply with **Card ID**, clear/remove. **`/deck view`** — your bench.\n"
            "**`/duel challenge`** — challenge someone, optional **bet**; both **Accept** / **Ready**; decks stay hidden until the fight. "
            "Turn-based: damage uses each attack’s **energy cost** as its type vs the defender’s **card types** (main-series matchups: **green** = super effective, **red** = not very effective / no effect, **grey** = neutral in the battle log). "
            "Winner takes the **pot**.",
            inline=False,
        )
        e.add_field(
            name="`trade` — **player trades**",
            value="**`/trade offer`** — you and another member list **Card IDs** (and optional **Pokedollars** on each side). "
            "They **Accept** or **Decline**; you can **Cancel offer**. **`/trade gift`** — give cards/₽ for nothing back. "
            "Chat: type **`trade`** for the same flow.",
            inline=False,
        )
        e.add_field(
            name="`auction` — **timed auctions**",
            value="**`/auction create`** — list a card (modal: **Card ID**, starting bid, **duration**). "
            "**`/auction search`** — optional **`seller`** (their listings only) plus name / rarity / Pokédex; in chat you can **reply** to someone instead of **`seller`**. "
            "**`/auction bid`** · chat: **`auction create`**, **`auction search`**, **`auction bid`**.\n"
            "**`/auction bid`** / chat **`auction bid`** — listing **#** or **Card ID** + ₽. "
            "When time ends, highest bidder gets the card; seller receives the winning ₽.",
            inline=False,
        )
        e.set_footer(
            text="Other slash: /ping, /hello, /daily, /vote, /balance (+optional user). "
            "In chat: type `help` for this embed.",
        )
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
