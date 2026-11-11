"""Drift read from the user's feedback: the work has moved away from what the user wants when the user shows
strong dissatisfaction with it (repeated "I told you", anger or insults aimed at the work, exasperation with
emphasis) or raises the same problem again and again. Then the model must stop, find the root cause and re-plan
(card._feedback, card.drift_note). Like every classification it is advisory: the message is archived either way.

Only the user's own words count (classify.own_sentences: no code blocks, quotes or pasted logs). The score adds
up per sentence: a strong signal counts 2, exasperation or an insult 1, or 2 when aimed at the work ("你", "you");
emphatic punctuation and shouting 1 each. A message scoring 2 is strong dissatisfaction. The same problem is the
current message plus earlier feedback in an unbroken run (corrections, repeats, complaints; acknowledgements do
not break it) and any earlier message of the window that says nearly the same thing.
"""
from __future__ import annotations

import re

from .classify import own_sentences

# "说了多少遍", "I told you", "for the third time": the user had to say it again.
REPEATED = re.compile(
    r"说了(?:多少|几|好几|无数|\d+|[二三四五六七八九十]+)(?:遍|次)|讲了(?:多少|几|好几|无数)(?:遍|次)|(?:跟|和|给)你说(?:过|了)|"
    r"我(?:已经|早就|刚才|刚刚|之前|前面|明明|都)(?:说|讲|强调|告诉你)(?:过|了)|我说过(?:了)?|"
    r"第[二三四五六七八九十\d]+(?:次|遍)(?:了|还)?|多少次了|几次了|又一次|一次又一次|反反复复|"
    r"\bhow many times\b|\bi (?:already |just |have |'ve )?(?:told|said|asked|explained)\b|"
    r"\bfor the (?:second|third|fourth|fifth|\d+(?:st|nd|rd|th)) time\b|\bagain and again\b|\byou keep\b|"
    r"\bkeeps? (?:breaking|failing|ignoring|doing|getting)\b|\bstop (?:guessing|ignoring|changing|doing)\b", re.I)
# Anger at the way the work is done: not listening, random edits.
ANGRY = re.compile(
    r"(?:你|您)(?:到底|究竟|是不是|能不能|会不会|有没有)(?:在)?(?:听|看|认真|理解|读|动脑)|听不懂|看不懂|没听(?:懂|明白|进去)|"
    r"(?:根本|完全|压根)(?:没|不)(?:听|看|理解|按|懂)|(?:别|不要)(?:再)?(?:瞎|乱)(?:改|搞|写|猜|动)|乱改|瞎改|乱搞|瞎搞|瞎猜|"
    r"\bare you (?:even )?(?:listening|reading)\b|\bdid you (?:even )?read\b|\blisten to me\b|\bpay attention\b|"
    r"\bread (?:what|my) (?:i|message|instructions)\b", re.I)
EXASPERATED = re.compile(
    r"怎么又|又(?:错|坏|出现|没|不|回|崩|挂|报错)|还是(?:不行|没|不对|错|一样)|仍然(?:没|不|是)|依然(?:没|不|是)|搞什么|什么鬼|"
    r"离谱|无语|服了|受不了|气死|烦死|崩溃|醉了|吐了|浪费(?:我的?)?时间|有完没完|"
    r"\bseriously\b|\bwtf\b|\bffs\b|\bwhat the (?:hell|heck|fuck)\b|\bridiculous\b|\buseless\b|\bterrible\b|\bawful\b|"
    r"\bfrustrat|\bannoy|\bwasting my time\b|\bstill (?:broken|wrong|failing|not)\b|\bnot again\b", re.I)
# The work is over-built: "你不用搞这么复杂", "直接全选删除就完事了".
OVERDONE = re.compile(r"(?:不用|不要|别|没必要|不需要)(?:搞|弄|做|写)?(?:得)?(?:这么|那么|太)(?:复杂|麻烦)|太复杂|搞复杂|过度设计|"
                      r"就完事了|没必要(?:备份|检查|分析)|\bover-?engineer|\btoo complicated\b|\bkeep it simple\b", re.I)
INSULT = re.compile(r"垃圾|废物|愚蠢|蠢|白痴|弱智|智障|没用|\bstupid\b|\bidiot|\bdumb\b|\bmoron|\bgarbage\b|\btrash\b|"
                    r"\bincompetent\b|\bclueless\b", re.I)
