// Food-name suggestions for the "add a meal" field. Pure functions only (no DOM), so the
// same code runs in the page (index.html) and in the Node tests (tests/test_food_search_js.py).
(() => {
  const MIN_QUERY_LENGTH = 1;  // one letter already narrows the list a lot, and Hebrew food names are often short
  const MAX_SUGGESTIONS = 20;  // the list scrolls; more than this is noise on a phone

  // Case-insensitive, ignores leading/trailing/repeated spaces (Hebrew or not). Same idea as the server's
  // normalize_food_name, which decides what counts as a duplicate name.
  const normalizeFoodText = text => String(text).trim().split(/\s+/).join(' ').toLowerCase();

  // Foods that would look the same in the list: same name, same pool, same calories.
  const identityKey = food => [normalizeFoodText(food.name), food.scope, food.calories].join('\u0000');

  // Drops identical foods, keeping the oldest (lowest id) of each; the order of `foods` is kept.
  function uniqueFoods(foods) {
    const oldest = new Map();
    foods.forEach(food => { const key = identityKey(food), kept = oldest.get(key); if (!kept || food.id < kept.id) oldest.set(key, food); });
    return foods.filter(food => oldest.get(identityKey(food)) === food);
  }

  // 0: the name starts with the text, 1: a later word does, 2: it appears elsewhere. Ties keep the server's
  // order (alphabetical), which is the ranking the old list had.
  function rank(name, query) {
    if (name.startsWith(query)) return 0;
    return ` ${name}`.includes(` ${query}`) ? 1 : 2;
  }

  // [{ food, name, calories, pool }]. `pool` ('global' / 'personal') is set only when two suggestions would
  // otherwise look the same (same name and calories, one global and one personal); otherwise it is null.
  function foodSuggestions(foods, query, limit = MAX_SUGGESTIONS) {
    const text = normalizeFoodText(query);
    if (text.length < MIN_QUERY_LENGTH) return [];
    const unique = uniqueFoods(foods);
    const lookalikes = new Map();
    unique.forEach(food => { const key = `${normalizeFoodText(food.name)}\u0000${food.calories}`; lookalikes.set(key, (lookalikes.get(key) || 0) + 1); });
    return unique
      .map((food, index) => ({ food, index, name: normalizeFoodText(food.name) }))
      .filter(match => match.name.includes(text))
      .sort((a, b) => rank(a.name, text) - rank(b.name, text) || a.index - b.index)
      .slice(0, limit)
      .map(({ food, name }) => ({ food, name: food.name, calories: food.calories,
                                  pool: lookalikes.get(`${name}\u0000${food.calories}`) > 1 ? food.scope : null }));
  }

  // Foods whose name equals the text (normalized), identical ones counted once. Used when the user typed a full
  // name without picking from the list: one result -> log it, several -> ask them to pick, none -> new food.
  function findFoodsByName(foods, text) {
    const wanted = normalizeFoodText(text);
    return uniqueFoods(foods).filter(food => normalizeFoodText(food.name) === wanted);
  }

  globalThis.foodSearch = { MIN_QUERY_LENGTH, MAX_SUGGESTIONS, normalizeFoodText, uniqueFoods, foodSuggestions, findFoodsByName };
})();
