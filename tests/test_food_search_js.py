"""Food-name suggestions. These run the REAL frontend/food-search.js in Node (skipped when Node is not
installed), plus static checks on index.html that no browser-native list can appear in the field."""
import json
import re
import shutil
import subprocess

import pytest

from conftest import PROJECT_ROOT

NODE = shutil.which("node")
FOOD_SEARCH_JS = PROJECT_ROOT / "frontend" / "food-search.js"
INDEX_HTML = PROJECT_ROOT / "frontend" / "index.html"
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

FOODS = [  # the server's order: by name, then id
    {"id": 7, "name": "Banana", "calories": 89, "scope": "global"},
    {"id": 3, "name": "Pro Protein Shake", "calories": 120, "scope": "personal"},
    {"id": 9, "name": "Pro Protein Shake", "calories": 120, "scope": "personal"},  # identical to #3 -> hidden
    {"id": 4, "name": "pro  protein shake", "calories": 120, "scope": "personal"},  # same after normalizing -> hidden
    {"id": 5, "name": "Pro Protein Shake", "calories": 120, "scope": "global"},  # other pool -> shown, marked
    {"id": 6, "name": "Pro Protein Shake", "calories": 140, "scope": "personal"},  # other calories -> shown
    {"id": 8, "name": "Rice cake", "calories": 387, "scope": "global"},
    {"id": 10, "name": "Shake", "calories": 60, "scope": "global"},
    {"id": 11, "name": "יוגורט   חלבון", "calories": 70, "scope": "global"},
    {"id": 12, "name": "חלב", "calories": 60, "scope": "personal"},
]


def run(expression, foods=FOODS):
    script = ("require(process.argv[1]); const foods = JSON.parse(process.argv[2]);"
              f"process.stdout.write(JSON.stringify({expression}));")
    out = subprocess.run([NODE, "-e", script, str(FOOD_SEARCH_JS), json.dumps(foods)],
                         capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


def suggest(query, foods=FOODS):
    return run(f"foodSearch.foodSuggestions(foods, {json.dumps(query)})"
               ".map(s => [s.food.id, s.name, s.calories, s.pool])", foods)


@needs_node
@pytest.mark.parametrize("query", ["", " ", "   \t "])
def test_empty_or_blank_query_suggests_nothing(query):
    assert suggest(query) == []


@needs_node
def test_one_character_is_enough():
    assert run("foodSearch.MIN_QUERY_LENGTH") == 1
    assert [s[0] for s in suggest("b")] == [7]


@needs_node
def test_contains_case_insensitive_and_ranked_prefix_first():
    # "shake": names starting with it, then a later word starting with it; ties keep the server's order.
    assert [s[0] for s in suggest("SHAKE")] == [10, 3, 5, 6]
    assert [s[0] for s in suggest("ake")] == [3, 5, 6, 8, 10]  # inside a word, server order
    assert [s[0] for s in suggest("  pro   PROTEIN ")] == [3, 5, 6]  # extra spaces ignored


@needs_node
def test_hebrew_ignores_extra_spaces():
    assert [s[0] for s in suggest("יוגורט חלבון")] == [11]
    assert [s[0] for s in suggest("חלב")] == [12, 11]  # starts with it, then a later word


@needs_node
def test_identical_foods_shown_once_without_numbering():
    rows = suggest("protein")
    assert rows == [[3, "Pro Protein Shake", 120, "personal"],  # oldest of the identical ones
                    [5, "Pro Protein Shake", 120, "global"],  # same name+calories, other pool: marked
                    [6, "Pro Protein Shake", 140, None]]
    assert not any("#" in name for _, name, _, _ in rows)


@needs_node
def test_find_by_exact_name_counts_identical_foods_once():
    only_identical = [f for f in FOODS if f["id"] in (3, 9, 4)]
    assert run("foodSearch.findFoodsByName(foods, ' PRO protein  shake').map(f => f.id)", only_identical) == [3]
    assert run("foodSearch.findFoodsByName(foods, 'Pro Protein Shake').map(f => f.id)") == [3, 5, 6]
    assert run("foodSearch.findFoodsByName(foods, 'Pro').map(f => f.id)") == []


@needs_node
def test_result_count_is_capped():
    many = [{"id": i, "name": f"Food {i:03}", "calories": i, "scope": "global"} for i in range(50)]
    assert len(suggest("food", many)) == run("foodSearch.MAX_SUGGESTIONS") == 20


def test_no_native_datalist_or_autocomplete_on_the_food_field():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "<datalist" not in html
    field = re.search(r'<input id="food-search"[^>]*>', html, re.S).group(0)
    assert " list=" not in field
    for attribute in ('autocomplete="off"', 'autocorrect="off"', 'autocapitalize="off"', 'spellcheck="false"',
                      'role="combobox"', 'aria-controls="food-suggestions"'):
        assert attribute in field
    assert 'src="/frontend/food-search.js' in html
    assert "#${" not in html  # the old "Name #2" labels are gone
