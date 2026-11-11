"""Deterministic user-message triage. Advisory only: originals are always archived.

Classes decide where a paragraph lands in the task card, never whether it is kept.
The rules come from measured native histories: roughly a third of retained user
messages were acknowledgements, automation notices, duplicates or image-only turns.
"""
from __future__ import annotations

import re

AUTOMATED = (
    re.compile(r"^\s*<(environment_context|user_instructions|turn_aborted|user_shell_command|subagent_notification|context_window)\b"),
    re.compile(r"^\s*The user flagged that you might have exceeded"),
    re.compile(r"^\s*【[^】\n]{0,60}(守卫|event=|自动|通知|hook|heartbeat|心跳|巡检)[^】\n]{0,80}】", re.I),
    # [BCC supervisor]: the supervisor's message that woke Codex (codex queue) arrives as a user message
    re.compile(r"^\s*\[(automated|automation|bot|hook|system|heartbeat|cron|watchdog|bcc supervisor)\b[^\]\n]{0,60}\]", re.I),
)
ACK = re.compile(r"(好|好的|好吧|ok|okay|嗯+|行|可以|继续|继续吧|收到|谢谢|多谢|thanks?|thank you|yes|y|对|是的|没问题|go|go on|"
                 r"开始|开始吧|执行|执行吧|确认|同意|lgtm)[\s!！。.~，,]*", re.I)
CORRECTION = re.compile(r"(不对|不是这样|搞错|错了|又(出现|坏|回|没|卡|引入|丢|变)|还是(没|不|有问题|不行)|怎么又|仍然(没|不|有)|依然(没|不|有)|"
                        r"并没有|没有按|不是我要|我说的是|我说的不是|跑偏|回退了|又一次|\bregress|still (broken|failing|not)|"
                        r"that'?s wrong|not what i|"
                        # "不用搞这么复杂", "全选 + del 就完事了": the work is over-built (seen in a real session)
                        r"(?:不用|不要|别|没必要|不需要)(?:搞|弄|做|写)?(?:得)?(?:这么|那么|太)(?:复杂|麻烦)|太复杂|搞复杂|过度设计|"
                        r"就完事了|没必要(?:备份|检查|分析)|\bover-?engineer|\btoo complicated\b|\bkeep it simple\b)", re.I)
# Standing rules need a durable marker. Prohibition words alone also appear inside
# investigative requests ("check whether we should forbid X"), so those only count
# when the paragraph is not a question or an investigation.
# "以后" usually just means "after"; only its standing forms count.
STANDING = re.compile(r"((?:^|[，,。；;：:\s])以后|以后(?:都|每次|一律|不要|别|请|要|必须|再也|也要|统一)|今后|从现在起|从今往后|每次|每当|始终|一律|永远|记住|切记|"
                      r"\balways\b|\bnever\b|from now on|every time|going forward)", re.I)
PROHIBIT = re.compile(r"(不要再|别再|禁止|严禁|不得|不准|不许|\bmust not\b|do not ever)", re.I)
INQUIRY = re.compile(r"([?？]|吗|是否|是不是|看看|研究|检查下|调查|why\b|whether\b|should we\b)", re.I)
QUESTION = re.compile(r"(([?？]|吗|么|呢)\s*$|^\s*(看看|研究|检查|是否|能不能|可不可以|为什么|why\b|should\b|can\b|could\b))", re.I)
WRAPPED = re.compile(r"^\s*<[a-z_]+>")  # client-wrapped payloads such as question replies
HEADING = re.compile(r"(?:#{1,6}\s+.+|[一二三四五六七八九十]+、[^\n。！？!?]{1,50})")



def message_class(text: str) -> str | None:
    """Whole-message classes. None means classify paragraph by paragraph."""
    head = text.lstrip()[:240]
    if any(pattern.search(head) for pattern in AUTOMATED):
        return "automated"
    if ACK.fullmatch(text.strip()):
        return "ack"
    return None


def paragraph_class(text: str) -> str:
    """A standing rule outlives the complaint that introduced it, so it wins."""
    stripped = text.strip()
    if HEADING.fullmatch(stripped):
        return "heading"
    if is_standing_rule(stripped):
        return "constraint"
    if CORRECTION.search(stripped):
        return "correction"
    return "task"


