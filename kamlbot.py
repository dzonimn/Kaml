import discord
import time

from datetime import datetime

from discord import Embed, Message, TextChannel
from discord.ext import commands
from discord.ext.commands import Bot

from itertools import chain

from player import PlayerManager, PlayerNotFoundError
from ranking import Ranking
from save_and_load import load_messages, load_tokens, parse_matchboard_msg
from utils import callback, connect, locking, logger


tokens = load_tokens()
ROLENAME = "Chamelier"


# TODO add methods that should use the ones in the ranking
class Kamlbot(Bot):
    def __init__(self, *args, **kwargs):
        connect("ranking_updated", self.edit_leaderboard)
        connect("game_registered", self.send_game_result)

        self.player_manager = None
        self.ranking = None
        self.is_ready = False
        self.maintenance_mode = False

        super().__init__(*args, **kwargs)

    async def debug(self, msg):
        await self.debug_chan.send(msg)
        logger.debug(msg)
    
    async def edit_leaderboard(self):
        msg_id = self.leaderboard.last_message_id
        msg = await self.leaderboard.fetch_message(msg_id)
        await msg.edit(content=self.leaderboard_content(1, 20))
    
    def get_player(self, *args, **kwargs):
        return self.player_manager.get_player(*args, **kwargs)
    
    async def info(self, msg):
        await self.info_chan.send(msg)
    
    def leaderboard_content(self, start, stop):
        # Convert from base 1 indexing for positive ranks
        if start >= 0:
            start -= 1

        new_content = "\n".join([self.message("leaderboard_line",
                                              rank=self.player_rank(player),
                                              player=player)
                                 for player in self.ranking[start:stop]])

        return f"```\n{new_content}\n```"

    def message(self, msg_name, **kwargs):
        return self.messages[msg_name].format(**kwargs)

    async def on_message(self, msg):
        if msg.channel == self.matchboard:
            if self.is_ready:
                game = parse_matchboard_msg(msg)
                if game is not None:
                    await self.ranking.register_game(game)

        elif msg.guild.id == tokens["kaml_server_id"]:
            await self.process_commands(msg)

    async def on_ready(self):
        if self.player_manager is not None:
            print("Too much on_ready")
            return

        for chan in self.get_guild(tokens["kaml_server_id"]).channels:
            if chan.name == "debug":
                self.debug_chan = chan
            
            if chan.name == "kamlboard":
                self.kamlboard = chan
            
            if chan.name == "leaderboard":
                self.leaderboard = chan
            
            if chan.name == "general":
                self.info_chan = chan
        
        for chan in self.get_guild(tokens["pw_server_id"]).channels:
            if chan.name == "matchboard":
                self.matchboard = chan

        await self.change_presence(status=discord.Status.online)
        await self.debug("The Kamlbot is logged in.")
        logger.info(f"Kamlbot has logged in.")
        start_time = time.time()

        self.messages = load_messages()

        self.player_manager = PlayerManager()
        await self.player_manager.load_data()
        await self.update_mentions()

        self.ranking = Ranking(self.player_manager)

        await self.ranking.fetch_data(self.matchboard)

        dt = time.time() - start_time

        logger.info(f"Initialization finished in {dt:0.2f} s.")
        await self.debug(f"Initialization finished in {dt:0.2f} s.")
        self.is_ready = True
        await self.info("The Kamlbot is ready to rock and ready to rank!")

    def player_rank(self, player):
        return self.ranking.player_to_rank.get(player, "[unkown]")

    @property
    def players(self):
        return self.player_manager.players
    
    async def send_game_result(self, change):
        msg = self.message("game_result_description",
                           change=change,
                           winner=change.winner,
                           loser=change.loser)

        embed = Embed(color=0xf36541,
                    timestamp=datetime.now(),
                    title=self.message("game_result_title"),
                    description=msg)

        embed.set_footer(text="")
        await self.kamlboard.send(embed=embed)

    @locking("mentions")
    async def update_mentions(self):
        for player in self.player_manager.claimed_players:
            user = await self.fetch_user(player.id)
            player.mention = user.display_name
            self.player_manager.alias_to_id[player.mention] = player.id
            

kamlbot = Kamlbot(command_prefix="!")

# TODO score change is displayed incorrectly only take in account diff for mu
# TODO implement admin reload command
# TODO give "Number one" rank to the top player

JET_ALIASES = ["\#LegalizeEdgyMemes", "JetEriksen", "KSR JetEriksen"]

@kamlbot.check
async def check_available(cmd):
    if kamlbot.is_ready:
        return True
    else:
        await cmd.channel.send("Kamlbot not ready, please wait a bit.")
        return False

