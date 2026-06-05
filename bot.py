from __future__ import annotations

import logging
import os
import textwrap
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

# Ruolo autorizzato a usare il pannello partnership.
# Puoi usare il nome del ruolo oppure, meglio ancora, il suo ID.
STAFFER_ROLE_NAME = "staffer"
STAFFER_ROLE_ID = 0

# Canale privato dove pubblicare il pannello di controllo.
# Se lasci 0, il bot cerca un canale chiamato CONTROL_PANEL_CHANNEL_NAME.
CONTROL_PANEL_CHANNEL_ID = 0
CONTROL_PANEL_CHANNEL_NAME = "cmd-partnership"

# Canale finale dove pubblicare le partnership.
# Se lasci 0, il bot cerca un canale chiamato PARTNERSHIP_CHANNEL_NAME.
# Se non lo trova, il bot mostra un errore privato.
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


def split_plain_text(value: str, *, limit: int) -> list[str]:
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


def partnership_message_chunks(server_name: str, description: str) -> list[str]:
    header = f"**Partnership con {server_name}**\n\n"
    remaining = description.strip() or "Nessuna descrizione fornita."
    messages: list[str] = []
    first_message = True

    while remaining:
        prefix = header if first_message else ""
        limit = 2000 - len(prefix)

        if len(remaining) <= limit:
            messages.append(f"{prefix}{remaining}")
            break

        chunk = split_plain_text(remaining, limit=limit)[0]
        messages.append(f"{prefix}{chunk}")
        remaining = remaining[len(chunk) :].strip()
        first_message = False

    return messages


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

    return None


async def resolve_control_panel_channel(
    interaction: discord.Interaction,
) -> discord.TextChannel | discord.Thread | None:
    guild = interaction.guild
    if guild is None:
        return None

    if CONTROL_PANEL_CHANNEL_ID:
        channel = guild.get_channel(CONTROL_PANEL_CHANNEL_ID)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(CONTROL_PANEL_CHANNEL_ID)
            except (discord.Forbidden, discord.HTTPException):
                channel = None

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel

    named_channel = discord.utils.get(guild.text_channels, name=CONTROL_PANEL_CHANNEL_NAME)
    if named_channel is not None:
        return named_channel

    if (
        isinstance(interaction.channel, (discord.TextChannel, discord.Thread))
        and interaction.channel.name == CONTROL_PANEL_CHANNEL_NAME
    ):
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


def control_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="✦ Partnership Control Panel",
        description=(
            "⟡ Usa il pulsante qui sotto per inserire una nuova partnership."
        ),
        color=COLOR_PANEL,
    )
    embed.set_footer(text=f"{BOT_NAME} • canale {CONTROL_PANEL_CHANNEL_NAME}")
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


def panel_success_embed(message: discord.Message) -> discord.Embed:
    embed = discord.Embed(
        title="✦ Pannello pubblicato",
        description=(
            f"⟡ Il pannello e stato inviato correttamente in `{CONTROL_PANEL_CHANNEL_NAME}`.\n"
            "❖ Puoi aprirlo dal pulsante qui sotto."
        ),
        color=COLOR_SUCCESS,
    )
    embed.add_field(name="⚡ Link", value=f"[Apri pannello]({message.jump_url})", inline=False)
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
# VIEWS E MODAL
# ---------------------------------------------------------------------------


class PartnershipControlPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not has_staffer_role(interaction.user):
            await safe_ephemeral_response(interaction, embed=permission_embed())
            return False

        return True

    @discord.ui.button(
        label="Esegui partnership",
        emoji="📝",
        style=discord.ButtonStyle.primary,
        custom_id="partnership_control_panel:execute",
    )
    async def open_partnership_modal(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(PartnershipModal())


class PartnershipModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="✦ Nuova partnership")
        self.server_name = discord.ui.TextInput(
            label="Nome Server",
            style=discord.TextStyle.short,
            placeholder="Esempio: SkyHub Network",
            min_length=2,
            max_length=100,
            required=True,
        )
        self.server_description = discord.ui.TextInput(
            label="Descrizione Server",
            style=discord.TextStyle.paragraph,
            placeholder="Descrivi community, tema, obiettivi, requisiti o link utili...",
            min_length=10,
            max_length=4000,
            required=True,
        )
        self.add_item(self.server_name)
        self.add_item(self.server_description)

    async def on_submit(self, interaction: discord.Interaction) -> None:
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
                embed=error_embed(
                    f"❖ Non riesco a trovare un canale `{PARTNERSHIP_CHANNEL_NAME}` valido."
                ),
                ephemeral=True,
            )
            return

        server_name = clean_text(str(self.server_name.value), limit=100)
        server_description = clean_text(str(self.server_description.value), limit=4000)

        try:
            sent_messages = []
            for content in partnership_message_chunks(server_name, server_description):
                sent_messages.append(
                    await channel.send(
                        content=content,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed(
                    "❖ Non ho i permessi per inviare messaggi nel canale partnership."
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

        message = sent_messages[0]
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
        self.add_view(PartnershipControlPanelView())

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
    name="partnership-control-panel",
    description="Pubblica il pannello di controllo per creare partnership.",
)
@app_commands.guild_only()
@app_commands.check(staffer_check)
async def partnership_control_panel(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    channel = await resolve_control_panel_channel(interaction)
    if channel is None:
        await interaction.followup.send(
            embed=error_embed(
                f"❖ Non riesco a trovare un canale `{CONTROL_PANEL_CHANNEL_NAME}` valido."
            ),
            ephemeral=True,
        )
        return

    try:
        message = await channel.send(
            embed=control_panel_embed(),
            view=PartnershipControlPanelView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        await interaction.followup.send(
            embed=error_embed(
                f"❖ Non ho i permessi per inviare il pannello in `{CONTROL_PANEL_CHANNEL_NAME}`."
            ),
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        log.exception("Discord ha rifiutato l'invio del pannello partnership")
        await interaction.followup.send(
            embed=error_embed("❖ Discord ha rifiutato il pannello. Riprova tra poco."),
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        embed=panel_success_embed(message),
        view=JumpView(message.jump_url),
        ephemeral=True,
    )


@partnership_control_panel.error
async def partnership_control_panel_error(
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

    log.error(
        "Errore nel comando /partnership-control-panel",
        exc_info=exception_info(root_error),
    )

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
