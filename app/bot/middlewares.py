from typing import Any, Callable, Dict, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..services.settings import SettingsService
from ..services.context import ContextService
from ..services.interjector import InterjectorService
from ..services.persona import StylePromptService
from ..services.app_config import AppConfigService
from ..services.reactions import ReactionService
from ..services.roulette import RouletteService
from ..services.spontaneity import SpontaneityPolicy
from ..services.usage_limits import UsageLimiter
from ..services.user_memory import UserMemoryService
from ..services.guess_game import GuessGameService
from ..services.dice_game import DiceGameService
from ..services.monthly_champion import MonthlyChampionService
from ..services.roast import RoastService


class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sessionmaker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with self.sessionmaker() as session:
            data["session"] = session
            return await handler(event, data)


class ServicesMiddleware(BaseMiddleware):
    def __init__(
        self,
        settings: SettingsService,
        context: ContextService,
        interjector: InterjectorService,
        personas: StylePromptService,
        app_config: AppConfigService,
        reactions: ReactionService,
        roulette: RouletteService,
        usage_limits: UsageLimiter,
        memory: UserMemoryService,
        policy: SpontaneityPolicy,
        guess_game: GuessGameService,
        dice_game: DiceGameService,
        monthly_champion: MonthlyChampionService,
        roast: RoastService,
    ):
        self.settings = settings
        self.context = context
        self.interjector = interjector
        self.personas = personas
        self.app_config = app_config
        self.reactions = reactions
        self.roulette = roulette
        self.usage_limits = usage_limits
        self.memory = memory
        self.policy = policy
        self.guess_game = guess_game
        self.dice_game = dice_game
        self.monthly_champion = monthly_champion
        self.roast = roast

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        data["settings"] = self.settings
        data["context"] = self.context
        data["interjector"] = self.interjector
        data["personas"] = self.personas
        data["app_config"] = self.app_config
        data["reactions"] = self.reactions
        data["roulette"] = self.roulette
        data["usage_limits"] = self.usage_limits
        data["memory"] = self.memory
        data["policy"] = self.policy
        data["guess_game"] = self.guess_game
        data["dice_game"] = self.dice_game
        data["monthly_champion"] = self.monthly_champion
        data["roast"] = self.roast
        return await handler(event, data)