@kamlbot.command(help="""
Associate in game name(s) to the user's discord profile.

Multiple names can be given at once.
""")
async def alias(cmd, *names):
    user = cmd.author

    logger.info("{0.mention} claims names {1}".format(user, names))

    if user.id == tokens["jet_id"] and any([name not in JET_ALIASES for name in names]):
        msg = kamlbot.message("anti_jet_meme")
        await cmd.channel.send(msg)
        return
    
    if len(names) == 0:
        await cmd.channel.send("Please give me at least one alias...")
        return
    
    taken = kamlbot.player_manager.extract_claims(names)

    if len(taken) > 0:
        taken_list = [kamlbot.message("taken_alias",
                                      alias=name,
                                      player=player)
                      for name, player in taken.items()]
        msg = kamlbot.message("not_associated_aliases",
                              n=len(taken),
                              taken_aliases="\n".join(taken_list))
        await cmd.channel.send(msg)
        return

    added, not_found = await kamlbot.player_manager.associate_aliases(user.id, names)
    player = kamlbot.get_player(user.id)
    await kamlbot.update_mentions()

    if len(player.aliases) > 0:
        msg = kamlbot.message("associated_aliases",
                              user=user,
                              aliases="\n".join(player.aliases))
    else:
        msg = kamlbot.message("no_alias_error", user=user)

    if len(not_found) > 0:
        msg += kamlbot.message("not_found_aliases",
                               aliases="\n".join(not_found))

    await cmd.channel.send(msg)


@kamlbot.command(help="""
Show the leaderboard between two ranks (maximum 30 lines).
""")
async def leaderboard(cmd, start, stop):
    try:
        start = int(start)
        stop = int(stop)
    except ValueError:
        await cmd.channel.send("Upper and lower rank should be integers.")
        return
    
    if stop - start > 30:
        await cmd.channel.send("At most 30 line can be displayed at once in leaderboards.")
        return

    await cmd.channel.send(kamlbot.leaderboard_content(start, stop))


@kamlbot.command(help="""
Compare two players, including the probability of win estimated by the Kamlbot.
""")
async def compare(cmd, p1_name, p2_name):
    try:
        p1 = kamlbot.get_player(p1_name,
                                test_mention=True,
                                create_missing=False)

    except PlayerNotFoundError:
        msg = kamlbot.message("player_not_found_error",
                              player_name=p1_name)
        await cmd.channel.send(msg)
        return
    
    try:
        p2 = kamlbot.get_player(p2_name,
                                test_mention=True,
                                create_missing=False)

    except PlayerNotFoundError:
        msg = kamlbot.message("player_not_found_error",
                              player_name=p2_name)
        await cmd.channel.send(msg)
        return

    msg = kamlbot.message("player_rank",
                          player=p1,
                          rank=kamlbot.player_rank(p1))

    msg += "\n" + kamlbot.message("player_rank",
                                  player=p2,
                                  rank=kamlbot.player_rank(p2))
    
    
    comparison = kamlbot.ranking.comparison(p1, p2)

    if comparison is not None:
        msg += "\n" + kamlbot.message("win_probability",
                                      p1=p1,
                                      p2=p2,
                                      comparison=comparison)
    else:
        msg += "\n" + kamlbot.message("win_probability_blind",
                                      p1=p1,
                                      p2=p2,
                                      win_estimate=100*p1.win_estimate(p2))
    
    await cmd.channel.send(msg)


@kamlbot.command(help="""
Return the rank and some additional information about the player.

If used without argument, return the rank of the player associated with the
discord profile of the user.
""")
async def rank(cmd, player_name=None):
    if player_name is None:
        player_name = cmd.author.id

    try:
        player = kamlbot.get_player(player_name,
                                    test_mention=True,
                                    create_missing=False)

    except PlayerNotFoundError:
        msg = kamlbot.message("player_not_found_error",
                              player_name=player_name)
        await cmd.channel.send(msg)
        return

    msg = kamlbot.message("player_rank",
                          player=player,
                          rank=kamlbot.player_rank(player))
    await cmd.channel.send(msg)


@kamlbot.command(help="""
[Admin] Stop the bot.
""")
@commands.has_role(ROLENAME)
async def stop(cmd):
    print("Disconnecting Kamlbot")
    await kamlbot.change_presence(activity=None, status=discord.Status.offline)
    await kamlbot.info("The Kamlbot takes his leave.")
    logger.info("Disconnecting Kamlbot.")
    await kamlbot.close()


@kamlbot.command(help="""
[Admin] Test the bot.
""")
@commands.has_role(ROLENAME)
async def test(cmd):
    logger.info("The Kamlbot is being tested.")
    await cmd.channel.send("The Kamlbot is working.")

kamlbot.run(tokens["bot_token"])