from __future__ import annotations

import logging
import os
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# CONFIGURAZIONE
# ---------------------------------------------------------------------------

BOT_NAME = "Partnership Bot"
TOKEN_ENV_NAME = "DISCORD_TOKEN"

# Ruolo autorizzato a usare /crea-partnership.
# Puoi usare il nome del ruolo oppure, meglio ancora, il suo ID.
STAFFER_ROLE_NAME = "staffer"
STAFFER_ROLE_ID = 0

# Canale finale dove pubblicare le partnership.
# Se lasci 0, il bot cerca un canale chiamato PARTNERSHIP_CHANNEL_NAME.
# Se non lo trova, pubblica nel canale dove viene usato il comando.
PARTNERSHIP_CHANNEL_ID = 0
PARTNERSHIP_CHANNEL_NAME = "partnership"

# Per aggiornare subito i comandi in un solo server, inserisci qui l'ID server.
# Se lasci 0, Discord sincronizza il comando globalmente.
DEV_GUILD_ID = 0

# Lascia True mentre configuri il bot: se qualcosa fallisce, il messaggio
# privato mostrera anche il nome tecnico dell'errore.
SHOW_ERROR_DETAILS = True

COLOR_PANEL = discord.Color.from_rgb(86, 154, 255)
COLOR_SUCCESS = discord.Color.from_rgb(77, 214, 158)
COLOR_WARNING = discord.Color.from_rgb(255, 185, 94)


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(BOT_NAME)


# ---------------------------------------------------------------------------
# MODELLI E UTILITY
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PartnershipDraft:
    manager: discord.Member | discord.User
    creator: discord.Member | discord.User
    partner_intro: str
    created_at: datetime


def role_matches(member: discord.Member) -> bool:
    if STAFFER_ROLE_ID:
        return any(role.id == STAFFER_ROLE_ID for role in member.roles)

    expected_name = STAFFER_ROLE_NAME.casefold()
    return any(role.name.casefold() == expected_name for role in member.roles)


def has_staffer_role(user: discord.abc.User) -> bool:
    return isinstance(user, discord.Member) and role_matches(user)


def clean_text(value: str, *, limit: int, fallback: str = "Non specificato") -> str:
    cleaned = value.strip()
    if not cleaned:
        return fallback

    if len(cleaned) <= limit:
        return cleaned

    return f"{cleaned[: limit - 1]}…"


def split_for_embed(value: str, *, limit: int = 950) -> list[str]:
    cleaned = value.strip()
    if not cleaned:
        return ["Nessuna descrizione fornita."]

    chunks: list[str] = []
    remaining = cleaned

    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at == -1:
            split_at = limit

        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


def user_block(user: discord.abc.User) -> str:
    return f"{user.mention}\n`{user.name}`"


def timestamp(dt: datetime, style: str = "F") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


async def safe_ephemeral_response(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    """Invia una risposta privata senza causare InteractionResponded."""

    if interaction.response.is_done():
        await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        return

    await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=True)


async def resolve_partnership_channel(
    interaction: discord.Interaction,
) -> discord.TextChannel | discord.Thread | None:
    guild = interaction.guild
    if guild is None:
        return None

    if PARTNERSHIP_CHANNEL_ID:
        channel = guild.get_channel(PARTNERSHIP_CHANNEL_ID)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(PARTNERSHIP_CHANNEL_ID)
            except (discord.Forbidden, discord.HTTPException):
                channel = None

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel

    named_channel = discord.utils.get(guild.text_channels, name=PARTNERSHIP_CHANNEL_NAME)
    if named_channel is not None:
        return named_channel

    if isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        return interaction.channel

    return None


# ---------------------------------------------------------------------------
# EMBED
# ---------------------------------------------------------------------------


def permission_embed() -> discord.Embed:
    role_label = f"<@&{STAFFER_ROLE_ID}>" if STAFFER_ROLE_ID else f"@{STAFFER_ROLE_NAME}"
    embed = discord.Embed(
        title="✦ Accesso riservato",
        description=(
            f"⟡ Questo pannello e disponibile solo per chi possiede il ruolo {role_label}.\n"
            "❖ Se pensi sia un errore, contatta un responsabile dello staff."
        ),
        color=COLOR_WARNING,
    )
    embed.set_footer(text=f"{BOT_NAME} • controllo permessi")
    return embed


