"""Three calendar months of continuously unreferenced Docker image retention.

This worker never reacts to pressure and never prunes containers or volumes.
Image identity is immutable. Callers coordinate reference checks/removal with
installation admission and supply a trustworthy UTC clock.
"""
import calendar
from datetime import datetime, timezone


def three_months_later(timestamp):
    date = datetime.fromtimestamp(timestamp, timezone.utc)
    month_index = date.year * 12 + date.month - 1 + 3
    year, month = divmod(month_index, 12)
    month += 1
    day = min(date.day, calendar.monthrange(year, month)[1])
    return date.replace(year=year, month=month, day=day).timestamp()


def observe(history, images, referenced, now, *, clock_trusted):
    """Return updated state and candidates; recheck references before removal."""
    if not isinstance(now, (int, float)) or now < 0:
        raise ValueError('invalid image retention clock')
    history = history or {'version': 1, 'images': {}, 'last_observed': now}
    if history.get('version') != 1:
        raise ValueError('unsupported retention history')
    previous = history.get('images', {})
    rollback = now < history.get('last_observed', now)
    images, referenced = set(images), set(referenced)
    updated, candidates = {}, []
    for image in sorted(images):
        if image in referenced:
            continue  # any renewed reference resets the eligibility clock
        first = previous.get(image, {}).get('unreferenced_since', now)
        if not clock_trusted or rollback:
            # An untrusted observation cannot prove continuity. Require a fresh
            # full window once the clock recovers, rather than deleting early.
            updated[image] = {'unreferenced_since': None}
            continue
        if first is None:
            first = now
        if first > now:
            first = now
        updated[image] = {'unreferenced_since': first}
        if now >= three_months_later(first):
            candidates.append(image)
    return {'version': 1, 'images': updated,
            'last_observed': max(now, history.get('last_observed', now))}, candidates


def remove_eligible(candidates, references, remove):
    """The caller holds the short installation lock around this operation.

    No force deletion: Docker itself remains authoritative about shared layers
    and running/stopped-container references. Errors leave the image intact.
    """
    removed = []
    for image in candidates:
        if image not in references():
            remove(image)
            removed.append(image)
    return removed
