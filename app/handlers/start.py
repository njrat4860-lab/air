from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.keyboards.user import main_menu
from app.states import SubmitStates

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.WELCOME, reply_markup=main_menu())


@router.callback_query(F.data == "menu:gossip")
async def on_gossip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SubmitStates.waiting_content)
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.ASK_POST_CONTENT)
    else:
        bot = callback.bot
        assert bot is not None
        await bot.send_message(callback.from_user.id, texts.ASK_POST_CONTENT)
    await callback.answer()
