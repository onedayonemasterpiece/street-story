"""Publisher postal group membership: retrieval link, never individual identity.

A historical catalogue may assign one complex a list (80,82,84,86,88)
or a bounded interval (51–57), while OSM maps physically distinct bodies
and doors. A member relationship helps fetch architectural descriptions
but does NOT establish which building appears in SOURCE.
"""
from __future__ import annotations
import re

_STREET_LABELS = {'ул','улица','пр','проспект','пер','переулок'}
_POSTAL_ATOM = r'\d{1,4}[а-яa-z]{0,2}'
_POSTAL_ELEMENT = _POSTAL_ATOM+r'(?:\s*[-–]\s*'+_POSTAL_ATOM+r')?'
_ATOM_RE = re.compile(r'^(\d{1,4})([а-яa-z]{0,2})$', re.I)


def _house_atom(raw: str):
    value=raw.strip().casefold().replace('а','a').replace('б','b')
    m=_ATOM_RE.fullmatch(value)
    return (int(m.group(1)),m.group(2)) if m else None


def _house_units(raw: str):
    parts=re.split(r'\s*[-–]\s*',raw)
    if not 1<=len(parts)<=2:
        return None
    first=_house_atom(parts[0])
    if first is None:
        return None
    if len(parts)==1:
        return {first}
    last=_house_atom(parts[1])
    if last is None:
        return None
    if first[1]==last[1]=='' and 0<=last[0]-first[0]<=30:
        return {(v,'') for v in range(first[0],last[0]+1)}
    # Do not interpolate from 82 to 82A or across different house numbers
    # with suffixes; two explicit endpoints only.
    return {first,last} if first[0]==last[0] else None


def _publisher_group(address_text: str, street: str):
    if not isinstance(address_text,str) or not isinstance(street,str):
        return []
    token=re.compile(r'[^\W_]+',re.UNICODE)
    road=[word for word in token.findall(street.casefold())
        if word not in _STREET_LABELS]
    if not road:
        return []
    body=address_text.casefold()
    matches=list(token.finditer(body))
    for i in range(len(matches)-len(road)+1):
        if [m.group() for m in matches[i:i+len(road)]]!=road:
            continue
        tail=body[matches[i+len(road)-1].end():]
        tail=re.sub(r'^\s*[,.;]?\s*(?:(?:д\.?|дом|№)\s*)?','',tail)
        match=re.match(r'('+_POSTAL_ELEMENT+r'(?:\s*[,;/]\s*'
            +_POSTAL_ELEMENT+r')*)',tail,re.I)
        if match:
            return [item.strip() for item in re.split(r'\s*[,;/]\s*',match.group(1))]
    return []


def complex_postal_member(address_text: str, street: str, house_number: str):
    """Possible subset of a received complex, not an exact postal binding.

    The single known house 6 never matches 6A or a lone 6A address. The
    publisher postal group must be verified metadata, not extracted prose.
    """
    if not isinstance(house_number,str):
        return False
    group=_publisher_group(address_text,street)
    if not group:
        return False
    if len(group)==1 and '-' not in group[0] and '–' not in group[0]:
        return False
    total=set()
    for item in group:
        units=_house_units(item)
        if units is None:
            return False
        total.update(units)
    target=set()
    for part in re.split(r'\s*[,;/]\s*',house_number):
        units=_house_units(part)
        if units is None:
            return False
        target.update(units)
    return bool(target and target<=total)
