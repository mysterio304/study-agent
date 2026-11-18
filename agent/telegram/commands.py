"""Read-only commands answered from templates — no LLM call (SPEC §10) — plus /undo,
/settings, /replan and /checkin."""

from __future__ import annotations

from datetime import timedelta

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from agent.flows import Services, propose_replan, send_proposal
from agent.planner.service import load_inputs, propose_undo
from agent.prep import checkin
from agent.prep.settings_service import SPECS, SettingsError, effective, set_value
from agent.telegram.cards import day_view, progress_view, usage_view
from agent.telegram.format import h
from agent.telegram.keyboards import checkin_kb

router = Router(name="commands")


@router.message(Command("today"))
async def cmd_today(message: Message, svc: Services) -> None:
    async with svc.sessionmaker() as s:
        await message.answer(await day_view(s, svc.settings, svc.clock.today()))


@router.message(Command("plan"))
async def cmd_plan(message: Message, svc: Services) -> None:
    today = svc.clock.today()
    async with svc.sessionmaker() as s:
        views = [await day_view(s, svc.settings, today + timedelta(days=i)) for i in range(3)]
    await message.answer("\n\n".join(views))


@router.message(Command("progress"))
async def cmd_progress(message: Message, svc: Services) -> None:
    async with svc.sessionmaker() as s:
        await message.answer(await progress_view(s))


@router.message(Command("usage"))
async def cmd_usage(message: Message, svc: Services) -> None:
    if svc.llm is None:
        await message.answer("LLM не настроен (нет ANTHROPIC_API_KEY).")
        return
    state = await svc.llm.budget()
    async with svc.sessionmaker() as s:
        await message.answer(await usage_view(s, svc.clock, state))


@router.message(Command("undo"))
async def cmd_undo(message: Message, svc: Services) -> None:
    async with svc.sessionmaker() as s:
        inputs = await load_inputs(s, svc.settings, svc.clock)
        p = await propose_undo(s, inputs, svc.clock)
        await s.commit()
    if p is None:
        await message.answer("Отменять нечего — это первая версия плана.")
        return
    await send_proposal(svc, p.id)


@router.message(Command("replan"))
async def cmd_replan(message: Message, svc: Services) -> None:
    if await propose_replan(svc, "Пересчёт по запросу") is None:
        await message.answer("Пересчёт дал тот же план — менять нечего.")


@router.message(Command("checkin"))
async def cmd_checkin(message: Message, svc: Services) -> None:
    async with svc.sessionmaker() as s:
        await checkin.ask(s, svc.clock.today())
        await s.commit()
    await message.answer(
        "Что сделал сегодня? Можно текстом или голосом.", reply_markup=checkin_kb()
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message, command: CommandObject, svc: Services) -> None:
    """`/settings` shows values; `/settings <key> <value>` changes one — including the locked
    non-negotiables, which can only be changed here, explicitly."""
    if command.args:
        key, _, value = command.args.partition(" ")
        async with svc.sessionmaker() as s:
            try:
                clean = await set_value(
                    s, key.strip(), value.strip(), svc.clock, by="user", allow_locked=True
                )
            except SettingsError as exc:
                await message.answer(h(str(exc)))
                return
            await s.commit()
        await message.answer(
            f"Готово: {h(SPECS[key.strip()].label_ru)} = {h(clean)}. Пересчитываю план…"
        )
        await propose_replan(svc, f"Пересчёт после /settings {key.strip()}")
        return
    async with svc.sessionmaker() as s:
        values = await effective(s, svc.settings)
    lines = ["<b>Настройки</b> (изменить: <code>/settings ключ значение</code>)"]
    for key, spec in SPECS.items():
        lock = "🔒 " if spec.locked else ""
        lines.append(f"{lock}<code>{key}</code> — {h(spec.label_ru)}: <b>{h(values[key])}</b>")
    lines.append("\n🔒 — неизменяемые правила: меняются только этой командой, не через чат.")
    await message.answer("\n".join(lines))
