"""Conservative one-off time grammar; uncertain expressions require clarification."""

import re
from datetime import date, datetime, timedelta

from .clock import SHANGHAI


NUMBER = r"[零〇一二三四五六七八九十两\d]+"
PERIODS = r"上午|下午|晚上|傍晚|早上|早晨|凌晨|中午|明晚|今晚|明早|今早"
RELATIVES = {"今天": 0, "今晚": 0, "今早": 0, "明天": 1, "明晚": 1, "明早": 1, "后天": 2}
DATE_PATTERN = re.compile(
    r"(?<!\d)(?:(?P<iso_year>\d{4})[-/](?P<iso_month>\d{1,2})[-/](?P<iso_day>\d{1,2})"
    r"|(?:(?P<cn_year>\d{4})年)?(?P<cn_month>\d{1,2})月(?P<cn_day>\d{1,2})[日号]?)(?!\d)"
)
TIME_PATTERN = re.compile(
    rf"(?<![\d:：])(?:(?P<colon_hour>\d{{1,2}})[:：](?P<colon_minute>\d{{2}})(?!\d)"
    rf"|(?P<point_hour>{NUMBER})(?:点|时)(?:(?P<half>半)|(?P<point_minute>{NUMBER})分?)?)"
)
# Keep the future-intent gate and unsupported-syntax check on the same matcher:
# unsupported timing must always request clarification, never become immediate.
UNSUPPORTED_TIME_PATTERN = re.compile(
    r"每[天日周月年]|每个|工作日|周|星期|礼拜|[上下本]月|下个|月底|月末|大后天|"
    r"昨[天晚]|前天|[今明后去]年|有钱|以前|以后|之前|之后|稍后|稍晚|一会|等会|过会|"
    r"晚点|晚些|早些|过几|小时|分钟后|天后|未来|将来|届时|到时|提前|延后|推迟|延迟|"
    r"左右|前后|大概|大约|差不多|最晚|不晚于|不早于|刻|秒|夜里|半夜|深夜|清晨|午后|"
    r"明日|今日|明儿|隔天|隔日|翌日|春节|元旦|国庆|生日|发工资|到账后|等到|"
    r"UTC|GMT|时区|纽约|伦敦|东京|\d{1,2}[:：]\d{2}[:：]\d|\d{2}:\d{2}Z|"
    r"\d{5,}年|(?:点|分|\d{2}:\d{2})[前后]", re.IGNORECASE,
)


def instruction_text(text: str) -> str:
    # A memo is data, never a timing instruction.
    return re.split(r"(?:备注|用途)(?:为|是|：|:)?", text, maxsplit=1)[0].strip()


def wants_schedule(text: str) -> bool:
    text = instruction_text(text)
    return bool(UNSUPPORTED_TIME_PATTERN.search(text) or re.search(
        rf"预约|定时|{'|'.join(RELATIVES)}|{PERIODS}|分钟|"
        rf"\d{{1,4}}[-/]\d{{1,2}}|{NUMBER}(?:年|月|号|日|点|时)|\d{{1,2}}[:：]\d", text))


def chinese_number(text: str) -> int:
    if text.isdigit():
        return int(text)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    digits.update({"两": 2, "〇": 0})
    if text in digits:
        return digits[text]
    if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", text):
        left, right = text.split("十", 1)
        return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
    if re.fullmatch(r"[零〇][一二三四五六七八九]", text):
        return digits[text[1]]
    return -1


def _clarify(result: dict, message: str, *slots: str) -> dict:
    return {**result, **{slot: None for slot in slots}, "error": message}


def _hour_in_period(hour: int, period: str | None) -> int | None:
    if not period:
        return hour
    # Require a period that actually agrees with the clock. In particular,
    # "晚上12点" must not become noon or silently move to the following day.
    if period == "中午":
        return 12 if hour == 12 else None
    if period == "凌晨":
        return hour if 0 <= hour <= 6 else None
    if period in ("上午", "早上", "早晨", "明早", "今早"):
        return hour if 1 <= hour <= 11 else None
    if period == "下午":
        return hour + 12 if 1 <= hour <= 6 else hour if 13 <= hour <= 18 else None
    if period == "傍晚":
        return hour + 12 if 5 <= hour <= 7 else hour if 17 <= hour <= 19 else None
    return hour + 12 if 6 <= hour <= 11 else hour if 18 <= hour <= 23 else None


