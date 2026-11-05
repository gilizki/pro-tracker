"""Every number and text of the friend push notifications, in one place.

Change a threshold here and nothing else needs to change.
"""
from israel_time import DAY_START_HOUR

# ----- thresholds -----------------------------------------------------------
PROTEIN_MACHINE_GRAMS = 120   # 💪 the day's protein total reaches this many grams
BULLDOZER_KCAL = 2000         # 🚜 the day's calorie total reaches this many kcal...
BULLDOZER_BEFORE_HOUR = 20    # ...on a meal logged from the day start (02:00) until 19:59 Israel time
# 🌙 Midnight Munch: a meal logged from 00:00 until the day start (01:59), see israel_time.DAY_START_HOUR.
MIDNIGHT_MUNCH_UNTIL_HOUR = DAY_START_HOUR

DAILY_CAP = 2                 # notifications one user can receive per tracker day, in total

# ----- types ----------------------------------------------------------------
PROTEIN_MACHINE = "protein_machine"
MIDNIGHT_MUNCH = "midnight_munch"
BULLDOZER = "bulldozer"
ALL_TYPES = (PROTEIN_MACHINE, MIDNIGHT_MUNCH, BULLDOZER)

# A single meal sends at most ONE notification to each recipient: the first type
# in this order that fires (and was not already sent today about that friend).
PRIORITY = (MIDNIGHT_MUNCH, BULLDOZER, PROTEIN_MACHINE)

# ----- push content -----------------------------------------------------------
OPEN_FRIENDS_URL = "/?open=friends"   # handled by frontend/notifications.js
OPEN_MAIN_URL = "/"
PUSH_TTL_SECONDS = 12 * 60 * 60       # the push service drops undelivered messages after this

# Only the friend's username and what the type means. Never foods or exact totals.
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
}