def is_standing_rule(text: str) -> bool:
    if WRAPPED.match(text) or len(text) < 6:
        return False  # short answers ("都一律通过") and structured replies are not rules
    if STANDING.search(text):
        # Strong markers survive quoted questions ("if a customer asks X, always answer Y");
        # only a paragraph that is itself a question is excluded.
        return not QUESTION.search(text)
    return bool(PROHIBIT.search(text)) and not INQUIRY.search(text)


def is_correction(text: str) -> bool:
    return bool(CORRECTION.search(text))


# A redirect: the user stops the current direction as a whole (the plan, the approach, the design) and wants it
# replaced, not repaired. A local complaint ("the button is off", "重构这个函数") is a correction or a task.
_WHOLE = r"(?:整个|全部|完全|彻底|整体|根本|一开始就|从一开始)"
_WORK = r"(?:方案|计划|思路|方向|做法|设计|架构|路线|实现方式|这套|这一套|这些改动|改动|工作)"
_STOP = r"(?:停下|停止|先停|停一下|暂停|别做了|不要做了|别再做)"
REDIRECT = re.compile(
    r"推倒重来|推翻重来|推翻(?:之前|前面|原来|现在|当前|目前|这个|这套|全部)|从头(?:再)?来|从零(?:开始|重来|再来)|重新来过|重头来|全部重来|全部重做|"
    rf"(?:之前|前面|原来|现在|当前|目前|这个|这套|你的|上面的|刚才的)的?{_WORK}{_WHOLE}?(?:都|全)?(?:作废|废弃|废掉|不要了|不用了|推翻|放弃|错了|不对|不行)|"
    rf"{_WORK}{_WHOLE}?(?:都|全)?(?:作废|废弃|废掉|推翻|放弃)|"
    rf"(?:方向|思路|路线|架构|设计){_WHOLE}?(?:错了|错误|偏了|跑偏|走偏|不对|有问题)|"
    rf"{_WHOLE}(?:跑偏|走偏|偏离|偏了|搞错了方向)|偏离了?(?:目标|需求|方向|我的意思|初衷|要求)|"
    rf"{_STOP}.{{0,24}}(?:重新(?:设计|规划|构思|架构|来|做|思考|考虑)|重构|重来|重做|推倒|换(?:个|一个)?(?:思路|方案|方向|做法))|"
    r"\bstart (?:over|again|from scratch)\b|\bback to (?:the )?drawing board\b|\bscrap (?:it|this|that|everything|all|the|your|our|what)\b|"
    r"\bthrow (?:it|this|that|everything|all of (?:it|this)) (?:away|out)\b|\b(?:gone|went|going|veered|drifted|is|are|got) off[- ]track\b|"
    r"\bdrift(?:ed|ing)? (?:away )?from (?:what|the|my)\b|\bwrong direction\b|\boff course\b|"
    r"\babandon (?:the|this|that|your|our|the current) (?:plan|approach|direction|design|work)\b|"
    r"\b(?:stop|halt)\b.{0,48}\b(?:rethink|restructure|re-?design|re-?plan|re-?architect|start over|redo)\b",
    re.I)
# "不用推倒重来", "don't start over", "no need to scrap it": the opposite.
_NEGATED = re.compile(r"(?:不用|不要|无需|没必要|不必|别|不需要|用不着|没有|并没|并非|don'?t|do not|no need to|not|never|without|shouldn'?t)"
                      r"\s*(?:\S{0,6}\s*){0,2}$", re.I)
_QUOTED = re.compile(r"^\s*(?:>|```|~~~|    |\t)")


def own_sentences(text: str) -> list[str]:
    """The sentences of the user's own words: pasted material (code blocks, quoted or indented lines, paragraphs
    longer than a short instruction) is left out."""
    fenced, own = False, []
    for line in (text or "").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced and not _QUOTED.match(line):
            own.append(line)
    sentences = []
    for paragraph in re.split(r"\n\s*\n", "\n".join(own)):
        paragraph = paragraph.strip()
        if paragraph and len(paragraph) <= 600:
            sentences += [s for s in re.split(r"(?<=[。！？!?；;])\s*|(?<=[.])\s+|\n", paragraph) if s.strip()]
    return sentences


def is_redirect(text: str) -> bool:
    """Whether the user's own words stop the current direction as a whole. Pasted material, questions
    ("要不要推倒重来？") and negations ("不用推倒重来") do not count; neither does a local complaint (a correction)."""
    for sentence in own_sentences(text):
        match = REDIRECT.search(sentence)
        if match and not QUESTION.search(sentence) and not _NEGATED.search(sentence[:match.start()]):
            return True
    return False


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