def parse_schedule(text: str, now: datetime, previous: dict | None = None, requested: bool = False) -> dict | None:
    text = instruction_text(text)
    temporal = wants_schedule(text)
    immediate = bool(re.search(r"立即|马上|现在|即时", text))
    if immediate and not temporal:
        return None
    if not previous and not requested and not temporal:
        return None
    # Only retain resolved slots. Errors are recomputed from the slots, so a
    # contact/amount reply cannot clear an invalid time and execute stale data.
    result = {key: previous[key] for key in ("day", "time") if previous and previous.get(key)}
    if immediate and temporal:
        return _clarify({}, "即时与预约时间冲突，请明确是现在转账，还是提供单次预约日期和时间。", "day", "time")
    if UNSUPPORTED_TIME_PATTERN.search(text):
        return _clarify({}, "请提供单次、明确的日期和 24 小时制时间，例如“10月6日20:00”（北京时间）；暂不支持周期、条件或模糊预约。", "day", "time")

    dates = list(DATE_PATTERN.finditer(text))
    relatives = list(re.finditer("|".join(RELATIVES), text))
    if len(dates) + len(relatives) > 1:
        return _clarify({}, "检测到多个日期，请只保留一个预约日期和时间。", "day", "time")
    found_day = bool(dates or relatives)
    if found_day:
        try:
            if relatives:
                day = now.astimezone(SHANGHAI).date() + timedelta(days=RELATIVES[relatives[0][0]])
            else:
                match = dates[0]
                day = date(int(match["iso_year"] or match["cn_year"] or now.astimezone(SHANGHAI).year),
                           int(match["iso_month"] or match["cn_month"]), int(match["iso_day"] or match["cn_day"]))
            result["day"] = day.isoformat()
        except ValueError:
            return _clarify(result, "这个日期不存在，请重新提供有效日期。", "day")
    # Unparsed date fragments (including alternatives such as “6日或7日”)
    # must not inherit an old day or silently use the first recognized day.
    date_remainder = DATE_PATTERN.sub("", text)
    date_remainder = re.sub("|".join(RELATIVES), "", date_remainder)
    if re.search(rf"{NUMBER}(?:年|月|号|日)|\d{{1,4}}[-/]\d", date_remainder):
        return _clarify(result, "请只提供一个完整的数字月份和日期，例如“10月6日20:00”。", "day")

    times = list(TIME_PATTERN.finditer(text))
    periods = re.findall(PERIODS, text)
    if len(times) > 1 or len(periods) > 1:
        return _clarify(result, "检测到多个或冲突的时间，请只保留一个时间，例如“20:00”。", "time")
    if re.search(rf"{NUMBER}(?:点|时)|\d{{1,2}}[:：]\d", TIME_PATTERN.sub("", text)):
        return _clarify(result, "时间表达不完整，请只提供一个时间，例如“20:00”。", "time")
    if times:
        match = times[0]
        colon = match["colon_hour"] is not None
        hour = int(match["colon_hour"]) if colon else chinese_number(match["point_hour"])
        minute = (int(match["colon_minute"]) if colon else 30 if match["half"]
                  else chinese_number(match["point_minute"]) if match["point_minute"] else 0)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return _clarify(result, "时间无效，请提供 00:00 至 23:59 之间的时间。", "time")
        period = periods[0] if periods else None
        if not colon and not period and 1 <= hour <= 12:
            return _clarify(result, "请说明上午还是下午，或使用 24 小时制，例如“20:00”。", "time")
        hour = _hour_in_period(hour, period)
        if hour is None:
            return _clarify(result, "时段与钟点含义不明确，请提供完整日期和 24 小时制时间；午夜请写对应日期的 00:00。", "time")
        result["time"] = f"{hour:02d}:{minute:02d}"
    elif periods or re.search(rf"{NUMBER}(?:点|时)|\d{{1,2}}[:：]\d", text):
        # "改成明天晚上" cannot reuse a previously supplied morning time.
        result.pop("time", None)

    if not result.get("day"):
        result["error"] = "请告诉我预约日期，例如“明天”，日期以页面显示的演示时间为准。"
    elif not result.get("time"):
        result["error"] = "请补充具体执行时间，例如“晚上8点”或“20:00”。"
    else:
        target = datetime.fromisoformat(f"{result['day']}T{result['time']}:00").replace(tzinfo=SHANGHAI)
        if target <= now:
            result["error"] = "预约时间已过去，请提供晚于当前演示时间的日期和时间。"
        else:
            result["execute_at"] = target.isoformat(timespec="seconds")
    return result

