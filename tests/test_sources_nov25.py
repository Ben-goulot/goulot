"""Sources of 25 November 2026: China and mining feeds to check from the Mac, and what the check says of a feed."""

from __future__ import annotations

from datetime import date

from goulot.cli.check_sources import candidate_targets, describe_feed
from goulot.collect.base import load_sources
from goulot.net.allowlist import load_allowlist
from goulot.settings import CONFIG_DIR

ON = ["media_miningweekly", "media_mining_technology"]  # checked from the Mac, switched on with the user's yes
OFF = ["media_smm", "media_miningweekly_asia", "media_globalminingreview"]  # stale, duplicate, missing


def test_mining_feeds_on_and_the_others_left_off() -> None:
    found = load_sources()
    assert all(found[sid]["enabled"] is True and found[sid]["family"] == "media" for sid in ON)
    assert all(found[sid]["enabled"] is False for sid in OFF)
    allowed = load_allowlist(CONFIG_DIR)
    assert allowed.allows("www.miningweekly.com") and allowed.allows("www.mining-technology.com")
    for host in ("rss.metal.com", "news.metal.com", "www.globalminingreview.com"):
        assert not allowed.allows(host)  # nothing reaches them from the watch


def test_the_smm_leads_are_asked_by_the_check() -> None:
    names = dict(candidate_targets(date(2026, 11, 25)))
    assert names["piste_smm_accueil"] == "https://news.metal.com/"


def test_describe_feed_counts_dates_and_summaries() -> None:
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Gallium prices rise</title><link>https://news.metal.com/a</link>
<pubDate>Mon, 23 Nov 2026 08:00:00 GMT</pubDate><description>Prices of gallium rose on tight supply in China.</description></item>
<item><title>Lithium plant halts</title><link>https://news.metal.com/b</link><description>Lithium plant halts</description></item>
</channel></rss>"""
    assert describe_feed(feed) == "2 articles, 1 datés, le plus récent du 23/11/2026, 1 avec résumé"
    assert describe_feed(b"<html><body>no feed</body></html>") == "flux vide (ou page qui n'est pas un flux)"
    assert describe_feed(b"not xml at all <").startswith("pas un flux lisible")
