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
                "**Chat commands** — type **`pp`** + the command (**`ppcd`**, **`ppbal`**, **`ppas`** for auction search, …). Shorter forms like **`ppbal`** work instead of **`ppbalance`**. In the official server, bare names like **`balance`** or **`auction as`** also work. Needs **Message Content Intent** (Developer Portal → **Bot**). Set **`DISCORD_MESSAGE_CONTENT_INTENT=0`** in `.env` only for slash-only."
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
            name="`duel` / `deck` — **PvP & PvE duels**",
            value="**`/deck edit`** — interactive bench (**1–6** Pokémon): slot dropdown, reply with **Card ID**, clear/remove. **`/deck view`** — your bench.\n"
            "**`/duel challenge`** — challenge someone, optional **bet**; both **Accept** / **Ready**; decks stay hidden until the fight. "
            "Turn-based: damage uses each attack’s **energy cost** as its type vs the defender’s **card types** (main-series matchups: **green** = super effective, **red** = not very effective / no effect, **grey** = neutral in the battle log). "
            "Winner takes the **pot**.\n"
            "**`/pd`** / **`pcpd`** — wild **Poke-duel** for ₽. Run the command again to spawn a new fight. "
            "Beat a rare (Illustration Rare+), high-HP (220+), or high-damage (170+) wild for a **💎 Crystal** bonus!",
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
            "**`/auction spotlight`** — feature **your active listing** in search for 24h (**12** 💎). "
            "**`/auction bid`** · chat: **`auction create`**, **`auction search`**, **`auction bid`**, **`auction spotlight`**.\n"
            "**`/auction bid`** / chat **`auction bid`** — listing **#** or **Card ID** + ₽. "
            "When time ends, highest bidder gets the card; seller receives the winning ₽.",
            inline=False,
        )
        e.add_field(
            name="**Crystal spends** (optional)",
            value=(
                "**`/cd`** — after opening a pack, tap **Reroll 8 💎** (right of the Take buttons) to replace one random unclaimed card.\n"
                "**`/auction spotlight`** — feature **your active listing** in search for **24h** (**12** 💎).\n"
                "**Website → Profile → Cosmetics** — preview, unlock, and **select** a **leaderboard avatar frame** (metals, gems, animated effects)."
            ),
            inline=False,
        )
        e.add_field(
            name="`marketcolv` — **card investing**",
            value="**`/marketcolv`** / **`ppmarketcolv`** — flip **your collection** with live **TCGPlayer** chart; pass a **Card ID** (`ppmarketcolv <id>`) to jump to that copy. **Buy** opens a ₽ amount prompt, **Sell** closes the position. "
            "**`/marketinveststatus`** / **`ppmis`** — list open investments. "
            "You can only invest in printings you own. Same panel is on the website collection card → **Market** tab.",
            inline=False,
        )
        e.add_field(
            name="`grade` — **PSA-style grading**",
            value="**`/grade`** — slab view for a copy in your collection; **roll** or **reroll** for **15** 💎. "
            "High grades (**7+**) can **bump display rarity** (never drops). Each roll also grants a random **enchantment film** (rarer films are harder to hit). "
            "High grades (**7+**) add a small **shop sell bonus**. Slab badges show on trades, auctions, and leaderboards.",
            inline=False,
        )
        e.add_field(
            name="`leaderboard` — **rankings**",
            value="**`/leaderboard`** — **global** or **server** rankings (strongest, tankiest, rarest, auctions, graded). "
            "Server-only: **packs opened**, **top traders**, **top collectors**. "
            "**`/servermilestones`** — community pack progress + admin config. "
            "Chat: **`pcleaderboard`** or **`lb`**.",
            inline=False,
        )
        e.add_field(
            name="`referral` — **invite friends**",
            value="**`/referral`** — your personal invite link, friends you invited, and your progress if someone referred you. "
            "First **`/cd`** after a first-time join rewards **both** of you; **25** 💎 more for the inviter at **10** packs (up to **3** friends).",
            inline=False,
        )
        e.add_field(
            name="`wishlist` — **card wishlist**",
            value="⭐ on **`/colv`** or **`/cv`** to wishlist a card. "
            "When someone drops or opens a wishlisted card in your server, you get tagged.\n"
            "**`/wishlist`** — view your wishlist. **`/wishlistremove`** — remove by name. "
            "Chat: **`wl`** / **`wlr`**.",
            inline=False,
        )
        e.set_footer(
            text="Other slash: /ping, /hello, /daily, /vote, /balance (+optional user), /leaderboard, /referral, /wishlist. "
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
