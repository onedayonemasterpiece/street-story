"""Literal owner-selected search terms; semantic search stays provider-owned."""


def fact_search_queries(identity, requested=None):
    anchor = identity.get('search_anchor') or {}
    city = str(anchor.get('city') or identity.get('locality') or '').strip()
    address = str(anchor.get('address') or ' '.join(str(anchor.get(key) or '').strip()
                  for key in ('street', 'house_number'))).strip()
    name = str(identity.get('candidate_name') or '').strip()
    names = [value for value in anchor.get('names', []) if isinstance(value, str) and value.strip()]
    aliases = [value for value in anchor.get('historical_names', []) if isinstance(value, str) and value.strip()]
    def joined(*parts):
        return ' '.join(dict.fromkeys(part.strip() for part in parts if part and part.strip()))[:600]
    first = str(requested or joined(city, address, name)).strip()[:600]
    # One different, meaningful continuation after a closed empty discovery.
    # Let the search worker refine it using its saved history and research goal.
    alternatives = [joined(city, value, address) for value in [*aliases, *names]]
    if address:
        alternatives.append(joined(city, address))
    if name:
        alternatives.append(joined(city, name))
    alternative = next((query for query in alternatives if query and query != first), None)
    if not alternative and first:
        alternative = joined(first, 'история архитектура')
    return [query for query in (first, alternative) if query]
