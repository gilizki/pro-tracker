"""Every number and text of the friend push notifications, in one place.

Change a threshold or a wording here and nothing else needs to change.
"""
from app.core.israel_time import DAY_START_HOUR

# ----- thresholds -----------------------------------------------------------
# All hours are Israel wall-clock hours. The tracker day starts at DAY_START_HOUR (02:00).
PROTEIN_MACHINE_GRAMS = 120   # 💪 the day's protein total reaches this many grams
BULLDOZER_KCAL = 2000         # 🚜 the day's calorie total reaches this many kcal...
BULLDOZER_BEFORE_HOUR = 20    # ...on a meal logged from the day start (02:00) until 19:59
# 🌙 Midnight Munch: a meal logged from 00:00 until the day start (01:59), see israel_time.DAY_START_HOUR.
MIDNIGHT_MUNCH_UNTIL_HOUR = DAY_START_HOUR
EARLY_BIRD_BEFORE_HOUR = 8    # ☀️ a meal logged from the day start (02:00) until 07:59
BEFORE_TEN_BEFORE_HOUR = 10   # 🍳 a meal logged from the day start (02:00) until 09:59
MEAL_PROTEIN_GRAMS = 25       # 💨 the logged meal ITSELF has MORE than this many grams of protein
                              #    (amount_grams * protein per 100 g / 100, unrounded)

# ----- types ----------------------------------------------------------------
PROTEIN_MACHINE = "protein_machine"
MIDNIGHT_MUNCH = "midnight_munch"
BULLDOZER = "bulldozer"
EARLY_BIRD = "early_bird"
BEFORE_TEN = "before_ten"
MEAL_PROTEIN = "meal_protein"

# Each recipient receives each type at most ONCE per tracker day, no matter which
# friend triggered it. A single meal sends at most ONE notification to each
# recipient: the first type in this order that applies and was not already sent
# to them today. If none is left, nothing is sent (there is no generic fallback).
# Types that can only happen now (time windows, this meal's protein) come before
# the day totals, which keep applying to every later meal of the day.
PRIORITY = (MIDNIGHT_MUNCH, EARLY_BIRD, BEFORE_TEN, MEAL_PROTEIN, BULLDOZER, PROTEIN_MACHINE)
ALL_TYPES = PRIORITY

# ----- push content -----------------------------------------------------------
OPEN_FRIENDS_URL = "/?open=friends"   # handled by frontend/notifications.js
OPEN_MAIN_URL = "/"
PUSH_TTL_SECONDS = 12 * 60 * 60       # the push service drops undelivered messages after this

# What a notification deliberately shows: the friend's username ({name}) and what
# the type means. MEAL_PROTEIN also shows that meal's protein amount ({protein},
# one decimal, without ".0" for whole numbers). Never food names, and never the
# day's exact totals (the day-total types only name their fixed threshold).
MESSAGES = {
    PROTEIN_MACHINE: {
        "title": "💪 מכונת חלבון",
        "body": "{name} עבר/ה " + str(PROTEIN_MACHINE_GRAMS) + " גרם חלבון היום. השרירים מוחאים כפיים 👏",
        "url": OPEN_FRIENDS_URL,
        "silent": False,
    },
    MIDNIGHT_MUNCH: {
        "title": "🌙 מאנץ' לילי",
        "body": "{name} נתפס/ה ליד המקרר אחרי חצות 👀",
        "url": OPEN_MAIN_URL,
        "silent": True,  # no sound/vibration where the platform supports it
    },
    BULLDOZER: {
        "title": "🚜 בולדוזר",
        "body": "{name} כבר עבר/ה " + str(BULLDOZER_KCAL) + " קלוריות, והשעה עוד לא "
                + str(BULLDOZER_BEFORE_HOUR) + ":00 😅",
        "url": OPEN_FRIENDS_URL,
        "silent": False,
    },
    EARLY_BIRD: {
        "title": "☀️ השכמה מוקדמת",
        "body": "{name} כבר אוכל. הוא ישן בכלל?",
        "url": OPEN_FRIENDS_URL,
        "silent": False,
    },
    BEFORE_TEN: {
        "title": "🍳 ארוחת בוקר מוקדמת",
        "body": "עוד לא עשר בבוקר ו{name} כבר דפק ארוחה",
        "url": OPEN_FRIENDS_URL,
        "silent": False,
    },
    MEAL_PROTEIN: {
        "title": "💨 פצצת חלבון",
        "body": "{name} אכל {protein} גרם חלבון. עם כזו כמות הוא בטח מפליץ ניחוחות כמו חירייה",
        "url": OPEN_FRIENDS_URL,
        "silent": False,
    },
}