AT_WORK = re.compile(r"你|您|\byou\b|\byour\b|\bcodex\b", re.I)
EMPHASIS = re.compile(r"[!！]{2,}|[?？]{2,}|[!！?？]{3,}")
SHOUT = re.compile(r"\b[A-Z]{2,}(?:['’][A-Z]+)?(?:\s+[A-Z]{2,}(?:['’][A-Z]+)?){2,}\b")
SHOUT_WORDS = {"NOT", "DO", "DON'T", "DONT", "STOP", "WHY", "THE", "YOU", "AGAIN", "NEVER", "ALWAYS", "READ", "LISTEN",
               "WRONG", "WHAT", "SAID", "TOLD", "PLEASE", "JUST", "NO"}
WINDOW = 10        # user messages looked back over (acknowledgements and automated notices are skipped)
SIMILAR = 0.3      # token overlap that makes two messages the same problem
STRONG = 2         # score of strong dissatisfaction
SAME_PROBLEM = 3   # raised this many times (or twice with some dissatisfaction): drift


def dissatisfaction(text: str) -> tuple[int, list[str]]:
    """Score of the user's own words and the phrases that scored."""
    score, phrases = 0, []
    for sentence in own_sentences(text):
        found = 0
        for pattern, weight in ((REPEATED, 2), (ANGRY, 2)):
            match = pattern.search(sentence)
            if match:
                found += weight
                phrases.append(match.group(0))
        aimed = bool(AT_WORK.search(sentence))
        for pattern in (EXASPERATED, OVERDONE, INSULT):
            match = pattern.search(sentence)
            if match:
                found += 2 if aimed else 1
                phrases.append(match.group(0))
        if EMPHASIS.search(sentence):
            found += 1
        shout = SHOUT.search(sentence)
        if shout and SHOUT_WORDS & set(shout.group(0).replace("’", "'").split()):
            found += 1
            phrases.append(shout.group(0))
        score += found
    return score, list(dict.fromkeys(phrases))


def tokens(text: str) -> set[str]:
    """Content tokens: CJK character pairs, words of three letters or more, numbers, paths and identifiers."""
    text = text.lower()
    found = set(re.findall(r"[a-z_][a-z0-9_./-]{2,}|\d{2,}", text))
    for run in re.findall(r"[㐀-鿿]+", text):
        found.update(run[i:i + 2] for i in range(len(run) - 1))
    return found - {"the", "and", "for", "you", "this", "that", "with", "not", "but", "are", "was", "have", "again", "still"}


def similarity(a: str, b: str) -> float:
    left, right = tokens(a), tokens(b)
    return len(left & right) / len(left | right) if left and right else 0.0


def assess(messages: list[dict]) -> dict | None:
    """Drift at the last of `messages` (oldest first; each {"text", "classes", "rows"}), or None. Returns the
    reasons, the rows (R ids) of the messages that raised the problem, and the dissatisfaction phrases."""
    if not messages:
        return None
    current = messages[-1]
    score, phrases = dissatisfaction(current["text"])

    def feedback(message):
        return bool(message["classes"] & {"correction", "duplicate", "redirect"}) or dissatisfaction(message["text"])[0] > 0

    same, unbroken = [], True
    for message in reversed(messages[:-1]):
        alike = similarity(message["text"], current["text"]) >= SIMILAR
        if unbroken and feedback(message):
            same.append(message)
        elif alike:
            same.append(message)
            continue
        else:
            unbroken = False
    if not (feedback(current) or same):
        return None
    count = 1 + len(same)
    reasons = []
    if score >= STRONG:
        reasons.append("strong dissatisfaction (" + ", ".join(f"“{p}”" for p in phrases[:3]) + ")" if phrases
                       else "strong dissatisfaction")
    if count >= SAME_PROBLEM or (count >= 2 and score >= 1):
        reasons.append(f"the same problem raised {count} times")
    if not reasons:
        return None
    rows = [row for message in [*reversed(same), current] for row in message["rows"][:1]]
    return {"reasons": reasons, "rows": rows, "count": count, "score": score, "phrases": phrases[:3]}
