"""Conservative AA slots: describe participants, never calculate their shares."""

from __future__ import annotations

import re

from .schedule_time import chinese_number, instruction_text


AA_WORDS = re.compile(r"(?<![a-z])aa(?![a-z])|均分|分摊|平摊|收款单|分账|分帐", re.I)
SELF_WORDS = {"我", "本人", "自己"}
COUNT = re.compile(r"(?:总共|一共|共)?([零〇一二三四五六七八九十两\d]+)(?:个)?人")
MONEY = re.compile(r"[+\-−－＋¥￥\d.,，eE\s]+(?:元|块)")
PHONE = re.compile(r"1[3-9]\d{9}")
NON_EQUAL = re.compile(r"少付|多付|少出|多出|各付|各出|分别付|分别出|每人|一人\s*\d|比例|按份|不均分|不平摊|不平均|承担\s*\d|付\s*\d.*付\s*\d")


def wants_aa(message: str) -> bool:
    return bool(AA_WORDS.search(instruction_text(message)))


def _split_names(value: str, contact_names: list[str]) -> list[str]:
    """Retain unmatched names instead of silently reducing the participant list."""
    names = sorted(set(contact_names), key=len, reverse=True)
    parts = re.split(r"[、,，/\s]+|以及|还有|和|与|及", value)
    result: list[str] = []
    for part in parts:
        if not part:
            continue
        # Adjacent known names are common in short Chinese instructions. Unknown
        # spans are retained, so the service must resolve them or ask again.
        position = 0
        unknown = ""
        while position < len(part):
            phone = PHONE.match(part, position)
            token = phone.group() if phone else next(
                (name for name in names if part.startswith(name, position)), None,
            )
            if token is None and part[position] == "我":
                token = "我"
            if token:
                if unknown:
                    result.append(unknown)
                    unknown = ""
                result.append(token)
                position += len(token)
            else:
                unknown += part[position]
                position += 1
        if unknown:
            result.append(unknown)
    return result


def parse_aa_message(message: str, contact_names: list[str]) -> dict:
    """Extract partial slots, including short replies without an AA keyword.

    Names are untrusted raw tokens. Resolving contacts, deduplicating IDs,
    checking counts and allocating integer cents belong to the service.
    """
    # Local import reuses the transfer money grammar without a module cycle.
    from .agent import extract_amount_text

    text = instruction_text(message.strip())
    note_match = re.search(r"(?:备注|用途)(?:为|是|：|:)?\s*([^，。；;]+)", message)
    note = note_match[1].strip() if note_match else None
    if note is None:
        purpose = re.search(r"聚餐|房租|打车|旅游|餐费|团建|饭钱", text)
        note = purpose.group() if purpose else None

    count_match = COUNT.search(text)
    count = chinese_number(count_match[1]) if count_match else None
    if count is not None and count < 0:
        count = None
    non_equal = bool(NON_EQUAL.search(text) or len(MONEY.findall(text)) > 1)
    include_self: bool | None = None
    negative_self = re.search(r"不包括我|不包含我|不含我|不算我|我不(?:参与|参加|分摊|承担)|除我(?:以外|之外)?", text)
    if negative_self:
        include_self = False
    elif re.search(r"包括我|包含我|算上我|含我|我也(?:参与|参加|分摊|承担)|我(?:和|与|、)|(?:和|与|、)我(?:$|[，。；\s]|一起|[一二三四五六七八九十\d]+(?:个)?人|AA|aa)", text):
        include_self = True

    payer_is_self: bool | None = None
    payer = re.search(r"([^，,。；;\s、]+?)(?:先)?(?:垫付|垫了|付了|支付了|买单|付款)", text)
    if payer:
        payer_is_self = bool(re.search(r"我(?:自己|来)?$", payer[1])) and not payer[1].endswith("不是我")

    cleaned = text
    # Remove narrative payment clauses before looking for participant lists.
    cleaned = re.sub(r"[^，,。；;]*?(?:垫付|垫了|付了|支付了|买单|付款)\s*(?:[+\-−－＋¥￥\d.,eE\s]+(?:元|块))?", "", cleaned)
    cleaned = re.sub(r"不包括我|不包含我|不含我|不算我|我不(?:参与|参加|分摊|承担)|除我(?:以外|之外)?", "", cleaned)
    cleaned = re.sub(r"(?:包括我|包含我|算上我|含我|我也(?:参与|参加|分摊|承担))", "", cleaned)
    cleaned = MONEY.sub("", cleaned)
    cleaned = COUNT.sub("", cleaned)
    cleaned = AA_WORDS.sub("", cleaned)
    cleaned = re.sub(r"聚餐|房租|打车|旅游|餐费|团建|饭钱", "", cleaned)
    cleaned = re.sub(r"(?:帮我|请)(?:生成|建立|创建|发起)?|生成|建立|创建|发起|收款|一共|总共|一起|平均|均摊", "", cleaned)
    cleaned = re.sub(r"(?:参与人|参与者|成员|人员)(?:包括|为|是)?\s*[：:]?", "", cleaned)
    cleaned = re.sub(r"(?:让|由|找|向)|[：:。；;]", "，", cleaned)
    cleaned = re.sub(r"(?:来|吧|一下|帮忙|帮我)$", "", cleaned).strip(" ，,、和与")

    participants = _split_names(cleaned, contact_names) if cleaned else []
    if any(token in SELF_WORDS for token in participants):
        if not negative_self:
            include_self = True
        participants = [token for token in participants if token not in SELF_WORDS]
    # "我付了100元，和林悦、陈晨AA" explicitly joins the speaker to the
    # group. A standalone "我垫付" does not establish cost participation.
    if include_self is None and payer_is_self is True and re.search(r"(?:元|块)[，,\s]*和", text):
        include_self = True

    return {
        "amount_yuan": extract_amount_text(text),
        "participants": participants or None,
        "include_self": include_self,
        "participant_count": count,
        "note": note,
        "aa_non_equal": non_equal,
        "payer_is_self": payer_is_self,
    }
