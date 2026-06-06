"""Pack art URL helpers and Scrydex discovery."""

from __future__ import annotations

import asyncio

from poke_pon_bot.services.pack_series_loader import (
    _PLACEHOLDER_IMAGE_BYTES,
    _discover_scrydex_pack_art,
    has_real_pack_art_url,
    is_logo_pack_art_url,
    pokemontcg_logo_url,
    scrydex_sealed_image_url,
)


def test_is_logo_pack_art_url() -> None:
    assert is_logo_pack_art_url(pokemontcg_logo_url("sv1"))
    assert is_logo_pack_art_url(None)
    assert not is_logo_pack_art_url(scrydex_sealed_image_url("sv1-s1"))
    assert has_real_pack_art_url(scrydex_sealed_image_url("sv1-s1"))
    assert not has_real_pack_art_url(pokemontcg_logo_url("neo1"))


def test_discover_scrydex_skips_placeholder_picks_first_real() -> None:
  class FakeResp:
    def __init__(self, body: bytes, status: int = 200) -> None:
      self.content = body
      self.status_code = status

  class FakeClient:
    async def get(self, url: str, follow_redirects: bool = True) -> FakeResp:
      del follow_redirects
      if url.endswith("-s1/large") or url.endswith("-s2/large"):
        return FakeResp(b"x" * _PLACEHOLDER_IMAGE_BYTES)
      if url.endswith("-s3/large"):
        return FakeResp(b"real-pack-bytes")
      return FakeResp(b"", status=404)

  found = asyncio.run(_discover_scrydex_pack_art(FakeClient(), "gym1"))
  assert found == scrydex_sealed_image_url("gym1-s3")


def test_discover_scrydex_returns_none_when_all_placeholder() -> None:
  class FakeResp:
    content = b"x" * _PLACEHOLDER_IMAGE_BYTES
    status_code = 200

  class FakeClient:
    async def get(self, url: str, follow_redirects: bool = True) -> FakeResp:
      del url, follow_redirects
      return FakeResp()

  assert asyncio.run(_discover_scrydex_pack_art(FakeClient(), "neo1")) is None
