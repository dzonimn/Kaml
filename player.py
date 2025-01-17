import numpy as np
import time
import trueskill

from collections import namedtuple, OrderedDict
from itertools import chain
from math import log, exp, sqrt
from random import randint, sample
from random import random as rand
from scipy.stats import norm as Gaussian

from save_and_load import load_alias_tables, parse_mention_to_id, save_aliases
from utils import locking, logger
from utils import ChainedDict


PlayerState = namedtuple("PlayerState", ["rank", "mu", "sigma", "score"])


class Player:
    _id_counter = 0
    wins = 0
    losses = 0
    rank = None

    # TODO Better safeguard for invalid arg combinations
    def __init__(self, player_id=None, aliases=None, name=None):
        if name is not None:
            self.aliases = set([name])
            self.id = Player._id_counter
            self.mention = name
            self.claimed = False
            Player._id_counter += 1
        else:
            self.aliases = set(aliases)
            self.id = player_id
            self.claimed = True
            self.mention = list(aliases)[0]
        
        self.rating = trueskill.Rating()
        self.states = OrderedDict()
    
    def __hash__(self):
        return self.id
            
    def __str__(self):
        return f"Player {self.mention} (mu = {self.mu}, sigma = {self.sigma})"

    @property
    def mu(self):
        return self.rating.mu
    
    @property
    def ranks(self):
        return np.array([s.rank for s in self.states.values()] + [self.rank])

    @property
    def score(self):
        return self.mu - 3*self.sigma
    
    @property
    def scores(self):
        return np.array([s.score for s in self.states.values()] + [self.score])
    
    @property
    def sigma(self):
        return self.rating.sigma

    @property
    def times(self):
        return np.array(list(self.states.keys()) + [time.time()])
    
    @property
    def total_games(self):
        return self.wins + self.losses

    @property
    def variance(self):
        return self.sigma**2
    
    @property
    def win_ratio(self):
        return self.wins/self.total_games
    
    def save_state(self, timestamp, rank):
        self.states[float(timestamp)] = PlayerState(rank=rank,
                                             mu=self.mu,
                                             sigma=self.sigma,
                                             score=self.score)


class PlayerNotFoundError(Exception):
    def __init__(self, player_id):
        self.player_id = player_id
    
    def __str__(self):
        return f"No player found with identifier {self.player_id}."


class PlayerManager:
    # Core dicts
    alias_to_id = None
    id_to_player = None

    def __init__(self):
        self.alias_to_id = {}
        self.id_to_player = {}
    
    async def load_data(self):
        logger.info("Building PlayerManager.")
        logger.info("PlayerManager - Fetching alias tables.")

        self.alias_to_id, id_to_aliases = await load_alias_tables()
        self.id_to_player = dict()

        logger.info(f"PlayerManager - Constructing {len(id_to_aliases)} player objects.")

        for player_id, aliases in id_to_aliases.items():
            self.id_to_player[player_id] = Player(player_id=player_id,
                                                  aliases=aliases)
    
    def alias_exists(self, alias):
        return alias in self.alias_to_id

    @property
    def alias_to_player(self):
        return ChainedDict(self.alias_to_id, self.id_to_player)

    @property
    def aliases(self):
        return self.get_aliases()
    
    @property
    def claimed_aliases(self):
        return self.get_aliases(filter=lambda p: p.claimed)
    
    @property
    def claimed_players(self):
        return [p for p in self.players if p.claimed]

    @property
    def players(self):
        return list(self.id_to_player.values())
    
    @property
    def id_to_claimed_aliases(self):
        return {p.id:p.aliases for p in self.players if p.claimed}

    def add_player(self, name=None,
                   player_id=None,
                   aliases=None):
        if name is not None:
            player = Player(name=name)
        else:
            player = Player(player_id=player_id, aliases=aliases)

        player_id = player.id
        self.id_to_player[player_id] = player
        self.alias_to_id[name] = player_id

        return player

    def associate_aliases(self, player_id, aliases):
        # Assume that none of the aliases is already taken
        # Assume len(aliases) > 0

        found = [alias for alias in aliases if self.alias_exists(alias)]
        not_found = [alias for alias in aliases if not self.alias_exists(alias)]

        if not self.id_exists(player_id):
            self.add_player(player_id=player_id,
                            aliases=aliases)
        else:
            player = self.id_to_player[player_id]
            player.aliases.update(aliases)
            
        for alias in found:
            past_id = self.alias_to_id[alias]
            del self.id_to_player[past_id]
        
        for alias in aliases:
            self.alias_to_id[alias] = player_id
            
        save_aliases(self.id_to_claimed_aliases)

        return found, not_found

    def extract_claims(self, aliases):
        return {alias:self.alias_to_player[alias] for alias in aliases
                if self.is_claimed(alias)}

    def get_aliases(self, filter=lambda p: True):
        return set(chain.from_iterable([p.aliases for p in self.players
                                        if filter(p)]))

    def get_player(self, alias,
                   test_mention=False,
                   create_missing=True):
        player_id = None

        t = time.time()

        # Check if alias is a player id
        if isinstance(alias, int):
            player_id = alias
            if not self.id_exists(player_id):
                logger.error(f"No player found with id {player_id}.")
                raise PlayerNotFoundError(player_id)
            
            player = self.id_to_player[player_id]
        else:
            if test_mention:
                # Check if alias is a discord mention
                player_id = parse_mention_to_id(alias)

            if player_id is not None:
                player = self.id_to_player[player_id]
            elif not self.alias_exists(alias):
                if create_missing:
                    logger.debug(f"New player created with name {alias} in get_player.")
                    player = self.add_player(name=alias)
                else:
                    logger.error(f"No player found with name {player_id}.")
                    raise PlayerNotFoundError(alias)
            else:
                player = self.alias_to_player[alias]
        
        return player

    def id_exists(self, player_id):
        return player_id in self.id_to_player
    
    def is_claimed(self, alias):
        return alias in self.claimed_aliases