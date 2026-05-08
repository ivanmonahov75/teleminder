import asyncio
import html
import logging
import os
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest


BASE_DIR = Path(__file__).resolve().parent
ALLOWED_USERS_FILE = BASE_DIR / "allowed_users.txt"
ENV_FILE = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
REMINDERS_FILE = DATA_DIR / "reminders.json"
ANNUAL_REMINDERS_FILE = DATA_DIR / "annual_reminders.json"
DEFAULT_TIMEZONE = "Europe/Moscow"
REMINDER_CHECK_INTERVAL_SECONDS = 30
REMINDER_STORE_LOCK = asyncio.Lock()
WEEKDAY_LABELS = {
    0: "Monday",
    1: "Tuesday",
    2: "Wednesday",
    3: "Thursday",
    4: "Friday",
    5: "Saturday",
    6: "Sunday",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def load_allowed_user_ids() -> set[int]:
    if not ALLOWED_USERS_FILE.exists():
        logger.warning("Allowed users file does not exist: %s", ALLOWED_USERS_FILE)
        return set()

    allowed_ids: set[int] = set()
    for line_number, raw_line in enumerate(ALLOWED_USERS_FILE.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        try:
            allowed_ids.add(int(line))
        except ValueError:
            logger.warning("Invalid Telegram user id in %s:%s: %r", ALLOWED_USERS_FILE, line_number, line)

    return allowed_ids


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in load_allowed_user_ids()


async def reject_unauthorized(update: Update) -> None:
    user = update.effective_user
    user_id = user.id if user else "unknown"
    username = user.username if user else None
    full_name = user.full_name if user else None

    logger.warning(
        "Unauthorized Telegram user: id=%s username=%r full_name=%r",
        user_id,
        username,
        full_name,
    )

    if update.effective_message:
        await update.effective_message.reply_text(
            "Access denied.\n"
            f"Your Telegram user ID is: {user_id}\n"
            "Ask the bot owner to add it to allowed_users.txt."
        )


def require_allowed(handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_allowed(update):
            await reject_unauthorized(update)
            return

        await handler(update, context)

    return wrapped


def get_timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("MAIN_TIMEZONE", DEFAULT_TIMEZONE))


def build_application(token: str) -> Application:
    builder = Application.builder().token(token)
    proxy_url = os.getenv("TELEGRAM_PROXY_URL")
    if proxy_url:
        logger.info("Using Telegram proxy: %s", proxy_url)
        builder = builder.request(HTTPXRequest(proxy_url=proxy_url))
        builder = builder.get_updates_request(HTTPXRequest(proxy_url=proxy_url))

    return builder.build()


def parse_time_input(raw_value: str) -> time | None:
    parts = raw_value.replace(":", " ").split()
    if len(parts) != 2:
        return None

    try:
        hour = int(parts[0])
        minute = int(parts[1])
        return time(hour=hour, minute=minute)
    except ValueError:
        return None


def parse_date_input(raw_value: str, timezone: ZoneInfo) -> datetime | None:
    value = raw_value.strip()
    formats = ("%d %m %Y", "%d.%m.%Y", "%Y-%m-%d")

    for date_format in formats:
        try:
            parsed = datetime.strptime(value, date_format)
            return parsed.replace(tzinfo=timezone)
        except ValueError:
            continue

    return None


def parse_annual_date_input(raw_value: str, timezone: ZoneInfo) -> tuple[int, int] | None:
    value = raw_value.strip()
    formats = ("%d %m", "%d.%m")

    for date_format in formats:
        try:
            parsed = datetime.strptime(value, date_format)
            return parsed.day, parsed.month
        except ValueError:
            continue

    dated = parse_date_input(value, timezone)
    if dated:
        return dated.day, dated.month

    return None


def parse_positive_int(raw_value: str) -> int | None:
    try:
        value = int(raw_value.strip())
    except ValueError:
        return None

    return value if value > 0 else None


def combine_date_and_time(date_value: datetime, time_value: time, timezone: ZoneInfo) -> datetime:
    return datetime(
        year=date_value.year,
        month=date_value.month,
        day=date_value.day,
        hour=time_value.hour,
        minute=time_value.minute,
        tzinfo=timezone,
    )


def event_at_from_flow(flow: dict, time_value: time, timezone: ZoneInfo) -> datetime:
    event_at = combine_date_and_time(flow["date"], time_value, timezone)
    if flow.get("date_source") == "weekday_button" and event_at <= datetime.now(timezone):
        event_at += timedelta(days=7)

    return event_at


def next_weekday_date(weekday: int, timezone: ZoneInfo):
    now = datetime.now(timezone)
    days_ahead = (weekday - now.weekday()) % 7
    return now.date() + timedelta(days=days_ahead)


def annual_event_at(day: int, month: int, year: int, time_value: time, timezone: ZoneInfo) -> datetime:
    return datetime(year=year, month=month, day=day, hour=time_value.hour, minute=time_value.minute, tzinfo=timezone)


def next_annual_event(day: int, month: int, time_value: time, timezone: ZoneInfo) -> datetime:
    now = datetime.now(timezone)
    candidate = annual_event_at(day, month, now.year, time_value, timezone)
    if candidate <= now:
        candidate = annual_event_at(day, month, now.year + 1, time_value, timezone)

    return candidate


def format_dt(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M")


def parse_iso_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def dt_to_iso(value: datetime) -> str:
    return value.isoformat()


def format_schedule(event_times: list[datetime], notification_times: list[datetime]) -> str:
    lines = ["<b><u>Notification done</u></b>", "", "Event time:"]
    lines.extend(f"{index}. {format_dt(event_at)}" for index, event_at in enumerate(event_times, start=1))
    lines.append("")
    lines.append("Bot will notify at:")
    lines.extend(f"{index}. {format_dt(notify_at)}" for index, notify_at in enumerate(notification_times, start=1))
    return "\n".join(lines)


def clear_make_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("make_flow", None)


def weekday_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(WEEKDAY_LABELS[0], callback_data="weekday:0"),
            InlineKeyboardButton(WEEKDAY_LABELS[1], callback_data="weekday:1"),
        ],
        [
            InlineKeyboardButton(WEEKDAY_LABELS[2], callback_data="weekday:2"),
            InlineKeyboardButton(WEEKDAY_LABELS[3], callback_data="weekday:3"),
        ],
        [
            InlineKeyboardButton(WEEKDAY_LABELS[4], callback_data="weekday:4"),
            InlineKeyboardButton(WEEKDAY_LABELS[5], callback_data="weekday:5"),
        ],
        [InlineKeyboardButton(WEEKDAY_LABELS[6], callback_data="weekday:6")],
    ]
    return InlineKeyboardMarkup(keyboard)


def date_selection_text(reminder_label: str) -> str:
    return (
        f"{reminder_label} selected.\n\n"
        "Choose a weekday button to use the next matching date, or send exact event date.\n"
        "Date formats: 08 05 2026, 08.05.2026, or 2026-05-08."
    )


def empty_reminder_store() -> dict:
    return {"next_id": 1, "reminders": []}


def load_reminder_store(path: Path) -> dict:
    if not path.exists():
        return empty_reminder_store()

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in %s. Treating it as empty.", path)
        return empty_reminder_store()

    if not isinstance(data, dict) or not isinstance(data.get("reminders"), list):
        logger.warning("Unexpected reminder store shape in %s. Treating it as empty.", path)
        return empty_reminder_store()

    data.setdefault("next_id", 1)
    return data


def save_reminder_store(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def add_reminder(path: Path, reminder: dict) -> dict:
    data = load_reminder_store(path)
    reminder = reminder.copy()
    reminder["id"] = data["next_id"]
    data["next_id"] += 1
    data["reminders"].append(reminder)
    save_reminder_store(path, data)
    return reminder


async def add_reminder_locked(path: Path, reminder: dict) -> dict:
    async with REMINDER_STORE_LOCK:
        return add_reminder(path, reminder)


def reminder_sort_key(reminder: dict) -> str:
    event_times = reminder.get("event_times") or []
    return event_times[0] if event_times else ""


def get_user_regular_reminders(user_id: int) -> list[dict]:
    reminders = load_reminder_store(REMINDERS_FILE)["reminders"]
    user_reminders = [reminder for reminder in reminders if reminder.get("user_id") == user_id]
    return sorted(user_reminders, key=reminder_sort_key)


def get_user_annual_reminders(user_id: int) -> list[dict]:
    reminders = load_reminder_store(ANNUAL_REMINDERS_FILE)["reminders"]
    user_reminders = [reminder for reminder in reminders if reminder.get("user_id") == user_id]
    return sorted(user_reminders, key=reminder_sort_key)


def make_reminder(
    user_id: int,
    kind: str,
    text: str,
    event_times: list[datetime],
    notification_times: list[datetime],
    timezone: ZoneInfo,
    extra: dict | None = None,
) -> dict:
    reminder = {
        "user_id": user_id,
        "kind": kind,
        "text": text,
        "event_times": [dt_to_iso(event_at) for event_at in event_times],
        "notifications": [{"at": dt_to_iso(notify_at), "sent": False} for notify_at in notification_times],
        "timezone": str(timezone),
        "created_at": dt_to_iso(datetime.now(timezone)),
    }
    if extra:
        reminder.update(extra)

    return reminder


def group_reminders_by_kind(reminders: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for reminder in reminders:
        grouped.setdefault(reminder.get("kind", "unknown"), []).append(reminder)

    return grouped


def format_stored_reminder(reminder: dict) -> str:
    first_event = reminder.get("event_times", [""])[0]
    parsed_event = format_dt(parse_iso_dt(first_event)) if first_event else "no event time"
    notifications = reminder.get("notifications", [])
    repeat_count = len(reminder.get("event_times", []))
    repeat_suffix = f", {repeat_count} events" if repeat_count > 1 else ""
    reminder_text = reminder.get("text", "No text")
    return f"#{reminder['id']} - {reminder_text} - {parsed_event}{repeat_suffix}, {len(notifications)} notifications"


def format_grouped_reminders(title: str, reminders: list[dict]) -> str:
    if not reminders:
        return f"{title}\n\nNo reminders."

    lines = [title]
    grouped = group_reminders_by_kind(reminders)
    for kind in sorted(grouped):
        lines.append("")
        lines.append(kind.capitalize())
        for index, reminder in enumerate(grouped[kind], start=1):
            lines.append(f"{index}. {format_stored_reminder(reminder)}")

    return "\n".join(lines)


def get_user_reminders(user_id: int) -> list[dict]:
    return get_user_regular_reminders(user_id)


def format_reminder_list(reminders: list[dict]) -> str:
    lines = ["Choose reminder to edit. Send its number:"]
    for index, reminder in enumerate(reminders, start=1):
        lines.append(f"{index}. {format_stored_reminder(reminder)}")

    return "\n".join(lines)


async def send_due_notifications(context: ContextTypes.DEFAULT_TYPE) -> None:
    async with REMINDER_STORE_LOCK:
        await send_due_notifications_from_file(context, REMINDERS_FILE)
        await send_due_notifications_from_file(context, ANNUAL_REMINDERS_FILE)


async def send_due_notifications_from_file(context: ContextTypes.DEFAULT_TYPE, path: Path) -> None:
    timezone = get_timezone()
    now = datetime.now(timezone)
    data = load_reminder_store(path)
    changed = False

    for reminder in data["reminders"]:
        for notification in reminder.get("notifications", []):
            if notification.get("sent"):
                continue

            try:
                notify_at = parse_iso_dt(notification["at"])
            except (KeyError, ValueError):
                logger.warning("Skipping invalid notification in %s for reminder id=%s", path, reminder.get("id"))
                continue

            if notify_at > now:
                continue

            try:
                await context.bot.send_message(
                    chat_id=reminder["user_id"],
                    text=format_notification_message(reminder, notify_at),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.exception("Failed to send reminder id=%s from %s", reminder.get("id"), path)
                continue

            notification["sent"] = True
            notification["sent_at"] = dt_to_iso(now)
            changed = True

    if changed:
        save_reminder_store(path, data)


def format_notification_message(reminder: dict, notify_at: datetime) -> str:
    reminder_text = html.escape(reminder.get("text", "Reminder"))
    return (
        "<b>Notification done</b>\n\n"
        f"{reminder_text}\n\n"
        f"Scheduled notification time: {format_dt(notify_at)}"
    )


@require_allowed
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Reminder bot is running.\n\n"
        "Reminder types:\n"
        "Fast - 5 minutes before and on time\n"
        "Pre-planned - day before at 19:30 and 20 minutes before\n"
        "Weekly - selected weekday/time for N weeks\n"
        "Annual - week before at 19:30 and day before at 19:30 for N years\n\n"
        "Commands:\n"
        "/make - create a reminder\n"
        "/edit - edit a reminder\n"
        "/list - show current reminders\n"
        "/list_annual - show annual reminders\n"
        "/remove - remove one reminder\n"
        "/clear_today - clear today's reminders\n"
        "/clear_all - clear all reminders"
    )


@require_allowed
async def make(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_make_state(context)

    keyboard = [
        [InlineKeyboardButton("Fast reminder", callback_data="make:fast")],
        [InlineKeyboardButton("Pre-planned reminder", callback_data="make:planned")],
        [InlineKeyboardButton("Weekly reminder", callback_data="make:weekly")],
        [InlineKeyboardButton("Annual reminder", callback_data="make:annual")],
    ]

    await update.message.reply_text(
        "Choose reminder type:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@require_allowed
async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reminders = get_user_regular_reminders(update.effective_user.id)
    await update.message.reply_text(format_grouped_reminders("Current reminders:", reminders))


@require_allowed
async def list_annual_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reminders = get_user_annual_reminders(update.effective_user.id)
    await update.message.reply_text(format_grouped_reminders("Annual reminders:", reminders))


@require_allowed
async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_make_state(context)
    context.user_data.pop("edit_flow", None)

    reminders = get_user_reminders(update.effective_user.id)
    if not reminders:
        await update.message.reply_text("No reminders to edit.")
        return

    context.user_data["edit_flow"] = {
        "step": "select_reminder",
        "reminders": reminders,
    }
    await update.message.reply_text(format_reminder_list(reminders))


@require_allowed
async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_remove_number"] = True

    await update.message.reply_text(
        "Choose reminder to remove:\n\n"
        "No reminders yet.\n\n"
        "Later this will show a numbered list. Send a number to remove a reminder."
    )


@require_allowed
async def clear_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [
            InlineKeyboardButton("Clear today", callback_data="clear:today:confirm"),
            InlineKeyboardButton("Cancel", callback_data="clear:cancel"),
        ]
    ]

    await update.message.reply_text(
        "Clear all reminders scheduled for today?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@require_allowed
async def clear_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [
            InlineKeyboardButton("Clear all", callback_data="clear:all:confirm"),
            InlineKeyboardButton("Cancel", callback_data="clear:cancel"),
        ]
    ]

    await update.message.reply_text(
        "Clear all reminders?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@require_allowed
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "make:fast":
        context.user_data["make_flow"] = {"kind": "fast", "step": "text"}
        await query.edit_message_text("Fast reminder selected.\n\nSend reminder text.")
    elif data == "make:planned":
        context.user_data["make_flow"] = {"kind": "planned", "step": "text"}
        await query.edit_message_text("Pre-planned reminder selected.\n\nSend reminder text.")
    elif data == "make:weekly":
        context.user_data["make_flow"] = {"kind": "weekly", "step": "text"}
        await query.edit_message_text("Weekly reminder selected.\n\nSend reminder text.")
    elif data == "make:annual":
        context.user_data["make_flow"] = {"kind": "annual", "step": "text"}
        await query.edit_message_text("Annual reminder selected.\n\nSend reminder text.")
    elif data.startswith("weekday:"):
        flow = context.user_data.get("make_flow")
        if not flow or flow.get("step") != "date":
            await query.edit_message_text("No active reminder setup expecting a date. Use /make to start again.")
            return

        timezone = get_timezone()
        weekday = int(data.split(":", maxsplit=1)[1])
        selected_date = next_weekday_date(weekday, timezone)
        flow["date"] = datetime.combine(selected_date, time.min, tzinfo=timezone)
        flow["date_source"] = "weekday_button"
        flow["step"] = "time"
        await query.edit_message_text(
            f"{WEEKDAY_LABELS[weekday]} selected: {selected_date.strftime('%d.%m.%Y')}.\n\n"
            "Send event time as: 13 45"
        )
    elif data.startswith("edit_keep:"):
        flow = context.user_data.get("edit_flow")
        if not flow:
            await query.edit_message_text("No active edit setup. Use /edit to start again.")
            return

        parameter = data.split(":", maxsplit=1)[1]
        flow.setdefault("kept_parameters", []).append(parameter)
        await query.edit_message_text(
            f"Kept current {parameter}.\n\n"
            "Edit preview only for now. Saving will be added with reminder storage."
        )
        context.user_data.pop("edit_flow", None)
    elif data == "clear:today:confirm":
        await query.edit_message_text("Today's reminders cleared. Placeholder only for now.")
    elif data == "clear:all:confirm":
        await query.edit_message_text("All reminders cleared. Placeholder only for now.")
    elif data == "clear:cancel":
        await query.edit_message_text("Cancelled.")


@require_allowed
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    edit_flow = context.user_data.get("edit_flow")
    if edit_flow:
        await handle_edit_flow_text(update, context, edit_flow)
        return

    flow = context.user_data.get("make_flow")
    if flow:
        await handle_make_flow_text(update, context, flow)
        return

    if context.user_data.pop("awaiting_remove_number", False):
        await update.message.reply_text(
            "Remove by number is not implemented yet.\n"
            f"You sent: {update.message.text}"
        )
        return

    await update.message.reply_text("Use /make, /list, /remove, /clear_today, or /clear_all.")


async def handle_edit_flow_text(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    if flow["step"] != "select_reminder":
        await update.message.reply_text("Edit state is invalid. Use /edit to start again.")
        context.user_data.pop("edit_flow", None)
        return

    selected_number = parse_positive_int(update.message.text)
    reminders = flow["reminders"]
    if not selected_number or selected_number > len(reminders):
        await update.message.reply_text(f"Invalid number. Send a number from 1 to {len(reminders)}.")
        return

    reminder = reminders[selected_number - 1]
    flow["step"] = "edit_parameter"
    flow["selected_reminder"] = reminder

    keyboard = [
        [InlineKeyboardButton("Keep current type", callback_data="edit_keep:type")],
        [InlineKeyboardButton("Keep current date/weekday", callback_data="edit_keep:date")],
        [InlineKeyboardButton("Keep current time", callback_data="edit_keep:time")],
        [InlineKeyboardButton("Keep current repeat count", callback_data="edit_keep:repeat_count")],
    ]
    await update.message.reply_text(
        "Reminder selected.\n\n"
        "For now this is an edit preview. Choose a parameter to keep unchanged:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_make_flow_text(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    timezone = get_timezone()
    text = update.message.text
    kind = flow["kind"]
    step = flow["step"]

    if step == "text":
        reminder_text = text.strip()
        if not reminder_text:
            await update.message.reply_text("Reminder text cannot be empty. Send reminder text.")
            return

        flow["text"] = reminder_text
        if kind == "annual":
            flow["step"] = "annual_date"
            await update.message.reply_text(
                "Send event date without year.\n"
                "Formats: 08 05 or 08.05."
            )
            return

        flow["step"] = "date"
        await update.message.reply_text(date_selection_text(f"{kind.capitalize()} reminder"), reply_markup=weekday_keyboard())
        return

    if step == "date":
        date_value = parse_date_input(text, timezone)
        if not date_value:
            await update.message.reply_text("Invalid date. Send date as 08 05 2026, 08.05.2026, or 2026-05-08.")
            return

        flow["date"] = date_value
        flow["date_source"] = "typed_date"
        flow["step"] = "time"
        await update.message.reply_text("Send event time as: 13 45")
        return

    if step == "annual_date":
        annual_date = parse_annual_date_input(text, timezone)
        if not annual_date:
            await update.message.reply_text("Invalid date. Send annual date as 08 05 or 08.05.")
            return

        flow["day"], flow["month"] = annual_date
        flow["step"] = "time"
        await update.message.reply_text("Send event time as: 13 45")
        return

    if step == "time":
        time_value = parse_time_input(text)
        if not time_value:
            await update.message.reply_text("Invalid time. Send time as: 13 45")
            return

        flow["time"] = time_value

        if kind == "weekly":
            flow["step"] = "weeks"
            await update.message.reply_text("How many weeks should this reminder run? Send a number, for example: 6")
            return

        if kind == "annual":
            flow["step"] = "years"
            await update.message.reply_text("How many years should this reminder run? Send a number, for example: 3")
            return

        event_at = event_at_from_flow(flow, time_value, timezone)
        if kind == "fast":
            notifications = [event_at - timedelta(minutes=5), event_at]
        elif kind == "planned":
            notifications = [
                datetime.combine((event_at - timedelta(days=1)).date(), time(19, 30), tzinfo=timezone),
                event_at - timedelta(minutes=20),
            ]
        else:
            await update.message.reply_text("Unknown reminder type. Use /make to start again.")
            clear_make_state(context)
            return

        saved = await add_reminder_locked(
            REMINDERS_FILE,
            make_reminder(update.effective_user.id, kind, flow["text"], [event_at], notifications, timezone),
        )
        await update.message.reply_text(
            f"Saved reminder #{saved['id']}.\n\n{format_schedule([event_at], notifications)}",
            parse_mode=ParseMode.HTML,
        )
        clear_make_state(context)
        return

    if step == "weeks":
        weeks = parse_positive_int(text)
        if not weeks:
            await update.message.reply_text("Invalid number of weeks. Send a positive number, for example: 6")
            return

        first_event = event_at_from_flow(flow, flow["time"], timezone)
        event_times = [first_event + timedelta(weeks=week_index) for week_index in range(weeks)]
        saved = await add_reminder_locked(
            REMINDERS_FILE,
            make_reminder(
                update.effective_user.id,
                "weekly",
                flow["text"],
                event_times,
                event_times,
                timezone,
                {"weeks": weeks},
            ),
        )
        await update.message.reply_text(
            f"Saved reminder #{saved['id']}.\n\n{format_schedule(event_times, event_times)}",
            parse_mode=ParseMode.HTML,
        )
        clear_make_state(context)
        return

    if step == "years":
        years = parse_positive_int(text)
        if not years:
            await update.message.reply_text("Invalid number of years. Send a positive number, for example: 3")
            return

        first_event = next_annual_event(flow["day"], flow["month"], flow["time"], timezone)
        event_times = [
            annual_event_at(flow["day"], flow["month"], first_event.year + year_index, flow["time"], timezone)
            for year_index in range(years)
        ]
        notifications = []
        for event_at in event_times:
            notifications.append(datetime.combine((event_at - timedelta(days=7)).date(), time(19, 30), tzinfo=timezone))
            notifications.append(datetime.combine((event_at - timedelta(days=1)).date(), time(19, 30), tzinfo=timezone))

        saved = await add_reminder_locked(
            ANNUAL_REMINDERS_FILE,
            make_reminder(
                update.effective_user.id,
                "annual",
                flow["text"],
                event_times,
                notifications,
                timezone,
                {"years": years, "day": flow["day"], "month": flow["month"]},
            ),
        )
        await update.message.reply_text(
            f"Saved annual reminder #{saved['id']}.\n\n{format_schedule(event_times, notifications)}",
            parse_mode=ParseMode.HTML,
        )
        clear_make_state(context)
        return

    await update.message.reply_text("Setup state is invalid. Use /make to start again.")
    clear_make_state(context)


def main() -> None:
    load_dotenv(ENV_FILE)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN environment variable.")

    application = build_application(token)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("make", make))
    application.add_handler(CommandHandler("edit", edit))
    application.add_handler(CommandHandler("list", list_reminders))
    application.add_handler(CommandHandler("list_annual", list_annual_reminders))
    application.add_handler(CommandHandler("remove", remove))
    application.add_handler(CommandHandler("clear_today", clear_today))
    application.add_handler(CommandHandler("clear_all", clear_all))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.job_queue.run_repeating(
        send_due_notifications,
        interval=REMINDER_CHECK_INTERVAL_SECONDS,
        first=REMINDER_CHECK_INTERVAL_SECONDS,
        name="send_due_notifications",
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