def setup_embed(view: PartnershipSetupView | None = None) -> discord.Embed:
    manager = view.manager.mention if view and view.manager else "`Non selezionato`"

    embed = discord.Embed(
        title="✦ Pannello Partnership",
        description=(
            "⟡ Seleziona il manager della partnership e poi apri il modulo "
            "per inserire il server partner."
        ),
        color=COLOR_PANEL,
    )
    embed.add_field(name="❖ Manager", value=manager, inline=True)
    embed.add_field(
        name="⚡ Prossimo passaggio",
        value="Usa il pulsante qui sotto per compilare nome o introduzione del server.",
        inline=False,
    )
    embed.set_footer(text="Modulo privato • visibile solo a te")
    return embed


def intro_saved_embed(draft: PartnershipDraft) -> discord.Embed:
    embed = discord.Embed(
        title="✦ Dettagli salvati",
        description=(
            "⟡ Le informazioni principali sono state registrate con successo.\n"
            "❖ Ora manca solo la descrizione completa del server partner.\n\n"
            f"✦ **Server partner**\n{draft.partner_intro}\n\n"
            f"⚡ **Manager assegnato**\n{draft.manager.mention} · `{draft.manager.name}`\n\n"
            "⟡ Premi il pulsante qui sotto per completare la partnership."
        ),
        color=COLOR_PANEL,
    )
    embed.set_footer(text="Secondo modulo • descrizione completa")
    return embed


def partnership_embed(
    *,
    guild: discord.Guild,
    draft: PartnershipDraft,
    description: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="✦ Nuova Partnership",
        description=(
            "⟡ Una nuova collaborazione e stata registrata ufficialmente.\n"
            "❖ Dettagli, referenti e informazioni operative sono raccolti qui sotto."
        ),
        color=COLOR_PANEL,
        timestamp=draft.created_at,
    )

    if guild.icon:
        embed.set_author(name=f"{BOT_NAME} • {guild.name}", icon_url=guild.icon.url)
        embed.set_thumbnail(url=guild.icon.url)
    else:
        embed.set_author(name=f"{BOT_NAME} • {guild.name}")

    embed.add_field(name="✦ Server partner", value=draft.partner_intro, inline=False)

    for index, chunk in enumerate(split_for_embed(description), start=1):
        field_name = "⚡ Descrizione completa" if index == 1 else f"⚡ Descrizione completa {index}"
        embed.add_field(name=field_name, value=chunk, inline=False)

    embed.add_field(name="⟡ Manager assegnato", value=user_block(draft.manager), inline=True)
    embed.add_field(name="✧ Creata da", value=user_block(draft.creator), inline=True)
    embed.add_field(
        name="❖ Data creazione",
        value=f"{timestamp(draft.created_at, 'F')}\n{timestamp(draft.created_at, 'R')}",
        inline=False,
    )
    embed.set_footer(text=f"{BOT_NAME} • partnership system")
    return embed


def success_embed(message: discord.Message) -> discord.Embed:
    embed = discord.Embed(
        title="✦ Partnership pubblicata",
        description=(
            "⟡ Il messaggio e stato inviato correttamente nel canale partnership.\n"
            "❖ Puoi aprirlo dal pulsante qui sotto."
        ),
        color=COLOR_SUCCESS,
    )
    embed.add_field(name="⚡ Link", value=f"[Apri messaggio]({message.jump_url})", inline=False)
    embed.set_footer(text=f"{BOT_NAME} • operazione completata")
    return embed


def error_embed(message: str) -> discord.Embed:
    embed = discord.Embed(
        title="✦ Operazione non completata",
        description=message,
        color=COLOR_WARNING,
    )
    embed.set_footer(text=f"{BOT_NAME} • controllo configurazione")
    return embed


def exception_details(error: BaseException) -> str:
    text = str(error).strip() or repr(error)
    return clean_text(f"{type(error).__name__}: {text}", limit=900)


def exception_info(error: BaseException) -> tuple[type[BaseException], BaseException, object | None]:
    return (type(error), error, error.__traceback__)


# ---------------------------------------------------------------------------
# VIEWS, SELECT MENU E MODAL
# ---------------------------------------------------------------------------


