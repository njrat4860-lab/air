from aiogram.fsm.state import State, StatesGroup


class SubmitStates(StatesGroup):
    waiting_content = State()


class ComplaintStates(StatesGroup):
    waiting_code = State()
    waiting_description = State()