class ManagerSelect(discord.ui.UserSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Seleziona il manager della partnership",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, PartnershipSetupView):
            await safe_ephemeral_response(
                interaction,
                embed=error_embed("❖ Il pannello non e piu valido. Riapri il comando."),
            )
            return

        view.manager = self.values[0]
        await interaction.response.edit_message(embed=setup_embed(view), view=view)


class PartnershipSetupView(discord.ui.View):
    def __init__(self, owner: discord.Member | discord.User) -> None:
        super().__init__(timeout=300)
        self.owner = owner
        self.manager: discord.Member | discord.User | None = None

        self.add_item(ManagerSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await safe_ephemeral_response(
                interaction,
                embed=error_embed("⟡ Solo chi ha avviato il pannello puo usarlo."),
            )
            return False

        if not has_staffer_role(interaction.user):
            await safe_ephemeral_response(interaction, embed=permission_embed())
            return False

        return True

    @discord.ui.button(label="Compila server", emoji="📝", style=discord.ButtonStyle.primary)
    async def open_intro_modal(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if self.manager is None:
            await safe_ephemeral_response(
                interaction,
                embed=error_embed("❖ Seleziona prima il manager della partnership."),
            )
            return

        await interaction.response.send_modal(PartnerIntroModal(self))


class PartnerIntroModal(discord.ui.Modal):
    def __init__(self, setup_view: PartnershipSetupView) -> None:
        super().__init__(title="✦ Server partner")
        self.setup_view = setup_view
        self.partner_intro = discord.ui.TextInput(
            label="Nome server o breve introduzione",
            style=discord.TextStyle.paragraph,
            placeholder="Esempio: SkyHub Network - community gaming, eventi e chill...",
            min_length=2,
            max_length=450,
            required=True,
        )
        self.add_item(self.partner_intro)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.setup_view.owner.id:
            await safe_ephemeral_response(
                interaction,
                embed=error_embed("⟡ Solo chi ha avviato il pannello puo inviare questo modulo."),
            )
            return

        if not has_staffer_role(interaction.user):
            await safe_ephemeral_response(interaction, embed=permission_embed())
            return

        if self.setup_view.manager is None:
            await safe_ephemeral_response(
                interaction,
                embed=error_embed("❖ Il manager non e piu selezionato. Riapri il comando."),
            )
            return

        draft = PartnershipDraft(
            manager=self.setup_view.manager,
            creator=interaction.user,
            partner_intro=clean_text(str(self.partner_intro.value), limit=450),
            created_at=datetime.now(timezone.utc),
        )

        await interaction.response.send_message(
            embed=intro_saved_embed(draft),
            view=DescriptionView(draft, interaction.user.id),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Errore durante il modal iniziale", exc_info=error)
        await safe_ephemeral_response(
            interaction,
            embed=error_embed("❖ Non sono riuscito a salvare le informazioni iniziali."),
        )


class DescriptionView(discord.ui.View):
    def __init__(self, draft: PartnershipDraft, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.draft = draft
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await safe_ephemeral_response(
                interaction,
                embed=error_embed("⟡ Solo chi ha creato questa procedura puo completarla."),
            )
            return False

        if not has_staffer_role(interaction.user):
            await safe_ephemeral_response(interaction, embed=permission_embed())
            return False

        return True

    @discord.ui.button(label="Descrizione completa", emoji="📝", style=discord.ButtonStyle.primary)
    async def open_description_modal(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(PartnerDescriptionModal(self.draft, self.owner_id))


class PartnerDescriptionModal(discord.ui.Modal):
    def __init__(self, draft: PartnershipDraft, owner_id: int) -> None:
        super().__init__(title="⚡ Descrizione partnership")
        self.draft = draft
        self.owner_id = owner_id
        self.description = discord.ui.TextInput(
            label="Descrizione completa",
            style=discord.TextStyle.paragraph,
            placeholder="Descrivi community, tema, obiettivi, requisiti, note staff o link utili...",
            min_length=20,
            max_length=4000,
            required=True,
        )
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await safe_ephemeral_response(
                interaction,
                embed=error_embed("⟡ Solo chi ha iniziato la procedura puo pubblicarla."),
            )
            return

        if not has_staffer_role(interaction.user):
            await safe_ephemeral_response(interaction, embed=permission_embed())
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        if interaction.guild is None:
            await interaction.followup.send(
                embed=error_embed("❖ Questo comando puo essere usato solo dentro un server."),
                ephemeral=True,
            )
            return

        channel = await resolve_partnership_channel(interaction)
        if channel is None:
            await interaction.followup.send(
                embed=error_embed("❖ Non riesco a trovare un canale partnership valido."),
                ephemeral=True,
            )
            return

        embed = partnership_embed(
            guild=interaction.guild,
            draft=self.draft,
            description=clean_text(str(self.description.value), limit=4000),
        )

        try:
            message = await channel.send(
                content=f"✦ {self.draft.manager.mention}",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=[self.draft.manager],
                    roles=False,
                    everyone=False,
                ),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed(
                    "❖ Non ho i permessi per inviare messaggi o embed nel canale partnership."
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            log.exception("Discord ha rifiutato l'invio della partnership")
            await interaction.followup.send(
                embed=error_embed("❖ Discord ha rifiutato il messaggio. Riprova tra poco."),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=success_embed(message),
            view=JumpView(message.jump_url),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Errore durante il modal descrizione", exc_info=error)
        await safe_ephemeral_response(
            interaction,
            embed=error_embed("❖ Si e verificato un errore durante la pubblicazione."),
        )


class JumpView(discord.ui.View):
    def __init__(self, url: str) -> None:
        super().__init__(timeout=180)
        self.add_item(
            discord.ui.Button(
                label="Apri partnership",
                emoji="🔗",
                style=discord.ButtonStyle.link,
                url=url,
            )
        )


# ---------------------------------------------------------------------------
# BOT E COMANDI
# ---------------------------------------------------------------------------


class PartnershipBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=False,
                everyone=False,
            ),
        )

    async def setup_hook(self) -> None:
        if DEV_GUILD_ID:
            guild = discord.Object(id=DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Comandi slash sincronizzati nel server %s: %s", DEV_GUILD_ID, len(synced))
            return

        synced = await self.tree.sync()
        log.info("Comandi slash globali sincronizzati: %s", len(synced))

    async def on_ready(self) -> None:
        if self.user is None:
            return

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="✦ partnership e collaborazioni",
            )
        )
        log.info("Online come %s (%s)", self.user, self.user.id)


bot = PartnershipBot()


async def staffer_check(interaction: discord.Interaction) -> bool:
    return has_staffer_role(interaction.user)


@bot.tree.command(
    name="crea-partnership",
    description="Crea una partnership tramite pannello guidato.",
)
@app_commands.guild_only()
@app_commands.check(staffer_check)
async def crea_partnership(interaction: discord.Interaction) -> None:
    view = PartnershipSetupView(interaction.user)
    await interaction.response.send_message(embed=setup_embed(view), view=view, ephemeral=True)


@crea_partnership.error
async def crea_partnership_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    root_error = getattr(error, "original", error)

    if isinstance(root_error, app_commands.NoPrivateMessage):
        await safe_ephemeral_response(
            interaction,
            embed=error_embed("❖ Questo comando puo essere usato solo dentro un server."),
        )
        return

    if isinstance(root_error, app_commands.CheckFailure):
        await safe_ephemeral_response(interaction, embed=permission_embed())
        return

    log.error("Errore nel comando /crea-partnership", exc_info=exception_info(root_error))

    message = "❖ Si e verificato un errore durante l'apertura del pannello."
    if SHOW_ERROR_DETAILS:
        message += f"\n\n⚡ Dettaglio tecnico:\n`{exception_details(root_error)}`"

    await safe_ephemeral_response(
        interaction,
        embed=error_embed(message),
    )


def validate_environment(required_vars: Iterable[str]) -> None:
    missing = [
        name
        for name in required_vars
        if not os.getenv(name) or os.getenv(name) == "inserisci_il_token_del_bot_qui"
    ]

    if not missing:
        return

    formatted = ", ".join(missing)
    raise RuntimeError(
        textwrap.dedent(
            f"""
            Variabili ambiente mancanti: {formatted}
            Crea o aggiorna il file .env nella cartella del bot:

            DISCORD_TOKEN=il_tuo_token_discord
            """
        ).strip()
    )


if __name__ == "__main__":
    validate_environment([TOKEN_ENV_NAME])
    bot.run(os.environ[TOKEN_ENV_NAME], log_handler=None)
